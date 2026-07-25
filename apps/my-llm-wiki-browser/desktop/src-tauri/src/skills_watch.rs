//! Background awareness of the Skills Pack channel.
//!
//! Skills ship on their own cadence, so the Browser can be perfectly current
//! while the skills an agent actually runs are a release behind. This watches
//! for that in the background and says so; it never installs anything on its
//! own — applying an update stays a thing the user asks for.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use llm_wiki_setup::{SetupCore, SkillsUpdateState, UpdateResult};
use tauri::menu::MenuItem;

/// Matches the Browser updater's cadence: late enough not to compete with
/// startup, then once a day.
const FIRST_CHECK_DELAY: Duration = Duration::from_secs(10);
const CHECK_INTERVAL: Duration = Duration::from_secs(24 * 60 * 60);
const SETUP_MENU_LABEL: &str = "Skills 与工具链…";

#[derive(Clone)]
pub(crate) struct SkillsWatch {
    latest: Arc<Mutex<Option<UpdateResult>>>,
    menu: MenuItem<tauri::Wry>,
}

impl SkillsWatch {
    pub(crate) fn new(menu: MenuItem<tauri::Wry>) -> Self {
        Self {
            latest: Arc::new(Mutex::new(None)),
            menu,
        }
    }

    /// The most recent check, if one has completed. The Setup page reads this
    /// so opening it shows what is available immediately rather than after a
    /// round trip the user has to trigger.
    pub(crate) fn latest(&self) -> Option<UpdateResult> {
        self.latest.lock().ok().and_then(|latest| latest.clone())
    }

    pub(crate) fn record(&self, result: &UpdateResult) {
        if let Ok(mut latest) = self.latest.lock() {
            *latest = Some(result.clone());
        }
        self.mark(result);
    }

    /// Carry the news on the tray entry that already opens this page, rather
    /// than adding a second place to look.
    fn mark(&self, result: &UpdateResult) {
        let label = match (&result.skills.state, &result.skills.latest_version) {
            (SkillsUpdateState::Available, Some(version)) => {
                format!("{SETUP_MENU_LABEL}（Skills v{version} 可用）")
            }
            _ => SETUP_MENU_LABEL.to_owned(),
        };
        let _ = self.menu.set_text(label);
    }

    pub(crate) fn spawn_periodic_check(&self) {
        let watch = self.clone();
        tauri::async_runtime::spawn(async move {
            tokio::time::sleep(FIRST_CHECK_DELAY).await;
            loop {
                watch.check_now().await;
                tokio::time::sleep(CHECK_INTERVAL).await;
            }
        });
    }

    async fn check_now(&self) {
        let watch = self.clone();
        let checked = tauri::async_runtime::spawn_blocking(move || {
            // Read-only: `check` never writes, so a background poll cannot race
            // an install the user started.
            SetupCore::from_environment().and_then(|core| core.update(true))
        })
        .await;
        match checked {
            Ok(Ok(result)) => watch.record(&result),
            Ok(Err(error)) => tracing::debug!(error = ?error, "skills update check failed"),
            Err(error) => tracing::debug!(error = ?error, "skills update check panicked"),
        }
    }
}
