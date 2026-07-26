//! 应用自动更新：包一层 tauri-plugin-updater 的状态机，供内嵌 Setup 窗口轮询。
//!
//! 检查/下载都是异步长任务，Setup GUI 只读这里的状态快照。签名验证由 updater 插件强制
//! （minisign 公钥烧在 tauri.conf.json），清单/下载通道被劫持也装不进伪造包。

use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Serialize;
use tauri_plugin_updater::UpdaterExt as _;

/// 启动后首次自动检查的延迟：等本地服务/托盘就绪，也避免拖慢启动路径。
const FIRST_CHECK_DELAY: Duration = Duration::from_secs(10);
/// 周期检查间隔。只改状态与托盘文案，不自动下载。
const CHECK_INTERVAL: Duration = Duration::from_secs(24 * 60 * 60);

#[derive(Debug, Clone, Serialize)]
pub struct UpdateStatus {
    pub current_version: String,
    pub state: String,
    pub latest_version: Option<String>,
    pub notes: Option<String>,
    pub downloaded: Option<u64>,
    pub total: Option<u64>,
    pub error: Option<String>,
}

/// 内部状态机。`Available` 不缓存 Update 句柄——install 时重新 check 一次拿新鲜
/// 句柄（一次网络往返，换来状态可全量序列化、无生命周期纠缠）。
#[derive(Debug, Clone)]
enum State {
    Idle,
    Checking,
    UpToDate,
    Available {
        version: String,
        notes: Option<String>,
    },
    Downloading {
        version: String,
        downloaded: u64,
        total: Option<u64>,
    },
    Installing {
        version: String,
    },
    ReadyToRestart {
        version: String,
    },
    /// 便携版（exe 旁有 portable.flag）：没有安装器，只查版本、提示手动下载。
    Portable {
        latest: Option<String>,
        notes: Option<String>,
    },
    Error {
        message: String,
    },
}

#[derive(Clone)]
pub struct UpdateManager {
    app: tauri::AppHandle,
    state: Arc<Mutex<State>>,
    portable: bool,
}

impl UpdateManager {
    pub fn new(app: tauri::AppHandle) -> Self {
        Self {
            app,
            state: Arc::new(Mutex::new(State::Idle)),
            portable: is_portable(),
        }
    }

    /// 启动定期自动检查（首次延迟 + 24h 周期）。
    pub fn spawn_periodic_check(&self) {
        let manager = self.clone();
        tauri::async_runtime::spawn(async move {
            tokio::time::sleep(FIRST_CHECK_DELAY).await;
            loop {
                manager.check();
                tokio::time::sleep(CHECK_INTERVAL).await;
            }
        });
    }

    pub fn status(&self) -> UpdateStatus {
        let current_version = self.app.package_info().version.to_string();
        let state = self.state.lock().unwrap().clone();
        let mut status = UpdateStatus {
            current_version,
            state: String::new(),
            latest_version: None,
            notes: None,
            downloaded: None,
            total: None,
            error: None,
        };
        match state {
            State::Idle => status.state = "idle".into(),
            State::Checking => status.state = "checking".into(),
            State::UpToDate => status.state = "up-to-date".into(),
            State::Available { version, notes } => {
                status.state = "available".into();
                status.latest_version = Some(version);
                status.notes = notes;
            }
            State::Downloading {
                version,
                downloaded,
                total,
            } => {
                status.state = "downloading".into();
                status.latest_version = Some(version);
                status.downloaded = Some(downloaded);
                status.total = total;
            }
            State::Installing { version } => {
                status.state = "installing".into();
                status.latest_version = Some(version);
            }
            State::ReadyToRestart { version } => {
                status.state = "ready-to-restart".into();
                status.latest_version = Some(version);
            }
            State::Portable { latest, notes } => {
                status.state = "portable".into();
                status.latest_version = latest;
                status.notes = notes;
            }
            State::Error { message } => {
                status.state = "error".into();
                status.error = Some(message);
            }
        }
        status
    }

    /// 触发后台检查。检查/下载进行中时忽略（幂等），避免并发任务互踩状态。
    pub fn check(&self) {
        {
            let mut state = self.state.lock().unwrap();
            if matches!(
                *state,
                State::Checking
                    | State::Downloading { .. }
                    | State::Installing { .. }
                    | State::ReadyToRestart { .. }
            ) {
                return;
            }
            *state = State::Checking;
        }
        let manager = self.clone();
        tauri::async_runtime::spawn(async move {
            let next = match manager.app.updater() {
                Ok(updater) => match updater.check().await {
                    Ok(Some(update)) if manager.portable => State::Portable {
                        latest: Some(update.version.clone()),
                        notes: update.body.clone(),
                    },
                    Ok(Some(update)) => State::Available {
                        version: update.version.clone(),
                        notes: update.body.clone(),
                    },
                    Ok(None) if manager.portable => State::Portable {
                        latest: None,
                        notes: None,
                    },
                    Ok(None) => State::UpToDate,
                    Err(err) => {
                        tracing::warn!(error = ?err, "update check failed");
                        State::Error {
                            message: err.to_string(),
                        }
                    }
                },
                Err(err) => State::Error {
                    message: err.to_string(),
                },
            };
            *manager.state.lock().unwrap() = next;
        });
    }

    /// 检查并安装最新 Browser。Setup GUI 可以直接从 idle/error 状态发起，
    /// 不需要先走一轮 HTTP 检查接口；进行中与已就绪状态保持幂等。
    pub fn install_latest(&self) -> Result<(), String> {
        {
            let mut state = self.state.lock().unwrap();
            match &*state {
                State::Portable { .. } => {
                    return Err("便携版不支持原地更新，请手动下载新版本".into());
                }
                State::Checking
                | State::Downloading { .. }
                | State::Installing { .. }
                | State::ReadyToRestart { .. } => {
                    return Ok(());
                }
                State::Idle | State::UpToDate | State::Available { .. } | State::Error { .. } => {
                    *state = State::Checking;
                }
            }
        }
        let manager = self.clone();
        tauri::async_runtime::spawn(async move {
            let next = manager.download_and_install().await;
            *manager.state.lock().unwrap() = next;
        });
        Ok(())
    }

    async fn download_and_install(&self) -> State {
        // 重新 check 拿 Update 句柄（Available 态不缓存句柄，见 State 注释）。
        let update = match self.app.updater() {
            Ok(updater) => match updater.check().await {
                Ok(Some(update)) => update,
                Ok(None) => return State::UpToDate,
                Err(err) => {
                    return State::Error {
                        message: err.to_string(),
                    };
                }
            },
            Err(err) => {
                return State::Error {
                    message: err.to_string(),
                };
            }
        };
        let version = update.version.clone();
        *self.state.lock().unwrap() = State::Downloading {
            version: version.clone(),
            downloaded: 0,
            total: None,
        };
        let progress_state = self.state.clone();
        let progress_version = version.clone();
        let install_state = self.state.clone();
        let install_version = version.clone();
        let mut downloaded: u64 = 0;
        let result = update
            .download_and_install(
                move |chunk, total| {
                    downloaded += chunk as u64;
                    *progress_state.lock().unwrap() = State::Downloading {
                        version: progress_version.clone(),
                        downloaded,
                        total,
                    };
                },
                move || {
                    *install_state.lock().unwrap() = State::Installing {
                        version: install_version,
                    };
                },
            )
            .await;
        match result {
            Ok(()) => State::ReadyToRestart { version },
            Err(err) => {
                tracing::error!(error = ?err, "update download/install failed");
                State::Error {
                    message: err.to_string(),
                }
            }
        }
    }
}

/// 便携版标记：release 的 portable zip 在 exe 旁放了 portable.flag。
fn is_portable() -> bool {
    std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|dir| dir.join("portable.flag").exists()))
        .unwrap_or(false)
}
