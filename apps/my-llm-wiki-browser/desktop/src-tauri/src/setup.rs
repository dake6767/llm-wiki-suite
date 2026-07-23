use llm_wiki_setup::{
    ProviderConfig, SetupCore, SetupInspection, SetupProgress, SetupRequest, SetupResult,
    UpdateResult,
};
use std::path::{Path, PathBuf};
use tauri::{AppHandle, Emitter as _, Manager as _, WebviewWindow};
use tauri_plugin_dialog::DialogExt as _;

use crate::update::UpdateStatus;

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
    if let Some(directory) = current.and_then(expand_home_path) {
        picker = picker.set_directory(directory);
    }
    picker
        .blocking_pick_folder()
        .map(|path| path.into_path().map_err(|error| error.to_string()))
        .transpose()
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
    if let Err(error) = crate::open_local_wiki(&app) {
        tracing::warn!(error = ?error, "setup completed but the Wiki could not be opened");
    }
    Ok(result)
}

#[tauri::command]
pub(crate) async fn setup_repair(
    app: AppHandle,
    window: WebviewWindow,
) -> Result<SetupResult, String> {
    require_setup_window(&window)?;
    let progress_app = app.clone();
    let source = sidecar_cli_source(&app);
    let result = run(move || {
        SetupCore::from_environment()?
            .with_cli_source(source)
            .repair_with_progress(|event| emit(&progress_app, event))
    })
    .await?;
    crate::start_browser(&app).map_err(|error| error.to_string())?;
    Ok(result)
}

#[tauri::command]
pub(crate) fn setup_open_wiki(app: AppHandle, window: WebviewWindow) -> Result<(), String> {
    require_setup_window(&window)?;
    crate::start_browser(&app).map_err(|error| error.to_string())?;
    crate::open_local_wiki(&app).map_err(|error| error.to_string())
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
