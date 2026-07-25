use llm_wiki_setup::{
    InstallRootProbe, ProviderConfig, SetupCore, SetupError, SetupInspection, SetupProgress,
    SetupRequest, SetupResult, UpdateResult,
};
use serde::Serialize;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter as _, Manager as _, State, WebviewWindow};
use tauri_plugin_dialog::DialogExt as _;
use tauri_plugin_notification::NotificationExt as _;

use crate::update::UpdateStatus;

#[derive(Debug, Clone, Serialize)]
pub(crate) struct PackInstallJob {
    id: String,
    state: String,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    detail_percent: Option<u8>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

impl PackInstallJob {
    fn idle(id: &str) -> Self {
        Self {
            id: id.to_owned(),
            state: "idle".into(),
            message: "尚未开始".into(),
            detail_percent: None,
            error: None,
        }
    }

    fn running(id: &str) -> Self {
        Self {
            id: id.to_owned(),
            state: "running".into(),
            message: format!("正在启动 {id} 后台安装"),
            detail_percent: None,
            error: None,
        }
    }
}

/// Long pack downloads belong to the Browser process rather than to a single
/// WebView invoke. Hiding the Setup window or opening the Wiki therefore does
/// not cancel the task, and reopening Setup can recover the latest snapshot.
#[derive(Clone, Default)]
pub(crate) struct PackInstallManager {
    jobs: Arc<Mutex<BTreeMap<String, PackInstallJob>>>,
}

impl PackInstallManager {
    fn snapshot(&self, id: &str) -> PackInstallJob {
        self.jobs
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .get(id)
            .cloned()
            .unwrap_or_else(|| PackInstallJob::idle(id))
    }

    fn replace(&self, id: &str, job: PackInstallJob) {
        self.jobs
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert(id.to_owned(), job);
    }

    fn update_progress(&self, id: &str, progress: SetupProgress) {
        self.replace(
            id,
            PackInstallJob {
                id: id.to_owned(),
                state: "running".into(),
                message: progress.message,
                detail_percent: progress.detail_percent,
                error: None,
            },
        );
    }

    fn start(&self, app: AppHandle, id: String) -> PackInstallJob {
        let starting = {
            let mut jobs = self
                .jobs
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            if let Some(current) = jobs.get(&id)
                && current.state == "running"
            {
                return current.clone();
            }
            let starting = PackInstallJob::running(&id);
            jobs.insert(id.clone(), starting.clone());
            starting
        };

        let manager = self.clone();
        tauri::async_runtime::spawn(async move {
            let task_manager = manager.clone();
            let task_id = id.clone();
            let result = tauri::async_runtime::spawn_blocking(move || {
                let core = SetupCore::from_environment()?;
                if task_id == "asr-zh" {
                    core.prepare_asr_zh_with_progress(|progress| {
                        task_manager.update_progress(&task_id, progress)
                    })
                } else {
                    core.ensure_pack_with_progress(&task_id, |progress| {
                        task_manager.update_progress(&task_id, progress)
                    })
                }
            })
            .await;

            match result {
                Ok(Ok(_)) => {
                    manager.replace(
                        &id,
                        PackInstallJob {
                            id: id.clone(),
                            state: "installed".into(),
                            message: if id == "asr-zh" {
                                "asr-zh 与两个中文转写模型均已就绪".into()
                            } else {
                                format!("{id} 已安装并通过健康检查")
                            },
                            detail_percent: Some(100),
                            error: None,
                        },
                    );
                    if let Err(error) = app
                        .notification()
                        .builder()
                        .title(if id == "asr-zh" {
                            "中文视频转写已就绪".to_owned()
                        } else {
                            format!("{id} 已就绪")
                        })
                        .body(if id == "asr-zh" {
                            "fsmn-vad 与 SenseVoiceSmall 已下载并通过离线加载检查。"
                        } else {
                            "后台能力包已安装完成。"
                        })
                        .show()
                    {
                        tracing::warn!(?error, "failed to show pack completion notification");
                    }
                }
                Ok(Err(error)) => {
                    let detail = error.to_string();
                    manager.replace(
                        &id,
                        PackInstallJob {
                            id: id.clone(),
                            state: "error".into(),
                            message: format!("{id} 安装未完成"),
                            detail_percent: None,
                            error: Some(detail.clone()),
                        },
                    );
                    tracing::warn!(pack = %id, error = %detail, "background pack install failed");
                }
                Err(error) => {
                    let detail = format!("后台安装任务意外停止: {error}");
                    manager.replace(
                        &id,
                        PackInstallJob {
                            id: id.clone(),
                            state: "error".into(),
                            message: format!("{id} 安装未完成"),
                            detail_percent: None,
                            error: Some(detail.clone()),
                        },
                    );
                    tracing::warn!(pack = %id, error = %detail, "background pack task stopped");
                }
            }
        });
        starting
    }
}

/// What Repair is doing right now. `updated_at` is the wall clock of the last
/// real progress step: the Setup page compares it against its own clock to tell
/// "still working" from "stalled", which is the difference a plain spinner
/// cannot express.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct RepairJob {
    state: String,
    phase: String,
    message: String,
    current: u32,
    total: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    detail_percent: Option<u8>,
    started_at: u64,
    updated_at: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

impl RepairJob {
    fn idle() -> Self {
        Self {
            state: "idle".into(),
            phase: "idle".into(),
            message: "尚未开始".into(),
            current: 0,
            total: 0,
            detail_percent: None,
            started_at: 0,
            updated_at: 0,
            error: None,
        }
    }

    fn running(started_at: u64) -> Self {
        Self {
            state: "running".into(),
            phase: "preparing".into(),
            message: "正在准备修复".into(),
            current: 0,
            total: 0,
            detail_percent: None,
            started_at,
            updated_at: started_at,
            error: None,
        }
    }
}

/// Repair reinstalls whatever is missing, which on a slow or blocked network
/// means minutes inside one download. Owning it here rather than inside a
/// single `invoke` keeps the work — and the ability to report or stop it —
/// alive across a reloaded or hidden Setup window.
#[derive(Clone)]
pub(crate) struct RepairManager {
    job: Arc<Mutex<RepairJob>>,
    cancel: Arc<AtomicBool>,
}

impl Default for RepairManager {
    fn default() -> Self {
        Self {
            job: Arc::new(Mutex::new(RepairJob::idle())),
            cancel: Arc::new(AtomicBool::new(false)),
        }
    }
}

impl RepairManager {
    fn snapshot(&self) -> RepairJob {
        self.job
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }

    fn replace(&self, job: RepairJob) {
        *self
            .job
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = job;
    }

    fn report(&self, progress: SetupProgress) {
        let mut job = self
            .job
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if job.state != "running" && job.state != "stopping" {
            return;
        }
        job.phase = progress.phase;
        job.message = progress.message;
        job.current = progress.current;
        job.total = progress.total;
        job.detail_percent = progress.detail_percent;
        job.updated_at = now_ms();
    }

    fn stop(&self) -> RepairJob {
        self.cancel.store(true, Ordering::Relaxed);
        let mut job = self
            .job
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if job.state == "running" {
            // The worker only notices between steps or download chunks, so the
            // page needs its own state for "asked to stop, still winding down".
            job.state = "stopping".into();
            job.message = "正在停止修复".into();
            job.detail_percent = None;
            job.updated_at = now_ms();
        }
        job.clone()
    }

    fn finish(&self, state: &str, message: String, error: Option<String>) {
        let mut job = self
            .job
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        job.state = state.to_owned();
        job.message = message;
        job.error = error;
        job.detail_percent = None;
        job.updated_at = now_ms();
        if state == "done" {
            job.current = job.total;
        }
    }

    fn start(&self, app: AppHandle, source: Option<PathBuf>) -> RepairJob {
        {
            let job = self
                .job
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            if job.state == "running" || job.state == "stopping" {
                return job.clone();
            }
        }
        self.cancel.store(false, Ordering::Relaxed);
        let starting = RepairJob::running(now_ms());
        self.replace(starting.clone());

        let manager = self.clone();
        let cancel = Arc::clone(&self.cancel);
        tauri::async_runtime::spawn(async move {
            let worker = manager.clone();
            let outcome = tauri::async_runtime::spawn_blocking(move || {
                SetupCore::from_environment()?
                    .with_cli_source(source)
                    .with_cancel(cancel)
                    .repair_with_progress(|event| worker.report(event))
            })
            .await;

            match outcome {
                Ok(Ok(_)) => {
                    manager.finish("done", "修复完成，所有组件已通过校验".into(), None);
                    if let Err(error) = crate::start_browser(&app) {
                        tracing::warn!(?error, "cannot start browser after repair");
                    }
                }
                Ok(Err(SetupError::Cancelled)) => manager.finish(
                    "cancelled",
                    "修复已停止；已下载的部分会保留，下次从断点继续".into(),
                    None,
                ),
                Ok(Err(error)) => {
                    let detail = error.to_string();
                    tracing::warn!(error = %detail, "repair failed");
                    manager.finish("error", repair_failure_message(&error), Some(detail));
                }
                Err(error) => {
                    let detail = format!("修复任务意外停止: {error}");
                    tracing::warn!(error = %detail, "repair task stopped");
                    manager.finish("error", "修复任务意外停止".into(), Some(detail));
                }
            }
        });
        starting
    }
}

/// Turn the failure into the sentence a stuck user actually needs: what went
/// wrong and what to do next. The raw error still travels in `error`.
fn repair_failure_message(error: &SetupError) -> String {
    match error {
        SetupError::Locked => {
            "另一个安装任务正在进行（例如中文视频转写组件），等它结束后再修复".into()
        }
        SetupError::Download { .. } => {
            "下载没有完成，通常是网络不通或被代理拦截；恢复网络后可重试，已下载的部分会续传".into()
        }
        SetupError::Probe { pack, .. } => format!("{pack} 安装后未通过健康检查，可重试修复"),
        SetupError::OwnershipLost(path) => {
            format!(
                "{} 已被其他程序替换，请在设置中重新安装该宿主",
                path.display()
            )
        }
        other => other.to_string(),
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_millis() as u64)
        .unwrap_or_default()
}

#[tauri::command]
pub(crate) async fn setup_inspect(
    app: AppHandle,
    window: WebviewWindow,
) -> Result<SetupInspection, String> {
    require_setup_window(&window)?;
    let source = sidecar_cli_source(&app);
    run(move || {
        SetupCore::from_environment()?
            .with_cli_source(source)
            .inspect()
    })
    .await
}

#[tauri::command]
pub(crate) async fn setup_status(window: WebviewWindow) -> Result<SetupResult, String> {
    require_setup_window(&window)?;
    run(|| SetupCore::from_environment()?.status()).await
}

#[tauri::command]
pub(crate) async fn setup_pick_wiki_directory(
    app: AppHandle,
    window: WebviewWindow,
    current: Option<PathBuf>,
) -> Result<Option<PathBuf>, String> {
    require_setup_window(&window)?;
    let mut picker = app
        .dialog()
        .file()
        .set_parent(&window)
        .set_title("选择 Wiki 存放文件夹");
    if let Some(directory) = picker_start_dir(current) {
        picker = picker.set_directory(directory);
    }
    picker
        .blocking_pick_folder()
        .map(|path| path.into_path().map_err(|error| error.to_string()))
        .transpose()
}

#[tauri::command]
pub(crate) async fn setup_pick_install_directory(
    app: AppHandle,
    window: WebviewWindow,
    current: Option<PathBuf>,
) -> Result<Option<PathBuf>, String> {
    require_setup_window(&window)?;
    let mut picker = app
        .dialog()
        .file()
        .set_parent(&window)
        .set_title("选择工具链与 Skills 安装文件夹");
    if let Some(directory) = picker_start_dir(current) {
        picker = picker.set_directory(directory);
    }
    picker
        .blocking_pick_folder()
        .map(|path| path.into_path().map_err(|error| error.to_string()))
        .transpose()
}

/// Report whether a candidate install location is usable and how much room it
/// has, so the Setup page can warn before a multi-gigabyte download starts
/// rather than after it runs out of space.
#[tauri::command]
pub(crate) async fn setup_probe_install_root(
    window: WebviewWindow,
    path: PathBuf,
) -> Result<InstallRootProbe, String> {
    require_setup_window(&window)?;
    run(move || SetupCore::from_environment()?.probe_install_root(&path)).await
}

#[tauri::command]
pub(crate) async fn setup_apply(
    app: AppHandle,
    window: WebviewWindow,
    request: SetupRequest,
) -> Result<SetupResult, String> {
    require_setup_window(&window)?;
    let progress_app = app.clone();
    let source = sidecar_cli_source(&app);
    let result = run(move || {
        let core = SetupCore::from_environment()?.with_cli_source(source);
        core.setup_with_progress(request, |event| emit(&progress_app, event))
    })
    .await?;
    crate::start_browser(&app).map_err(|error| error.to_string())?;
    Ok(result)
}

#[tauri::command]
pub(crate) fn setup_start_repair(
    app: AppHandle,
    window: WebviewWindow,
    manager: State<'_, RepairManager>,
) -> Result<RepairJob, String> {
    require_setup_window(&window)?;
    let source = sidecar_cli_source(&app);
    Ok(manager.start(app, source))
}

#[tauri::command]
pub(crate) fn setup_repair_status(
    window: WebviewWindow,
    manager: State<'_, RepairManager>,
) -> Result<RepairJob, String> {
    require_setup_window(&window)?;
    Ok(manager.snapshot())
}

#[tauri::command]
pub(crate) fn setup_stop_repair(
    window: WebviewWindow,
    manager: State<'_, RepairManager>,
) -> Result<RepairJob, String> {
    require_setup_window(&window)?;
    Ok(manager.stop())
}

#[tauri::command]
pub(crate) fn setup_open_wiki(app: AppHandle, window: WebviewWindow) -> Result<(), String> {
    require_setup_window(&window)?;
    crate::start_browser(&app).map_err(|error| error.to_string())?;
    crate::open_local_wiki(&app).map_err(|error| error.to_string())
}

#[tauri::command]
pub(crate) fn setup_open_wiki_directory(window: WebviewWindow) -> Result<(), String> {
    require_setup_window(&window)?;
    let status = SetupCore::from_environment()
        .map_err(|error| error.to_string())?
        .status()
        .map_err(|error| error.to_string())?;
    open::that(&status.wiki.path).map_err(|error| format!("无法打开 Wiki 目录: {error}"))
}

#[tauri::command]
pub(crate) async fn setup_update(
    app: AppHandle,
    window: WebviewWindow,
    check: bool,
) -> Result<UpdateResult, String> {
    require_setup_window(&window)?;
    let result = run(move || SetupCore::from_environment()?.update(check)).await?;
    if let Some(runtime) = app.try_state::<crate::BrowserRuntime>() {
        if check {
            runtime.update_manager.check();
        } else if result.restart_required || runtime.update_manager.status().state == "available" {
            runtime.update_manager.install_latest()?;
        }
    }
    Ok(result)
}

#[tauri::command]
pub(crate) fn setup_browser_update_status(
    app: AppHandle,
    window: WebviewWindow,
) -> Result<UpdateStatus, String> {
    require_setup_window(&window)?;
    app.try_state::<crate::BrowserRuntime>()
        .map(|runtime| runtime.update_manager.status())
        .ok_or_else(|| "Browser updater is unavailable".into())
}

#[tauri::command]
pub(crate) fn setup_restart(app: AppHandle, window: WebviewWindow) -> Result<(), String> {
    require_setup_window(&window)?;
    app.restart();
}

#[tauri::command]
pub(crate) async fn setup_ensure_pack(
    app: AppHandle,
    window: WebviewWindow,
    id: String,
) -> Result<SetupResult, String> {
    require_setup_window(&window)?;
    let progress_app = app.clone();
    run(move || {
        SetupCore::from_environment()?
            .ensure_pack_with_progress(&id, |event| emit(&progress_app, event))
    })
    .await
}

#[tauri::command]
pub(crate) fn setup_start_pack_install(
    app: AppHandle,
    window: WebviewWindow,
    manager: State<'_, PackInstallManager>,
    id: String,
) -> Result<PackInstallJob, String> {
    require_setup_window(&window)?;
    if !matches!(id.as_str(), "asr-zh" | "asr-other" | "toolchain-base") {
        return Err(format!("unsupported background pack: {id}"));
    }
    Ok(manager.start(app, id))
}

#[tauri::command]
pub(crate) fn setup_pack_install_status(
    window: WebviewWindow,
    manager: State<'_, PackInstallManager>,
    id: String,
) -> Result<PackInstallJob, String> {
    require_setup_window(&window)?;
    Ok(manager.snapshot(&id))
}

#[tauri::command]
pub(crate) async fn setup_provider_config(window: WebviewWindow) -> Result<ProviderConfig, String> {
    require_setup_window(&window)?;
    run(|| SetupCore::from_environment()?.provider_config()).await
}

#[tauri::command]
pub(crate) async fn setup_save_provider_config(
    window: WebviewWindow,
    config: ProviderConfig,
) -> Result<ProviderConfig, String> {
    require_setup_window(&window)?;
    run(move || SetupCore::from_environment()?.save_provider_config(&config)).await
}

async fn run<T: Send + 'static>(
    task: impl FnOnce() -> llm_wiki_setup::Result<T> + Send + 'static,
) -> Result<T, String> {
    tauri::async_runtime::spawn_blocking(task)
        .await
        .map_err(|error| format!("setup task stopped unexpectedly: {error}"))?
        .map_err(|error| error.to_string())
}

fn emit(app: &AppHandle, event: SetupProgress) {
    let _ = app.emit("setup-progress", event);
}

fn require_setup_window(window: &WebviewWindow) -> Result<(), String> {
    if window.label() == "setup" {
        Ok(())
    } else {
        Err("Setup Core commands are only available to the embedded Setup window".into())
    }
}

/// Where the Wiki folder picker should open. `current` is the wiki collection
/// root the user is choosing (e.g. ~/wikis), which may not exist yet at install
/// time. Open at that directory, or — since a fresh machine has no ~/wikis — at
/// its nearest existing ancestor (~), so the user can pick or create the
/// collection folder there.
fn picker_start_dir(current: Option<PathBuf>) -> Option<PathBuf> {
    let expanded = current.and_then(expand_home_path)?;
    nearest_existing_dir(expanded)
}

fn nearest_existing_dir(path: PathBuf) -> Option<PathBuf> {
    let mut candidate = Some(path);
    while let Some(dir) = candidate {
        if dir.is_dir() {
            return Some(dir);
        }
        candidate = dir.parent().map(Path::to_path_buf);
    }
    None
}

fn expand_home_path(path: PathBuf) -> Option<PathBuf> {
    if path.is_absolute() {
        return Some(path);
    }
    let home = dirs::home_dir()?;
    if path == Path::new("~") {
        return Some(home);
    }
    path.strip_prefix("~")
        .ok()
        .map(|relative| home.join(relative))
}

fn sidecar_cli_source(app: &AppHandle) -> Option<std::path::PathBuf> {
    let mut directories = Vec::new();
    if let Ok(executable) = std::env::current_exe()
        && let Some(parent) = executable.parent()
    {
        directories.push(parent.to_path_buf());
    }
    if let Ok(resources) = app.path().resource_dir() {
        directories.push(resources);
    }
    directories.into_iter().find_map(|directory| {
        std::fs::read_dir(directory)
            .ok()?
            .filter_map(Result::ok)
            .find_map(|entry| {
                let path = entry.path();
                let name = path.file_name()?.to_str()?.to_ascii_lowercase();
                let sidecar_name = matches!(name.as_str(), "my-llm-wiki" | "my-llm-wiki.exe")
                    || (name.starts_with("my-llm-wiki-") && !name.contains("browser"));
                (path.is_file() && sidecar_name).then_some(path)
            })
    })
}
