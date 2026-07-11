use std::collections::BTreeMap;
use std::fmt;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use base64::{
    Engine as _, engine::general_purpose::STANDARD, engine::general_purpose::URL_SAFE_NO_PAD,
};
use futures_util::{SinkExt, StreamExt};
use hmac::{Hmac, Mac};
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use tokio::io::AsyncWriteExt;
use tokio::sync::{Notify, Semaphore, mpsc};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::protocol::Message;

type HmacSha256 = Hmac<Sha256>;
const SUBPROTOCOL: &str = "wiki-relay.v1";
const STREAM_ID_LEN: usize = 16;

// 并发转发上界：限制同时打到本机 server 的请求数，防止公网刷一波 / 客户端 bug 无界回源。
// 取 permit 在 task 内（见 connect_once），故这是“并发活跃转发数”上界，不阻塞读循环；
// 留足头寸给长连接（SSE/MCP）各占一个的情况。
const MAX_INFLIGHT: usize = 128;
// 发送队列容量（有界）：公网下游很慢时回压，避免无限占内存。
const SEND_QUEUE_CAP: usize = 256;
// 回源本机 server 的连接超时：本机端口异常应快速暴露，而不是挂住。
const ORIGIN_CONNECT_TIMEOUT: Duration = Duration::from_secs(2);
// reader 读空闲超时：半开连接（对端无 FIN/close）下，靠它主动重连。
// Worker 每 20s 发一次 JSON 心跳，健康连接至少每 20s 见到一帧，不会误触发。
const READ_IDLE_TIMEOUT: Duration = Duration::from_secs(60);
// 单帧写入超过此时间通常意味着 TCP/TUN/代理链路已经半开。writer 必须退出，
// 由 reader 侧 watchdog 观察到并重建整条 WebSocket，而不是永久卡在 SinkExt::send。
const WRITE_TIMEOUT: Duration = Duration::from_secs(15);
// writer 已卡住或退出时，有界队列最终会填满。任何生产者最多等待这么久，
// 超时后快速失败；心跳生产者会据此触发整条连接重建。
const SEND_QUEUE_TIMEOUT: Duration = Duration::from_secs(10);
// 健康快照写入持久化日志的频率。它既是诊断时间线，也能证明连接循环仍在调度。
const WATCHDOG_LOG_INTERVAL: Duration = Duration::from_secs(60);
// 公网端到端探测会经过 Worker 路由 -> 当前 WebSocket -> 本机 healthz -> 回包，
// 能发现“DO 还在发心跳但公网请求已无法到达”的假活连接。连续失败才重连，避免单次抖动。
const END_TO_END_PROBE_INTERVAL: Duration = Duration::from_secs(60);
const END_TO_END_PROBE_TIMEOUT: Duration = Duration::from_secs(10);
const END_TO_END_FAILURE_THRESHOLD: u32 = 2;
// 重连退避上界。
const MAX_BACKOFF: Duration = Duration::from_secs(30);
// 部署版本轮询周期：每隔这么久打一次 Worker 顶层 /_version（永远是最新部署的代码），
// 版本号变化即认定 Worker 重新部署，主动断开重连以落到新 DO 实例上。
// 打顶层 fetch 而非 DO，故不受「旧 DO 实例仍存活并继续发心跳、致读空闲兜底永不触发」的影响。
const VERSION_POLL_INTERVAL: Duration = Duration::from_secs(45);
static NEXT_CONNECTION_ID: AtomicU64 = AtomicU64::new(1);

#[derive(Clone)]
pub struct ConnectorConfig {
    pub worker_ws: String,
    pub origin: String,
    pub identity_file: PathBuf,
    /// Browser API Bearer token used only in the HTTPS health probe header.
    /// It is never included in the probe URL or tracing fields.
    pub probe_token: String,
}

impl fmt::Debug for ConnectorConfig {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ConnectorConfig")
            .field("worker_ws", &worker_endpoint_for_log(&self.worker_ws))
            .field("origin", &self.origin)
            .field("identity_file", &self.identity_file)
            .field("probe_token_configured", &(!self.probe_token.is_empty()))
            .finish()
    }
}

impl Default for ConnectorConfig {
    fn default() -> Self {
        let identity_file = dirs::home_dir()
            .unwrap_or_else(|| PathBuf::from("."))
            .join(".my-llm-wiki")
            .join("connector")
            .join("identity.json");

        Self {
            worker_ws: "wss://wiki.htmlgo.to".to_string(),
            origin: "http://127.0.0.1:8800".to_string(),
            identity_file,
            probe_token: String::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Identity {
    pub uid: String,
    pub connector_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum ConnectorEvent {
    IdentityLoaded {
        uid: String,
    },
    Connected {
        uid: String,
    },
    Disconnected,
    Retrying {
        after_ms: u64,
    },
    RequestFinished {
        method: String,
        path: String,
        status: u16,
    },
}

#[derive(Debug, thiserror::Error)]
pub enum ConnectorError {
    #[error("invalid HMAC key")]
    InvalidHmacKey,

    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("http error: {0}")]
    Http(#[from] http::Error),

    #[error("invalid header value: {0}")]
    InvalidHeaderValue(#[from] http::header::InvalidHeaderValue),

    #[error("request error: {0}")]
    Reqwest(#[from] reqwest::Error),

    #[error("url error: {0}")]
    Url(#[from] url::ParseError),

    #[error("websocket error: {0}")]
    WebSocket(#[from] tokio_tungstenite::tungstenite::Error),

    #[error("invalid relay stream id length for {id}")]
    InvalidStreamId { id: String },

    #[error("relay writer closed")]
    WriterClosed,

    #[error("relay websocket write timed out after {after_ms}ms")]
    WriteTimeout { after_ms: u64 },

    #[error("relay send queue stalled for {after_ms}ms")]
    SendQueueStalled { after_ms: u64 },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ConnectionEnd {
    WorkerVersionChanged,
    EndToEndProbeFailed,
    ReadIdleTimeout,
    RemoteStreamClosed,
    RemoteCloseFrame,
    WriterStopped,
    WriterFailed,
    WriterTaskFailed,
    SendQueueStalled,
}

impl ConnectionEnd {
    fn as_str(self) -> &'static str {
        match self {
            Self::WorkerVersionChanged => "worker_version_changed",
            Self::EndToEndProbeFailed => "end_to_end_probe_failed",
            Self::ReadIdleTimeout => "read_idle_timeout",
            Self::RemoteStreamClosed => "remote_stream_closed",
            Self::RemoteCloseFrame => "remote_close_frame",
            Self::WriterStopped => "writer_stopped",
            Self::WriterFailed => "writer_failed",
            Self::WriterTaskFailed => "writer_task_failed",
            Self::SendQueueStalled => "send_queue_stalled",
        }
    }
}

pub type Result<T> = std::result::Result<T, ConnectorError>;

pub fn derive_connector_key(secret: &str, uid: &str) -> Result<String> {
    let mut mac = HmacSha256::new_from_slice(secret.as_bytes())
        .map_err(|_| ConnectorError::InvalidHmacKey)?;
    mac.update(format!("connector:v1:{uid}").as_bytes());
    Ok(URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes()))
}

pub fn redact_token_query(path: &str) -> String {
    path.split_once('?')
        .map(|(base, query)| {
            let query = query
                .split('&')
                .map(|part| {
                    if part
                        .split_once('=')
                        .is_some_and(|(key, _)| key.eq_ignore_ascii_case("token"))
                    {
                        "token=<redacted>".to_string()
                    } else {
                        part.to_string()
                    }
                })
                .collect::<Vec<_>>()
                .join("&");
            format!("{base}?{query}")
        })
        .unwrap_or_else(|| path.to_string())
}

pub async fn run_connector(config: ConnectorConfig) -> Result<()> {
    run_connector_with_events(config, |_| {}).await
}

pub async fn run_connector_with_events<F>(config: ConnectorConfig, emit: F) -> Result<()>
where
    F: Fn(ConnectorEvent) + Send + Sync + 'static,
{
    let emit = Arc::new(emit);
    // 回源本机 server 的 HTTP client，全程复用（连接池/keep-alive），并显式禁用代理：
    // 即便本机开了 Clash/系统代理/TUN，回源 127.0.0.1 也强制直连，不被劫持。
    let client = Arc::new(build_origin_client()?);
    let mut backoff = Duration::from_secs(1);
    loop {
        let connection_id = NEXT_CONNECTION_ID.fetch_add(1, Ordering::Relaxed);
        let mut connected = false;
        match resolve_identity(&config).await {
            Ok(identity) => {
                emit(ConnectorEvent::IdentityLoaded {
                    uid: identity.uid.clone(),
                });
                tracing::info!(
                    uid = identity.uid,
                    origin = config.origin,
                    "relay connector identity loaded"
                );
                tracing::info!(
                    connection_id,
                    worker = %worker_endpoint_for_log(&config.worker_ws),
                    "relay connection attempt started"
                );
                match connect_once(
                    &config,
                    &identity,
                    emit.clone(),
                    client.clone(),
                    connection_id,
                )
                .await
                {
                    Ok(reason) => {
                        connected = true;
                        tracing::warn!(
                            connection_id,
                            reason = reason.as_str(),
                            "relay connector disconnected"
                        );
                    }
                    Err(err) => tracing::warn!(
                        connection_id,
                        error = %safe_error_message(&err),
                        "relay connector connection failed"
                    ),
                }
                emit(ConnectorEvent::Disconnected);
            }
            Err(err) => {
                emit(ConnectorEvent::Disconnected);
                tracing::warn!(error = ?err, "relay connector identity unavailable");
            }
        }
        // 成功连过一次就把退避重置回基线；持续失败则指数退避，避免短时间密集重连。
        if connected {
            backoff = Duration::from_secs(1);
        }
        let wait = backoff + retry_jitter(backoff);
        emit(ConnectorEvent::Retrying {
            after_ms: wait.as_millis() as u64,
        });
        tracing::info!(
            connection_id,
            after_ms = wait.as_millis() as u64,
            "relay reconnect scheduled"
        );
        tokio::time::sleep(wait).await;
        backoff = (backoff * 2).min(MAX_BACKOFF);
    }
}

fn build_origin_client() -> Result<reqwest::Client> {
    Ok(reqwest::Client::builder()
        .no_proxy()
        .connect_timeout(ORIGIN_CONNECT_TIMEOUT)
        .build()?)
}

// 轻量抖动：取当前时间纳秒派生 [0, backoff/2) 的随机偏移，避免多连接器同步重连。
// 不引入 rand 依赖，抖动质量足够打散重连风暴即可。
fn retry_jitter(backoff: Duration) -> Duration {
    let span = backoff.as_millis() as u64 / 2 + 1;
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.subsec_nanos() as u64)
        .unwrap_or(0);
    Duration::from_millis(nanos % span)
}

pub async fn resolve_identity(config: &ConnectorConfig) -> Result<Identity> {
    if let (Ok(uid), Some(key)) = (
        std::env::var("WIKI_UID"),
        std::env::var("CONNECTOR_KEY").ok().or_else(|| {
            std::env::var("RELAY_SECRET").ok().and_then(|secret| {
                derive_connector_key(&secret, &std::env::var("WIKI_UID").ok()?).ok()
            })
        }),
    ) {
        return Ok(Identity {
            uid,
            connector_key: key,
        });
    }

    if config.identity_file.is_file() {
        let text = tokio::fs::read_to_string(&config.identity_file).await?;
        let identity = serde_json::from_str::<Identity>(&text)?;
        if !identity.uid.is_empty() && !identity.connector_key.is_empty() {
            return Ok(identity);
        }
    }

    let identity = provision(config).await?;
    if let Some(parent) = config.identity_file.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    write_identity_file(&config.identity_file, &identity).await?;
    Ok(identity)
}

async fn provision(config: &ConnectorConfig) -> Result<Identity> {
    let url = provision_url(&config.worker_ws)?;
    let response = reqwest::Client::new()
        .post(url)
        .send()
        .await?
        .error_for_status()?;
    Ok(response.json::<Identity>().await?)
}

async fn write_identity_file(path: &PathBuf, identity: &Identity) -> Result<()> {
    let body = serde_json::to_vec_pretty(identity)?;
    #[cfg(unix)]
    {
        let mut file = tokio::fs::OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .mode(0o600)
            .open(path)
            .await?;
        file.write_all(&body).await?;
        file.write_all(b"\n").await?;
    }
    #[cfg(not(unix))]
    {
        tokio::fs::write(path, [&body[..], b"\n"].concat()).await?;
    }
    Ok(())
}

async fn connect_once(
    config: &ConnectorConfig,
    identity: &Identity,
    emit: Arc<dyn Fn(ConnectorEvent) + Send + Sync>,
    client: Arc<reqwest::Client>,
    connection_id: u64,
) -> Result<ConnectionEnd> {
    let connect_url = format!(
        "{}/_connect/{}",
        config.worker_ws.trim_end_matches('/'),
        identity.uid
    );
    let mut request = connect_url.into_client_request()?;
    request.headers_mut().insert(
        http::header::SEC_WEBSOCKET_PROTOCOL,
        HeaderValue::from_str(&format!("{SUBPROTOCOL}, {}", identity.connector_key))?,
    );
    let (ws, response) = tokio_tungstenite::connect_async(request).await?;
    emit(ConnectorEvent::Connected {
        uid: identity.uid.clone(),
    });
    tracing::info!(
        uid = identity.uid,
        origin = config.origin,
        connection_id,
        handshake_status = response.status().as_u16(),
        "relay connector connected"
    );

    // 多路复用：reader 只负责收帧并按请求 spawn 独立 task；所有出站帧经有界队列由唯一 writer 串行写出。
    // 这样一个慢请求 / SSE / MCP 长连接不再阻塞读循环，彻底消除“一个长连接锁死整条隧道”的死锁。
    let (mut sink, mut stream) = ws.split();
    let (tx, mut rx) = mpsc::channel::<Message>(SEND_QUEUE_CAP);
    let frames_written = Arc::new(AtomicU64::new(0));
    let last_write_at_ms = Arc::new(AtomicU64::new(unix_time_ms()));
    let writer_frames_written = frames_written.clone();
    let writer_last_write_at_ms = last_write_at_ms.clone();
    let mut writer = tokio::spawn(async move {
        while let Some(msg) = rx.recv().await {
            match tokio::time::timeout(WRITE_TIMEOUT, sink.send(msg)).await {
                Err(_) => {
                    return Err(ConnectorError::WriteTimeout {
                        after_ms: WRITE_TIMEOUT.as_millis() as u64,
                    });
                }
                Ok(Err(err)) => return Err(ConnectorError::from(err)),
                Ok(Ok(())) => {
                    writer_frames_written.fetch_add(1, Ordering::Relaxed);
                    writer_last_write_at_ms.store(unix_time_ms(), Ordering::Relaxed);
                }
            }
        }
        let _ = sink.close().await;
        Ok::<(), ConnectorError>(())
    });

    let config = Arc::new(config.clone());
    let sem = Arc::new(Semaphore::new(MAX_INFLIGHT));

    // Worker 重新部署检测：轮询顶层 /_version（总是最新代码），版本变化即触发重连。
    // 这条路不经 DO，故能在旧 DO 实例仍活着发心跳、idle 兜底失效时照样发现重新部署。
    let reconnect = Arc::new(Notify::new());
    let version_poll = tokio::spawn(poll_worker_version(
        config.worker_ws.clone(),
        reconnect.clone(),
        connection_id,
    ));
    let end_to_end_reconnect = Arc::new(Notify::new());
    let end_to_end_probe = tokio::spawn(poll_end_to_end_health(
        config.worker_ws.clone(),
        identity.uid.clone(),
        config.probe_token.clone(),
        end_to_end_reconnect.clone(),
        connection_id,
    ));

    let connected_at = tokio::time::Instant::now();
    let mut last_frame_at = tokio::time::Instant::now();
    let mut frames_received = 0_u64;
    let mut heartbeats_received = 0_u64;
    let mut requests_received = 0_u64;
    let mut watchdog_log = tokio::time::interval(WATCHDOG_LOG_INTERVAL);
    watchdog_log.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    // interval() 的第一次 tick 会立即完成；先消费掉，避免刚连接就写一条无意义健康快照。
    watchdog_log.tick().await;

    let reader_result = loop {
        // 读空闲超时：健康连接有 Worker 心跳兜底；超时即认定半开，主动断开重连。
        // 同时监听版本轮询：Worker 重新部署时立即重连，不必等 idle 超时（且 idle 常被旧心跳压住）。
        let message = tokio::select! {
            biased;
            _ = reconnect.notified() => {
                tracing::info!(connection_id, "worker redeployed (version changed), reconnecting");
                break Ok(ConnectionEnd::WorkerVersionChanged);
            }
            _ = end_to_end_reconnect.notified() => {
                tracing::warn!(
                    connection_id,
                    watchdog_reason = ConnectionEnd::EndToEndProbeFailed.as_str(),
                    "relay end-to-end health probe failed repeatedly; forcing reconnect"
                );
                break Ok(ConnectionEnd::EndToEndProbeFailed);
            }
            writer_result = &mut writer => {
                let reason = match writer_result {
                    Ok(Ok(())) => ConnectionEnd::WriterStopped,
                    Ok(Err(err)) => {
                        tracing::warn!(
                            connection_id,
                            watchdog_reason = ConnectionEnd::WriterFailed.as_str(),
                            error = %safe_error_message(&err),
                            "relay writer stopped; forcing reconnect"
                        );
                        ConnectionEnd::WriterFailed
                    }
                    Err(err) => {
                        tracing::warn!(
                            connection_id,
                            watchdog_reason = ConnectionEnd::WriterTaskFailed.as_str(),
                            error = %err,
                            "relay writer task failed; forcing reconnect"
                        );
                        ConnectionEnd::WriterTaskFailed
                    }
                };
                break Ok(reason);
            }
            _ = tokio::time::sleep_until(last_frame_at + READ_IDLE_TIMEOUT) => {
                tracing::warn!(
                    connection_id,
                    watchdog_reason = ConnectionEnd::ReadIdleTimeout.as_str(),
                    idle_ms = READ_IDLE_TIMEOUT.as_millis() as u64,
                    "relay connector read idle timeout; forcing reconnect"
                );
                break Ok(ConnectionEnd::ReadIdleTimeout);
            }
            frame = stream.next() => match frame {
                None => break Ok(ConnectionEnd::RemoteStreamClosed),
                Some(Ok(message)) => message,
                Some(Err(err)) => break Err(ConnectorError::from(err)),
            },
            _ = watchdog_log.tick() => {
                tracing::info!(
                    connection_id,
                    uptime_ms = connected_at.elapsed().as_millis() as u64,
                    last_frame_age_ms = last_frame_at.elapsed().as_millis() as u64,
                    last_write_age_ms = unix_time_ms()
                        .saturating_sub(last_write_at_ms.load(Ordering::Relaxed)),
                    frames_received,
                    frames_written = frames_written.load(Ordering::Relaxed),
                    heartbeats_received,
                    requests_received,
                    inflight = MAX_INFLIGHT.saturating_sub(sem.available_permits()),
                    send_queue_remaining = tx.capacity(),
                    "relay watchdog healthy"
                );
                continue;
            }
        };
        last_frame_at = tokio::time::Instant::now();
        frames_received += 1;
        match message {
            Message::Text(text) => {
                // 先看是不是心跳；是则回 pong，不当请求处理。
                if let Some(pong) = handle_heartbeat(text.as_str()) {
                    heartbeats_received += 1;
                    if let Err(err) = send_frame(&tx, pong).await {
                        tracing::warn!(
                            connection_id,
                            watchdog_reason = ConnectionEnd::SendQueueStalled.as_str(),
                            error = %safe_error_message(&err),
                            "relay heartbeat response could not be queued; forcing reconnect"
                        );
                        break Ok(ConnectionEnd::SendQueueStalled);
                    }
                    continue;
                }
                let request = match serde_json::from_str::<RelayRequest>(text.as_str()) {
                    Ok(request) if request.t == "req" => request,
                    _ => continue,
                };
                requests_received += 1;
                // 限流在 task 内取 permit，而不是在读循环里：否则长连接（SSE/MCP）会各占一个
                // permit 直到结束，占满 MAX_INFLIGHT 后读循环被卡住，等于又把整条隧道锁死。
                // 放到 task 内：读循环永远保持响应（心跳/Close/新请求都不阻塞），permit 只约束
                // 并发转发量；超出上界的新请求以“等待中的轻量 task”排队，不再无界打本机 server。
                let config = config.clone();
                let client = client.clone();
                let tx = tx.clone();
                let sem = sem.clone();
                tokio::spawn(async move {
                    let _permit = match sem.acquire_owned().await {
                        Ok(permit) => permit,
                        Err(_) => return,
                    };
                    handle_request(&config, &client, request, &tx).await;
                });
            }
            Message::Ping(payload) => {
                if let Err(err) = send_frame(&tx, Message::Pong(payload)).await {
                    tracing::warn!(
                        connection_id,
                        watchdog_reason = ConnectionEnd::SendQueueStalled.as_str(),
                        error = %safe_error_message(&err),
                        "relay websocket pong could not be queued; forcing reconnect"
                    );
                    break Ok(ConnectionEnd::SendQueueStalled);
                }
            }
            Message::Close(_) => break Ok(ConnectionEnd::RemoteCloseFrame),
            _ => {}
        }
    };

    // 断开：丢弃 reader 持有的 tx 并中止 writer，触发外层重连。
    // 仍在途的 forward task 持有 tx 克隆，其后续 send 会因 writer 已停而失败并自行退出。
    drop(tx);
    if !writer.is_finished() {
        writer.abort();
    }
    version_poll.abort();
    end_to_end_probe.abort();
    match &reader_result {
        Ok(reason) => tracing::info!(
            connection_id,
            reason = reason.as_str(),
            uptime_ms = connected_at.elapsed().as_millis() as u64,
            frames_received,
            frames_written = frames_written.load(Ordering::Relaxed),
            heartbeats_received,
            requests_received,
            "relay connection ended"
        ),
        Err(err) => tracing::warn!(
            connection_id,
            error = %safe_error_message(err),
            uptime_ms = connected_at.elapsed().as_millis() as u64,
            "relay connection ended with error"
        ),
    }
    reader_result
}

// 轮询 Worker 顶层 /_version 检测重新部署：拿到与「基线」不同的非空版本号即通知重连。
// 打的是无状态顶层 fetch（永远跑最新部署代码），故即便旧 DO 实例仍在发心跳、读空闲兜底
// 不触发，也能在一个轮询周期内发现重新部署。基线在连接建立后首次成功轮询时确定，之后每次
// 重连都会重新取基线（落到新版本上）。端点不存在 / 网络抖动时回 None：不建立基线，也就不会
// 误触发重连——对未升级 /_version 的旧 Worker 优雅降级。
async fn poll_worker_version(worker_ws: String, reconnect: Arc<Notify>, connection_id: u64) {
    let Ok(url) = worker_http_url(&worker_ws, "/_version") else {
        return;
    };
    let Ok(client) = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
    else {
        return;
    };
    let mut baseline: Option<String> = None;
    loop {
        if let Some(version) = fetch_worker_version(&client, &url).await {
            match &baseline {
                None => {
                    tracing::info!(connection_id, worker_version = %version, "relay worker version baseline recorded");
                    baseline = Some(version);
                }
                Some(current) if *current != version => {
                    tracing::warn!(
                        connection_id,
                        previous_version = %current,
                        worker_version = %version,
                        "relay worker version changed"
                    );
                    reconnect.notify_one();
                    return;
                }
                _ => {}
            }
        }
        tokio::time::sleep(VERSION_POLL_INTERVAL).await;
    }
}

// 取一次当前部署版本；非 2xx / 网络失败 / 空 body 一律回 None（调用方据此不建立基线、不误触发）。
async fn fetch_worker_version(client: &reqwest::Client, url: &str) -> Option<String> {
    let response = client.get(url).send().await.ok()?;
    if !response.status().is_success() {
        return None;
    }
    let version = response.text().await.ok()?.trim().to_string();
    (!version.is_empty()).then_some(version)
}

async fn poll_end_to_end_health(
    worker_ws: String,
    uid: String,
    probe_token: String,
    reconnect: Arc<Notify>,
    connection_id: u64,
) {
    let Ok(url) = relay_health_url(&worker_ws, &uid) else {
        tracing::warn!(connection_id, "relay end-to-end probe URL is invalid");
        return;
    };
    let Ok(client) = reqwest::Client::builder()
        .timeout(END_TO_END_PROBE_TIMEOUT)
        // Do not let a relay redirect carry the Browser Bearer token anywhere
        // else. A redirect is treated as a failed probe and triggers recovery.
        .redirect(reqwest::redirect::Policy::none())
        .build()
    else {
        tracing::warn!(
            connection_id,
            "relay end-to-end probe client could not be created"
        );
        return;
    };
    let mut consecutive_failures = 0_u32;
    loop {
        tokio::time::sleep(END_TO_END_PROBE_INTERVAL).await;
        let mut request = client
            .get(url.clone())
            .header(reqwest::header::CACHE_CONTROL, "no-cache")
            .header("x-llm-wiki-watchdog", "1");
        if !probe_token.is_empty() {
            request = request.bearer_auth(&probe_token);
        }
        match request.send().await {
            Ok(response) if response.status().is_success() => {
                if consecutive_failures > 0 {
                    tracing::info!(
                        connection_id,
                        previous_failures = consecutive_failures,
                        "relay end-to-end health probe recovered"
                    );
                }
                consecutive_failures = 0;
            }
            Ok(response) => {
                consecutive_failures += 1;
                tracing::warn!(
                    connection_id,
                    status = response.status().as_u16(),
                    consecutive_failures,
                    "relay end-to-end health probe returned non-success"
                );
            }
            Err(err) => {
                consecutive_failures += 1;
                tracing::warn!(
                    connection_id,
                    timeout = err.is_timeout(),
                    connect = err.is_connect(),
                    consecutive_failures,
                    "relay end-to-end health probe request failed"
                );
            }
        }
        if consecutive_failures >= END_TO_END_FAILURE_THRESHOLD {
            reconnect.notify_one();
            return;
        }
    }
}

// 心跳：Worker 发 {t:"ping", ts}，连接器回 {t:"pong", ts}（原样回显 ts）。
// 不是心跳返回 None，调用方按请求继续处理。
fn handle_heartbeat(text: &str) -> Option<Message> {
    let ping = serde_json::from_str::<RelayPing>(text).ok()?;
    if ping.t != "ping" {
        return None;
    }
    let pong = serde_json::to_string(&RelayPong {
        t: "pong",
        ts: ping.ts,
    })
    .ok()?;
    Some(Message::Text(pong.into()))
}

// 处理单个转发请求；失败时回 err 帧，让 Worker 中止对应 stream，不悬挂。
async fn handle_request(
    config: &ConnectorConfig,
    client: &reqwest::Client,
    request: RelayRequest,
    tx: &mpsc::Sender<Message>,
) {
    let id = request.id.clone();
    if let Err(err) = forward_request(config, client, request, tx).await {
        let safe_error = safe_error_message(&err);
        if let Ok(frame) = serde_json::to_string(&RelayError {
            t: "err",
            id,
            msg: safe_error.clone(),
        }) {
            let _ = send_frame(tx, Message::Text(frame.into())).await;
        }
        tracing::warn!(error = %safe_error, "failed to handle relay request");
    }
}

async fn forward_request(
    config: &ConnectorConfig,
    client: &reqwest::Client,
    request: RelayRequest,
    tx: &mpsc::Sender<Message>,
) -> Result<u16> {
    let method = request.method.clone();
    let path = request.path.clone();
    let url = format!("{}{}", config.origin.trim_end_matches('/'), request.path);
    let mut builder = client.request(request.method.parse().unwrap_or(reqwest::Method::GET), url);
    let mut headers = HeaderMap::new();
    for (name, value) in &request.headers {
        if name.eq_ignore_ascii_case("host") || name.eq_ignore_ascii_case("content-length") {
            continue;
        }
        if let (Ok(name), Ok(value)) = (
            HeaderName::from_bytes(name.as_bytes()),
            HeaderValue::from_str(value),
        ) {
            headers.insert(name, value);
        }
    }
    headers.insert(
        HeaderName::from_static("x-llm-wiki-relay"),
        HeaderValue::from_static("1"),
    );
    builder = builder.headers(headers);
    if let Some(body) = request.body.as_deref()
        && !body.is_empty()
    {
        builder = builder.body(STANDARD.decode(body).unwrap_or_default());
    }
    let response = builder.send().await?;
    let status = response.status().as_u16();
    let headers = response
        .headers()
        .iter()
        .filter_map(|(name, value)| {
            value
                .to_str()
                .ok()
                .map(|value| (name.as_str().to_string(), value.to_string()))
        })
        .collect::<BTreeMap<_, _>>();

    send_frame(
        tx,
        Message::Text(
            serde_json::to_string(&RelayResponse {
                t: "res",
                id: request.id.clone(),
                status,
                headers,
            })?
            .into(),
        ),
    )
    .await?;

    let id_bytes = request.id.as_bytes();
    if id_bytes.len() != STREAM_ID_LEN {
        return Err(ConnectorError::InvalidStreamId { id: request.id });
    }
    let mut stream_id = [0_u8; STREAM_ID_LEN];
    stream_id.copy_from_slice(id_bytes);

    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk?;
        let mut frame = Vec::with_capacity(STREAM_ID_LEN + chunk.len());
        frame.extend_from_slice(&stream_id);
        frame.extend_from_slice(&chunk);
        send_frame(tx, Message::Binary(frame.into())).await?;
    }
    send_frame(
        tx,
        Message::Text(
            serde_json::to_string(&RelayEnd {
                t: "end",
                id: request.id,
            })?
            .into(),
        ),
    )
    .await?;
    tracing::info!(
        method,
        path = redact_token_query(&path),
        status,
        "relay request forwarded"
    );
    Ok(status)
}

// 把一帧投入有界发送队列；writer 已停（连接断开）则映射为 WriterClosed，让调用方尽快退出。
async fn send_frame(tx: &mpsc::Sender<Message>, message: Message) -> Result<()> {
    send_frame_with_timeout(tx, message, SEND_QUEUE_TIMEOUT).await
}

async fn send_frame_with_timeout(
    tx: &mpsc::Sender<Message>,
    message: Message,
    timeout: Duration,
) -> Result<()> {
    match tokio::time::timeout(timeout, tx.send(message)).await {
        Err(_) => Err(ConnectorError::SendQueueStalled {
            after_ms: timeout.as_millis() as u64,
        }),
        Ok(Err(_)) => Err(ConnectorError::WriterClosed),
        Ok(Ok(())) => Ok(()),
    }
}

fn provision_url(worker_ws: &str) -> Result<String> {
    worker_http_url(worker_ws, "/_provision")
}

// 把 worker_ws（ws/wss）换成 http/https 并指向某个顶层控制端点（/_provision、/_version）。
fn worker_http_url(worker_ws: &str, path: &str) -> Result<String> {
    let mut url = url::Url::parse(worker_ws)?;
    let scheme = match url.scheme() {
        "wss" => "https",
        "ws" => "http",
        other => other,
    }
    .to_string();
    url.set_scheme(&scheme).ok();
    url.set_path(path);
    url.set_query(None);
    Ok(url.to_string())
}

fn relay_health_url(worker_ws: &str, uid: &str) -> Result<url::Url> {
    let mut url = url::Url::parse(worker_ws)?;
    let scheme = match url.scheme() {
        "wss" => "https",
        "ws" => "http",
        other => other,
    }
    .to_string();
    url.set_scheme(&scheme).ok();
    url.set_query(None);
    url.set_fragment(None);
    if let Ok(mut segments) = url.path_segments_mut() {
        segments
            .clear()
            .push(uid)
            .push("api")
            .push("v1")
            .push("healthz");
    } else {
        return Err(ConnectorError::Url(url::ParseError::RelativeUrlWithoutBase));
    }
    Ok(url)
}

fn worker_endpoint_for_log(worker_ws: &str) -> String {
    let Ok(mut url) = url::Url::parse(worker_ws) else {
        return "<invalid-worker-url>".to_string();
    };
    let _ = url.set_username("");
    let _ = url.set_password(None);
    url.set_path("");
    url.set_query(None);
    url.set_fragment(None);
    url.to_string().trim_end_matches('/').to_string()
}

fn safe_error_message(err: &ConnectorError) -> String {
    redact_token_query(&err.to_string())
}

fn unix_time_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as u64)
        .unwrap_or(0)
}

#[derive(Debug, Deserialize)]
struct RelayRequest {
    t: String,
    id: String,
    method: String,
    path: String,
    #[serde(default)]
    headers: BTreeMap<String, String>,
    body: Option<String>,
}

#[derive(Debug, Serialize)]
struct RelayResponse {
    t: &'static str,
    id: String,
    status: u16,
    headers: BTreeMap<String, String>,
}

#[derive(Debug, Serialize)]
struct RelayEnd {
    t: &'static str,
    id: String,
}

#[derive(Debug, Serialize)]
struct RelayError {
    t: &'static str,
    id: String,
    msg: String,
}

#[derive(Debug, Deserialize)]
struct RelayPing {
    t: String,
    #[serde(default)]
    ts: Option<serde_json::Value>,
}

#[derive(Debug, Serialize)]
struct RelayPong {
    t: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    ts: Option<serde_json::Value>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_matches_existing_protocol_shape() {
        let key = derive_connector_key("dev-secret-change-me", "demo").unwrap();
        assert!(!key.contains('+'));
        assert!(!key.contains('/'));
        assert!(!key.contains('='));
    }

    #[test]
    fn worker_http_url_converts_scheme_and_path() {
        assert_eq!(
            worker_http_url("wss://wiki.htmlgo.to", "/_version").unwrap(),
            "https://wiki.htmlgo.to/_version"
        );
        assert_eq!(
            worker_http_url("ws://127.0.0.1:8787/", "/_provision").unwrap(),
            "http://127.0.0.1:8787/_provision"
        );
    }

    #[test]
    fn relay_health_url_uses_public_route_without_token_query() {
        assert_eq!(
            relay_health_url("wss://wiki.htmlgo.to/ws?token=secret", "demo uid")
                .unwrap()
                .as_str(),
            "https://wiki.htmlgo.to/demo%20uid/api/v1/healthz"
        );
    }

    #[test]
    fn token_query_is_redacted() {
        assert_eq!(
            redact_token_query("/api?x=1&token=secret&y=2"),
            "/api?x=1&token=<redacted>&y=2"
        );
    }

    #[test]
    fn worker_endpoint_log_value_drops_credentials_path_and_query() {
        assert_eq!(
            worker_endpoint_for_log("wss://user:secret@relay.example/ws?token=secret"),
            "wss://relay.example"
        );
    }

    #[test]
    fn connector_config_debug_never_prints_probe_token() {
        let config = ConnectorConfig {
            probe_token: "super-secret-token".to_string(),
            ..ConnectorConfig::default()
        };
        let debug = format!("{config:?}");
        assert!(!debug.contains("super-secret-token"));
        assert!(debug.contains("probe_token_configured: true"));
    }

    #[tokio::test]
    async fn send_queue_stall_times_out_instead_of_hanging() {
        let (tx, _rx) = mpsc::channel(1);
        tx.send(Message::Text("first".into())).await.unwrap();

        let err = send_frame_with_timeout(
            &tx,
            Message::Text("second".into()),
            Duration::from_millis(10),
        )
        .await
        .unwrap_err();

        assert!(matches!(
            err,
            ConnectorError::SendQueueStalled { after_ms: 10 }
        ));
    }
}
