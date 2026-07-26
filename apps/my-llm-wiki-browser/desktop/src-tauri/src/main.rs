// Windows 下必须声明 GUI 子系统，否则 exe 按控制台程序链接：启动即带一个常驻
// cmd 窗口，用户关掉窗口进程就没了。debug 构建保留控制台以便看日志。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::ErrorKind;
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use anyhow::{Context, Result};
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use llm_wiki_connector::{ConnectorConfig, ConnectorEvent, run_connector_with_events};
use llm_wiki_core::config::CoreConfig;
use llm_wiki_core::registry::load_registry;
use llm_wiki_core::{FullTextSearcher, IndexManager};
use llm_wiki_server::{AutostartControl, ServerConfig, ServerControl, serve};
use tauri::image::Image;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Emitter as _, Manager, WindowEvent};
use tracing_subscriber::EnvFilter;
use tracing_subscriber::fmt::writer::MakeWriterExt as _;
// macOS 专属：控制 Dock 图标与激活策略（Accessory=托盘常驻不占 Dock）。
// 其它平台没有这个概念，相关调用一并 cfg 掉。
#[cfg(target_os = "macos")]
use tauri::ActivationPolicy;
use tauri_plugin_autostart::{MacosLauncher, ManagerExt as _};

mod setup;
mod skills_watch;
mod update;
use skills_watch::SkillsWatch;
use update::UpdateManager;

const DEFAULT_PORT: u16 = 8800;

/// 本地服务端口，每进程解析一次：持久化配置 > `LLM_WIKI_PORT` > 默认 8800。
fn server_port() -> u16 {
    static PORT: OnceLock<u16> = OnceLock::new();
    *PORT.get_or_init(resolve_port)
}

fn resolve_port() -> u16 {
    if let Some(port) = load_persisted_port() {
        return port;
    }
    if let Ok(env) = std::env::var("LLM_WIKI_PORT")
        && let Ok(port) = env.trim().parse::<u16>()
        && port >= 1024
    {
        return port;
    }
    DEFAULT_PORT
}

fn local_base_url() -> String {
    format!("http://127.0.0.1:{}/", server_port())
}

fn port_pref_path() -> Option<PathBuf> {
    Some(llm_wiki_core::paths::connector_dir()?.join("server-port"))
}

fn load_persisted_port() -> Option<u16> {
    let content = std::fs::read_to_string(port_pref_path()?).ok()?;
    let port = content.trim().parse::<u16>().ok()?;
    (port >= 1024).then_some(port)
}

fn save_persisted_port(port: u16) -> std::io::Result<()> {
    let Some(path) = port_pref_path() else {
        return Err(std::io::Error::new(ErrorKind::NotFound, "home dir 不可用"));
    };
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(path, format!("{port}\n"))
}

fn main() {
    // Release GUI processes have stdout/stderr connected to /dev/null on macOS
    // and Windows. Persist synchronously because relay logs are low-volume and
    // the final pre-crash/watchdog event must survive an abrupt process stop.
    init_logging();
    tauri::Builder::default()
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            None,
        ))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            setup::setup_inspect,
            setup::setup_status,
            setup::setup_pick_wiki_directory,
            setup::setup_pick_install_directory,
            setup::setup_probe_install_root,
            setup::setup_apply,
            setup::setup_install_hosts,
            setup::setup_remove_host,
            setup::setup_start_repair,
            setup::setup_repair_status,
            setup::setup_stop_repair,
            setup::setup_open_wiki,
            setup::setup_open_wiki_directory,
            setup::setup_browser_settings_session,
            setup::setup_update,
            setup::setup_browser_update_status,
            setup::setup_restart,
            setup::setup_ensure_pack,
            setup::setup_start_pack_install,
            setup::setup_pack_install_status,
            setup::setup_update_cached,
            setup::setup_provider_config,
            setup::setup_save_provider_config,
        ])
        .setup(|app| {
            let local_url = local_base_url();
            let app_handle = app.handle().clone();
            let update_manager = UpdateManager::new(app.handle().clone());
            let online_urls = Arc::new(Mutex::new(OnlineUrls::default()));
            let tray = build_tray(app.handle(), local_url.clone(), online_urls.clone())?;
            #[cfg(target_os = "macos")]
            app.set_activation_policy(ActivationPolicy::Accessory);
            let relay_runtime = RelayRuntime::new(tray.clone(), online_urls.clone());
            let skills_watch = SkillsWatch::new(app.handle().clone(), tray.show_setup.clone());
            let browser_runtime = BrowserRuntime {
                started: Arc::new(AtomicBool::new(false)),
                online_urls,
                update_manager: update_manager.clone(),
                skills_watch: skills_watch.clone(),
            };

            tracing::info!("started {}", app.package_info().name);
            update_manager.spawn_periodic_check();
            skills_watch.spawn_periodic_check();
            app_handle.manage(DesktopState {
                local_url: local_url.clone(),
            });
            app_handle.manage(relay_runtime);
            app_handle.manage(update_manager);
            app_handle.manage(browser_runtime);
            app_handle.manage(setup::PackInstallManager::default());
            app_handle.manage(setup::RepairManager::default());
            match llm_wiki_setup::SetupCore::from_environment().and_then(|core| core.status()) {
                Ok(status) if status.state == llm_wiki_setup::SetupHealth::Ready => {
                    activate_browser(app.handle())?;
                }
                Ok(_) => show_setup_window(app.handle()),
                Err(error) => {
                    tracing::error!(error = ?error, "cannot read setup status");
                    show_setup_window(app.handle());
                }
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
                #[cfg(target_os = "macos")]
                let _ = window
                    .app_handle()
                    .set_activation_policy(ActivationPolicy::Accessory);
            }
        })
        .run(tauri::generate_context!())
        .expect("failed to run LLM-Wiki desktop app");
}

fn init_logging() {
    let filter = relay_log_filter();
    let Some(log_dir) = browser_log_dir() else {
        tracing_subscriber::fmt()
            .with_env_filter(filter)
            .with_ansi(false)
            .init();
        return;
    };
    if let Err(err) = std::fs::create_dir_all(&log_dir) {
        eprintln!("logging: cannot create {}: {err}", log_dir.display());
        tracing_subscriber::fmt()
            .with_env_filter(filter)
            .with_ansi(false)
            .init();
        return;
    }

    let appender = match tracing_appender::rolling::RollingFileAppender::builder()
        .rotation(tracing_appender::rolling::Rotation::DAILY)
        .filename_prefix("browser-relay")
        .filename_suffix("log")
        .max_log_files(7)
        .build(&log_dir)
    {
        Ok(appender) => appender,
        Err(err) => {
            eprintln!("logging: cannot open {}: {err}", log_dir.display());
            tracing_subscriber::fmt()
                .with_env_filter(filter)
                .with_ansi(false)
                .init();
            return;
        }
    };
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_ansi(false)
        .with_target(true)
        .with_writer(appender.and(std::io::stderr))
        .init();
    tracing::info!(
        log_dir = %log_dir.display(),
        retained_files = 7,
        "persistent browser logging initialized"
    );
}

/// Do not create the fixed suite anchor before Setup has chosen its real root.
///
/// A relocated Windows install must replace `%USERPROFILE%\.my-llm-wiki` with
/// a junction. Opening a log below that path first makes the directory both
/// non-empty and in use, so junction creation cannot succeed. Once Setup has
/// written state (including through an existing junction), logs return to the
/// normal suite location.
fn browser_log_dir() -> Option<PathBuf> {
    let suite_home = llm_wiki_core::paths::suite_home()?;
    browser_log_dir_for(&suite_home, dirs::data_local_dir())
}

fn browser_log_dir_for(
    suite_home: &std::path::Path,
    local_data: Option<PathBuf>,
) -> Option<PathBuf> {
    if suite_home.join("setup-state.json").is_file() {
        Some(suite_home.join("logs"))
    } else {
        local_data.map(|dir| dir.join("to.htmlgo.llm-wiki").join("setup-logs"))
    }
}

fn relay_log_filter() -> EnvFilter {
    // Preserve caller-provided filtering for dependencies, but relay health
    // snapshots are an operational contract and must not disappear under a
    // broad `RUST_LOG=warn` inherited from an agent/launcher process.
    ["llm_wiki_connector=info", "llm_wiki_desktop=info"]
        .into_iter()
        .fold(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
            |filter, directive| {
                filter.add_directive(directive.parse().expect("valid relay log directive"))
            },
        )
}

struct DesktopState {
    local_url: String,
}

#[derive(Clone)]
struct BrowserRuntime {
    started: Arc<AtomicBool>,
    online_urls: Arc<Mutex<OnlineUrls>>,
    update_manager: UpdateManager,
    skills_watch: SkillsWatch,
}

pub(crate) fn activate_browser(app: &tauri::AppHandle) -> Result<()> {
    start_browser(app)?;
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.hide();
    }
    show_setup_window(app);
    Ok(())
}

pub(crate) fn start_browser(app: &tauri::AppHandle) -> Result<()> {
    let runtime = app
        .try_state::<BrowserRuntime>()
        .ok_or_else(|| anyhow::anyhow!("Browser runtime is unavailable"))?;
    let first_start = runtime
        .started
        .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
        .is_ok();
    let mut secondary_instance = false;
    if first_start {
        let start =
            prepare_local_server(app, runtime.online_urls.clone()).map(|prepared| match prepared {
                Some((std_listener, mut server_config)) => {
                    server_config.control = Some(server_control(app.clone()));
                    tauri::async_runtime::spawn(async move {
                        let listener = match tokio::net::TcpListener::from_std(std_listener) {
                            Ok(listener) => listener,
                            Err(error) => {
                                tracing::error!(?error, "failed to create tokio listener");
                                return;
                            }
                        };
                        if let Err(error) = serve(listener, server_config).await {
                            tracing::error!(?error, "failed to start local wiki server");
                        }
                    });
                }
                None => secondary_instance = true,
            });
        if let Err(error) = start {
            runtime.started.store(false, Ordering::SeqCst);
            return Err(error);
        }
        if !secondary_instance {
            // These operations create files below the suite anchor. They must
            // happen only after Setup has created the default directory or
            // activated the relocated Windows junction.
            maybe_notify_first_launch(app);
            if let Some(relay) = app.try_state::<RelayRuntime>() {
                relay.enable_browser_controls();
                if load_relay_enabled() {
                    relay.start();
                }
            }
        }
    }
    Ok(())
}

pub(crate) fn open_local_wiki(app: &tauri::AppHandle) -> Result<()> {
    let state = app
        .try_state::<DesktopState>()
        .ok_or_else(|| anyhow::anyhow!("Browser desktop state is unavailable"))?;
    let url = local_url_with_token(&state.local_url, &auth_token());
    open::that(&url).context("open initialized Wiki")
}

/// 中继连接后的两个线上入口，严格分离（docs/19 §4.3）：
/// - `owner_url`：含 master token 的本人入口，只用于托盘的「打开/复制线上
///   WIKI（本人/自用）」与 `/api/v1/config/share`（agent 配置）。**绝不**用于构造分享链接。
/// - `public_base`：无凭证的 `https://<relay>/<uid>/`，是服务端拼分享链接的合法基座。
///   两者各自独立构造，`public_base` 不从 `owner_url` 做字符串删除得来。
#[derive(Default)]
struct OnlineUrls {
    owner_url: Option<String>,
    public_base: Option<String>,
}

#[derive(Clone)]
struct TrayState {
    relay_status: MenuItem<tauri::Wry>,
    show_setup: MenuItem<tauri::Wry>,
    open_local_wiki: MenuItem<tauri::Wry>,
    open_online_wiki: MenuItem<tauri::Wry>,
    copy_online_wiki: MenuItem<tauri::Wry>,
    relay_toggle: MenuItem<tauri::Wry>,
}

#[derive(Clone)]
struct RelayRuntime {
    tray: TrayState,
    online_urls: Arc<Mutex<OnlineUrls>>,
    handle: Arc<Mutex<Option<tauri::async_runtime::JoinHandle<()>>>>,
    // start 的代数：连接循环自然退出时只允许清理「自己那一代」的句柄，
    // 防止与 stop→start 竞态时误清新任务的句柄。
    generation: Arc<AtomicU64>,
}

impl RelayRuntime {
    fn new(tray: TrayState, online_urls: Arc<Mutex<OnlineUrls>>) -> Self {
        Self {
            tray,
            online_urls,
            handle: Arc::new(Mutex::new(None)),
            generation: Arc::new(AtomicU64::new(0)),
        }
    }

    fn enable_browser_controls(&self) {
        let _ = self.tray.open_local_wiki.set_enabled(true);
        let _ = self.tray.relay_toggle.set_enabled(true);
    }

    fn start(&self) {
        let Ok(mut handle) = self.handle.lock() else {
            return;
        };
        if handle.is_some() {
            return;
        }

        // connector_config() creates the persistent auth token, so resolve it
        // lazily only after Setup has activated the suite anchor.
        let config = connector_config();
        let worker_ws = config.worker_ws.clone();
        let tray = self.tray.clone();
        let online_urls = self.online_urls.clone();
        let _ = self.tray.relay_toggle.set_text("断开中继服务");
        let _ = self.tray.relay_status.set_text("🔴 中继：重连中");
        let _ = self.tray.open_online_wiki.set_enabled(false);
        let _ = self.tray.copy_online_wiki.set_enabled(false);

        let handle_slot = self.handle.clone();
        let generation = self.generation.clone();
        let my_generation = generation.fetch_add(1, Ordering::SeqCst) + 1;
        *handle = Some(tauri::async_runtime::spawn(async move {
            if let Err(err) = run_connector_with_events(config, move |event| {
                update_tray_relay(&tray, online_urls.clone(), &worker_ws, event);
            })
            .await
            {
                tracing::error!(error = ?err, "relay connector stopped");
            }
            // 连接循环整体退出（如被其他连接器接管）时清掉句柄，
            // 托盘再点“连接中继服务”可直接重新 start，而不是先经过一次无效的 stop。
            // 仅当没有更新一代的 start 时才清，避免误清新任务的句柄。
            // 代数检查须在持锁后做：start() 持同一把锁递增代数，两者才互斥。
            if let Ok(mut slot) = handle_slot.lock()
                && generation.load(Ordering::SeqCst) == my_generation
            {
                *slot = None;
            }
        }));
    }

    fn stop(&self) {
        if let Ok(mut handle) = self.handle.lock()
            && let Some(handle) = handle.take()
        {
            handle.abort();
        }
        if let Ok(mut guard) = self.online_urls.lock() {
            *guard = OnlineUrls::default();
        }
        let _ = self.tray.relay_status.set_text("⚪ 中继：已关闭");
        let _ = self.tray.open_online_wiki.set_enabled(false);
        let _ = self.tray.copy_online_wiki.set_enabled(false);
        let _ = self.tray.relay_toggle.set_text("连接中继服务");
    }

    fn toggle(&self) {
        let running = self
            .handle
            .lock()
            .map(|handle| handle.is_some())
            .unwrap_or(false);
        if running {
            self.stop();
            save_relay_enabled(false);
        } else {
            self.start();
            save_relay_enabled(true);
        }
    }
}

/// 首次启动弹一条系统通知：应用没有主窗口、常驻托盘，Windows 上托盘图标还常被
/// 折叠进任务栏溢出区，不提示的话用户对"已启动"完全无感知。只弹一次——弹成功
/// 才落盘标记，失败则留到下次启动再试。
fn maybe_notify_first_launch(app: &tauri::AppHandle) {
    use tauri_plugin_notification::NotificationExt;

    let Some(marker) =
        llm_wiki_core::paths::connector_dir().map(|dir| dir.join("first-launch-notified"))
    else {
        return;
    };
    if marker.exists() {
        return;
    }

    #[cfg(target_os = "windows")]
    let body =
        "已在后台运行。点击任务栏右下角的托盘图标（可能折叠在 ∧ 溢出区里）可打开本地 WIKI 和设置。";
    #[cfg(target_os = "macos")]
    let body = "已在后台运行。点击菜单栏右上角的托盘图标可打开本地 WIKI 和设置。";
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    let body = "已在后台运行。点击系统托盘图标可打开本地 WIKI 和设置。";

    if let Err(err) = app
        .notification()
        .builder()
        .title("My LLM Wiki Browser 已启动")
        .body(body)
        .show()
    {
        tracing::warn!(error = ?err, "failed to show first-launch notification");
        return;
    }
    if let Some(parent) = marker.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Err(err) = std::fs::write(&marker, "1\n") {
        tracing::warn!(error = ?err, "failed to persist first-launch marker");
    }
}

fn relay_pref_path() -> Option<PathBuf> {
    Some(llm_wiki_core::paths::connector_dir()?.join("relay-enabled"))
}

/// Whether the relay should auto-connect on launch. Defaults to `false` so the
/// first launch stays offline until the user opts in; once toggled, the choice
/// is persisted and restored on the next launch.
fn load_relay_enabled() -> bool {
    relay_pref_path()
        .and_then(|path| std::fs::read_to_string(path).ok())
        .is_some_and(|content| content.trim() == "1")
}

fn save_relay_enabled(enabled: bool) {
    let Some(path) = relay_pref_path() else {
        return;
    };
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Err(err) = std::fs::write(&path, if enabled { "1\n" } else { "0\n" }) {
        tracing::warn!(error = ?err, "failed to persist relay preference");
    }
}

fn prepare_local_server(
    app: &tauri::AppHandle,
    online_urls: Arc<Mutex<OnlineUrls>>,
) -> Result<Option<(std::net::TcpListener, ServerConfig)>> {
    let config = CoreConfig::from_env();
    let registry = load_registry(&config).context("load wiki registry")?;
    let manager = IndexManager::build(registry.into_values()).context("build wiki index")?;
    let searcher = FullTextSearcher::open(config.cache_dir.join("fts5.sqlite3"))
        .context("open full-text search index")?;
    for idx in manager.wikis.values() {
        searcher
            .reindex_wiki(&idx.entry.key, &idx.search_docs())
            .with_context(|| format!("reindex wiki {}", idx.entry.key))?;
    }

    let frontend_dist = frontend_dist_path(app).filter(|path| path.is_dir());
    let port = server_port();
    let addr = SocketAddr::new(
        std::env::var("HOST")
            .ok()
            .and_then(|host| host.parse::<IpAddr>().ok())
            .unwrap_or(IpAddr::V4(Ipv4Addr::LOCALHOST)),
        port,
    );
    let std_listener = match std::net::TcpListener::bind(addr) {
        Ok(listener) => listener,
        Err(err) if err.kind() == ErrorKind::AddrInUse => {
            tracing::warn!(
                address = %addr,
                "local wiki server port is already in use; assuming another instance is running"
            );
            return Ok(None);
        }
        Err(err) => return Err(err).context("bind local wiki server"),
    };
    std_listener
        .set_nonblocking(true)
        .context("set local wiki server listener nonblocking")?;

    Ok(Some((
        std_listener,
        ServerConfig {
            frontend_dist,
            index_manager: manager,
            searcher,
            auth_token: auth_token(),
            watch_wikis: true,
            registry_config: Some(config),
            port,
            control: None,
            owner_online_url: Some(Arc::new({
                let urls = online_urls.clone();
                move || urls.lock().ok().and_then(|guard| guard.owner_url.clone())
            })),
            public_base_url: Some(Arc::new({
                let urls = online_urls.clone();
                move || urls.lock().ok().and_then(|guard| guard.public_base.clone())
            })),
            grants_path: llm_wiki_core::paths::connector_dir().map(|dir| dir.join("grants.json")),
        },
    )))
}

/// 注入给 HTTP 层的 Browser 运行期控制。Skills 与工具链由 Tauri Setup Core 管理，
/// 不再通过本地 HTTP 设置页维护。
fn server_control(app: tauri::AppHandle) -> ServerControl {
    let restart_app = app.clone();
    let query_app = app.clone();
    ServerControl {
        persist_port: Arc::new(save_persisted_port),
        restart: Arc::new(move || {
            let app = restart_app.clone();
            // 延迟少许，让 HTTP 响应先回到设置页再重启。
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(Duration::from_millis(300)).await;
                app.restart();
            });
        }),
        autostart: Some(AutostartControl {
            is_enabled: Arc::new(move || {
                query_app
                    .autolaunch()
                    .is_enabled()
                    .map_err(|err| err.to_string())
            }),
            set_enabled: Arc::new(move |enabled| {
                let manager = app.autolaunch();
                if enabled {
                    // auto-launch 不会自建 ~/Library/LaunchAgents；新建的 macOS
                    // 账户可能没有该目录，缺失时 enable 会直接失败。
                    #[cfg(target_os = "macos")]
                    if let Some(home) = dirs::home_dir() {
                        let _ = std::fs::create_dir_all(home.join("Library/LaunchAgents"));
                    }
                    manager.enable()
                } else {
                    manager.disable()
                }
                .map_err(|err| err.to_string())
            }),
        }),
    }
}

/// 前端静态资源目录解析顺序：
/// 1. `LLM_WIKI_WEB_FRONTEND` 环境变量（显式覆盖）；
/// 2. 打进 .app 的随包资源（`$RESOURCE/frontend`）——发布形态，自洽且不会与仓库 dist 版本错位；
/// 3. 开发态回退到仓库内的 `frontend/dist`。
fn frontend_dist_path(app: &tauri::AppHandle) -> Option<PathBuf> {
    if let Some(custom) = std::env::var_os("LLM_WIKI_WEB_FRONTEND") {
        return Some(PathBuf::from(custom));
    }
    if let Ok(bundled) = app
        .path()
        .resolve("frontend", tauri::path::BaseDirectory::Resource)
        && bundled.is_dir()
    {
        return Some(bundled);
    }
    Some(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../frontend/dist"))
}

fn connector_config() -> ConnectorConfig {
    let mut config = ConnectorConfig::default();
    if let Ok(worker_ws) = std::env::var("WORKER_WS") {
        config.worker_ws = worker_ws;
    }
    config.origin = local_base_url().trim_end_matches('/').to_string();
    config.probe_token = auth_token();
    if let Some(dir) = llm_wiki_core::paths::connector_dir() {
        config.identity_file = dir.join("identity.json");
    }
    if let Ok(identity_file) = std::env::var("IDENTITY_FILE") {
        config.identity_file = PathBuf::from(identity_file);
    }
    config
}

/// Resolve the shared auth token, computed once per process.
///
/// Order: `LLM_WIKI_WEB_TOKEN` env override (dev / explicit), otherwise a
/// locally-persisted token under `~/.my-llm-wiki/connector/token`, generated on
/// first launch. A GUI app started from Finder inherits no shell env, so the
/// persisted file is what makes the token non-empty in production — without it
/// the server, the settings window, and the tray links would all be tokenless.
fn auth_token() -> String {
    static TOKEN: OnceLock<String> = OnceLock::new();
    TOKEN.get_or_init(resolve_auth_token).clone()
}

fn resolve_auth_token() -> String {
    if let Ok(env) = std::env::var("LLM_WIKI_WEB_TOKEN") {
        let env = env.trim();
        if !env.is_empty() {
            return env.to_string();
        }
    }
    load_or_create_token().unwrap_or_default()
}

fn load_or_create_token() -> Option<String> {
    let path = llm_wiki_core::paths::connector_dir()?.join("token");
    if let Ok(existing) = std::fs::read_to_string(&path) {
        let existing = existing.trim();
        if !existing.is_empty() {
            return Some(existing.to_string());
        }
    }

    let token = generate_token();
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Err(err) = write_token_file(&path, &token) {
        tracing::warn!(error = ?err, "failed to persist auth token; using in-memory token for this session");
    }
    Some(token)
}

fn generate_token() -> String {
    let mut bytes = [0u8; 18];
    getrandom::fill(&mut bytes).expect("system RNG unavailable");
    URL_SAFE_NO_PAD.encode(bytes)
}

fn write_token_file(path: &std::path::Path, token: &str) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let mut file = std::fs::OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .mode(0o600)
            .open(path)?;
        file.write_all(token.as_bytes())?;
        file.write_all(b"\n")?;
        Ok(())
    }
    #[cfg(not(unix))]
    {
        std::fs::write(path, format!("{token}\n"))
    }
}

fn build_tray(
    app: &tauri::AppHandle,
    local_url: String,
    online_urls: Arc<Mutex<OnlineUrls>>,
) -> Result<TrayState> {
    let relay_status =
        MenuItem::with_id(app, "relay_status", "⚪ 中继：已关闭", false, None::<&str>)?;
    let status = MenuItem::with_id(
        app,
        "status",
        format!("本地服务: {local_url}"),
        false,
        None::<&str>,
    )?;
    let show_setup = MenuItem::with_id(app, "show_setup", "设置…", true, None::<&str>)?;
    let open_local_wiki =
        MenuItem::with_id(app, "open_local_wiki", "打开本地 WIKI", false, None::<&str>)?;
    // 本人线上入口含 master token；「复制」明确标记为自用，不可作为分享链接。
    // 对外分享仍须走 web UI 确认面板，生成独立、可撤销的访客凭证。
    let open_online_wiki = MenuItem::with_id(
        app,
        "open_online_wiki",
        "打开线上 WIKI（本人）",
        false,
        None::<&str>,
    )?;
    let copy_online_wiki = MenuItem::with_id(
        app,
        "copy_online_wiki",
        "复制线上 WIKI 链接（自用）",
        false,
        None::<&str>,
    )?;
    let relay_toggle = MenuItem::with_id(app, "relay_toggle", "连接中继服务", false, None::<&str>)?;
    let check_update = MenuItem::with_id(app, "check_update", "检查更新…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(
        app,
        &[
            &relay_status,
            &status,
            &open_local_wiki,
            &open_online_wiki,
            &copy_online_wiki,
            &relay_toggle,
            &show_setup,
            &check_update,
            &separator,
            &quit,
        ],
    )?;
    // 模板图标:macOS 只取 alpha 通道着色,菜单栏深浅色下自动适配
    let icon = Image::from_bytes(include_bytes!("../icons/tray.png"))?;

    TrayIconBuilder::with_id("main")
        .tooltip("LLM-Wiki")
        .icon(icon)
        .icon_as_template(true)
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(move |app, event| match event.id().as_ref() {
            "show_setup" => {
                check_updates_from_settings(app);
                show_setup_window(app);
            }
            "open_local_wiki" => {
                if let Some(state) = app.try_state::<DesktopState>() {
                    let url = local_url_with_token(&state.local_url, &auth_token());
                    let _ = open::that(&url);
                }
            }
            "open_online_wiki" => {
                if let Ok(guard) = online_urls.lock()
                    && let Some(url) = guard.owner_url.as_ref()
                {
                    let _ = open::that(url);
                }
            }
            "copy_online_wiki" => {
                let owner_url = online_urls
                    .lock()
                    .ok()
                    .and_then(|guard| guard.owner_url.clone());
                copy_owner_url_to_clipboard(app, owner_url);
            }
            "relay_toggle" => {
                if let Some(relay_runtime) = app.try_state::<RelayRuntime>() {
                    relay_runtime.toggle();
                }
            }
            // 触发一次检查并打开设置页——结果在设置页的更新面板里呈现。
            "check_update" => {
                check_updates_from_settings(app);
                show_setup_window(app);
                let _ = app.emit("settings-navigate", "skills");
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;

    Ok(TrayState {
        relay_status,
        show_setup,
        open_local_wiki,
        open_online_wiki,
        copy_online_wiki,
        relay_toggle,
    })
}

/// A settings visit is a natural, user-initiated opportunity to refresh both
/// release channels. The checks stay asynchronous so opening the window never
/// waits on the network; the frontend receives the start signal immediately
/// and the Skills result when that check completes.
fn check_updates_from_settings(app: &tauri::AppHandle) {
    if let Some(runtime) = app.try_state::<BrowserRuntime>() {
        runtime.update_manager.check();
        runtime.skills_watch.check();
    }
    let _ = app.emit("settings-update-check-started", ());
}

fn copy_owner_url_to_clipboard(app: &tauri::AppHandle, owner_url: Option<String>) {
    use tauri_plugin_notification::NotificationExt;

    let result = owner_url
        .ok_or_else(|| anyhow::anyhow!("中继未连接，线上 WIKI 链接尚未就绪"))
        .and_then(|url| {
            let mut clipboard = arboard::Clipboard::new().context("无法访问系统剪贴板")?;
            clipboard.set_text(url).context("无法写入系统剪贴板")
        });

    let (title, body) = match result {
        Ok(()) => (
            "线上 WIKI 链接已复制",
            "该链接含本人访问凭证，请仅限自用，不要转发。",
        ),
        Err(err) => {
            tracing::warn!(error = ?err, "failed to copy owner online wiki URL");
            (
                "复制线上 WIKI 链接失败",
                "请确认中继已连接，且应用可访问系统剪贴板后重试。",
            )
        }
    };

    if let Err(err) = app.notification().builder().title(title).body(body).show() {
        tracing::warn!(error = ?err, "failed to show clipboard result notification");
    }
}

fn show_setup_window(app: &tauri::AppHandle) {
    #[cfg(target_os = "macos")]
    let _ = app.set_activation_policy(ActivationPolicy::Regular);
    if let Some(window) = app.get_webview_window("setup") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn update_tray_relay(
    tray: &TrayState,
    online_urls: Arc<Mutex<OnlineUrls>>,
    worker_ws: &str,
    event: ConnectorEvent,
) {
    match event {
        ConnectorEvent::Connected { uid } => {
            // 两个入口各自独立构造：owner 带 master token，public 无凭证。
            let owner_url = online_url(worker_ws, &uid, &auth_token());
            let public_base = online_base(worker_ws, &uid);
            let enabled = owner_url.is_some();
            if let Ok(mut guard) = online_urls.lock() {
                guard.owner_url = owner_url;
                guard.public_base = public_base;
            }
            let _ = tray
                .relay_status
                .set_text(format!("🟢 中继：已连接 ({uid})"));
            let _ = tray.open_online_wiki.set_enabled(enabled);
            let _ = tray.copy_online_wiki.set_enabled(enabled);
        }
        ConnectorEvent::Disconnected => {
            if let Ok(mut guard) = online_urls.lock() {
                *guard = OnlineUrls::default();
            }
            let _ = tray.relay_status.set_text("🔴 中继：未连接");
            let _ = tray.open_online_wiki.set_enabled(false);
            let _ = tray.copy_online_wiki.set_enabled(false);
        }
        ConnectorEvent::Retrying { .. } => {
            let _ = tray.relay_status.set_text("🔴 中继：重连中");
            let _ = tray.open_online_wiki.set_enabled(false);
            let _ = tray.copy_online_wiki.set_enabled(false);
        }
        ConnectorEvent::Replaced => {
            if let Ok(mut guard) = online_urls.lock() {
                *guard = OnlineUrls::default();
            }
            let _ = tray.relay_status.set_text("⚪ 中继：已被其他设备接管");
            let _ = tray.open_online_wiki.set_enabled(false);
            let _ = tray.copy_online_wiki.set_enabled(false);
            let _ = tray.relay_toggle.set_text("连接中继服务");
        }
        ConnectorEvent::IdentityLoaded { .. } | ConnectorEvent::RequestFinished { .. } => {}
    }
}

fn local_url_with_token(local_url: &str, auth_token: &str) -> String {
    if auth_token.is_empty() {
        return local_url.to_string();
    }
    match url::Url::parse(local_url) {
        Ok(mut url) => {
            url.query_pairs_mut().append_pair("token", auth_token);
            url.to_string()
        }
        Err(_) => local_url.to_string(),
    }
}

/// 无凭证的线上基座 `https://<relay>/<uid>/`（ws→http / wss→https，去 query）。
/// 分享链接的合法基座即由它构造，绝不含 token。
fn online_base(worker_ws: &str, uid: &str) -> Option<String> {
    let mut url = url::Url::parse(worker_ws).ok()?;
    match url.scheme() {
        "wss" => url.set_scheme("https").ok()?,
        "ws" => url.set_scheme("http").ok()?,
        _ => {}
    }
    url.set_path(&format!("/{uid}/"));
    url.set_query(None);
    Some(url.to_string())
}

/// 本人线上入口 = 无凭证基座 + master token。用 url 解析后 append，不做字符串拼接。
fn online_url(worker_ws: &str, uid: &str, auth_token: &str) -> Option<String> {
    let base = online_base(worker_ws, uid)?;
    if auth_token.is_empty() {
        return Some(base);
    }
    let mut url = url::Url::parse(&base).ok()?;
    url.query_pairs_mut().append_pair("token", auth_token);
    Some(url.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn relay_targets_stay_visible_in_persistent_logs() {
        let filter = relay_log_filter().to_string();
        assert!(filter.contains("llm_wiki_connector=info"));
        assert!(filter.contains("llm_wiki_desktop=info"));
    }

    #[test]
    fn first_run_logs_do_not_create_the_suite_anchor() {
        let temp = tempfile::tempdir().expect("temp dir");
        let suite_home = temp.path().join(".my-llm-wiki");
        let local_data = temp.path().join("local-data");

        let selected = browser_log_dir_for(&suite_home, Some(local_data.clone()));

        assert_eq!(
            selected,
            Some(local_data.join("to.htmlgo.llm-wiki/setup-logs"))
        );
        assert!(!suite_home.exists());
    }

    #[test]
    fn configured_install_logs_through_the_suite_anchor() {
        let temp = tempfile::tempdir().expect("temp dir");
        let suite_home = temp.path().join(".my-llm-wiki");
        std::fs::create_dir_all(&suite_home).expect("suite home");
        std::fs::write(suite_home.join("setup-state.json"), b"{}").expect("setup state");

        let selected = browser_log_dir_for(&suite_home, Some(temp.path().join("local-data")));

        assert_eq!(selected, Some(suite_home.join("logs")));
    }

    #[test]
    fn local_url_keeps_plain_when_token_empty() {
        assert_eq!(
            local_url_with_token("http://127.0.0.1:8800/", ""),
            "http://127.0.0.1:8800/"
        );
    }

    #[test]
    fn local_url_appends_token_when_present() {
        assert_eq!(
            local_url_with_token("http://127.0.0.1:8800/", "secret"),
            "http://127.0.0.1:8800/?token=secret"
        );
    }

    #[test]
    fn local_url_percent_encodes_token() {
        assert_eq!(
            local_url_with_token("http://127.0.0.1:8800/", "a b/c"),
            "http://127.0.0.1:8800/?token=a+b%2Fc"
        );
    }

    #[test]
    fn online_url_converts_scheme_and_appends_token() {
        assert_eq!(
            online_url("wss://relay.example/ws", "abc123", "secret").as_deref(),
            Some("https://relay.example/abc123/?token=secret")
        );
    }

    #[test]
    fn generated_token_is_url_safe_and_unique() {
        let a = generate_token();
        let b = generate_token();
        assert_ne!(a, b);
        assert_eq!(a.len(), 24);
        assert!(
            a.chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        );
    }

    #[test]
    fn online_url_omits_token_when_empty() {
        assert_eq!(
            online_url("wss://relay.example/ws", "abc123", "").as_deref(),
            Some("https://relay.example/abc123/")
        );
    }
}
