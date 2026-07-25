mod error;
mod link;
mod mcp_bridge;
mod model;
mod pack;
mod process;
mod root;
mod wiki;

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::{BufRead as _, BufReader, Read as _};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

pub use error::{Result, SetupError};
use fs2::FileExt as _;
use include_dir::{Dir, include_dir};
pub use mcp_bridge::{McpBridgeOptions, run as run_mcp_bridge};
pub use model::*;
use sha2::{Digest as _, Sha256};

static BUNDLED_SKILLS: Dir<'_> = include_dir!("$CARGO_MANIFEST_DIR/../../../../skills");

const DISTRIBUTION_VERSION: &str = env!("CARGO_PKG_VERSION");
/// The wiki collection root the user picks by default — the parent that holds
/// every wiki volume.
const DEFAULT_WIKI_COLLECTION_RELATIVE: &str = "wikis";
/// The default volume created inside the collection root on first setup. Later
/// siblings (e.g. `ai-wiki`) are created next to it via the agent skills.
const DEFAULT_WIKI_NAME: &str = "my-llm-wiki";
const ASR_ZH_MODEL_CACHE_ID: &str = "asr-zh";
const ASR_ZH_MODEL_MARKER: &str = ".my-llm-wiki-models.json";
const ASR_ZH_VAD_ID: &str = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch";
const ASR_ZH_SENSEVOICE_ID: &str = "iic/SenseVoiceSmall";
const ASR_PROGRESS_ENV: &str = "MY_LLM_WIKI_ASR_PROGRESS";

#[derive(Clone)]
pub struct SetupCore {
    home: PathBuf,
    /// The fixed `~/.my-llm-wiki` path. Every Skill and every already-registered
    /// MCP server resolves this on its own, so it never moves; when the user
    /// installs elsewhere it becomes a link to `suite_home`.
    anchor: PathBuf,
    /// The real install root. Equal to `anchor` for a default install.
    suite_home: PathBuf,
    state_path: PathBuf,
    providers_path: PathBuf,
    cli_source: Option<PathBuf>,
    cli_path: PathBuf,
    wiki_path: PathBuf,
    registry_path: PathBuf,
    current_manifest_sources: Vec<String>,
    latest_manifest_sources: Vec<String>,
    cancel: Arc<AtomicBool>,
}

impl SetupCore {
    pub fn from_environment() -> Result<Self> {
        let home = dirs::home_dir().ok_or(SetupError::HomeUnavailable)?;
        let mut core = Self::new(home);
        if let Ok(executable) = std::env::current_exe() {
            let name = executable.file_name().and_then(|value| value.to_str());
            if matches!(name, Some("my-llm-wiki" | "my-llm-wiki.exe")) {
                core.cli_source = Some(executable);
            }
        }
        Ok(core)
    }

    pub fn new(home: PathBuf) -> Self {
        let anchor = home.join(root::ANCHOR_NAME);
        let suite_home = root::resolve(&anchor);
        let version = DISTRIBUTION_VERSION;
        Self {
            state_path: PathBuf::new(),
            providers_path: PathBuf::new(),
            cli_path: PathBuf::new(),
            cli_source: None,
            registry_path: PathBuf::new(),
            wiki_path: home
                .join(DEFAULT_WIKI_COLLECTION_RELATIVE)
                .join(DEFAULT_WIKI_NAME),
            anchor,
            suite_home: PathBuf::new(),
            home,
            current_manifest_sources: vec![
                format!("https://wiki.htmlgo.to/_distribution/v{version}/distribution.json"),
                format!(
                    "https://github.com/dake6767/llm-wiki-suite/releases/download/v{version}/distribution.json"
                ),
            ],
            latest_manifest_sources: vec![
                "https://wiki.htmlgo.to/_distribution/latest.json".into(),
                "https://github.com/dake6767/llm-wiki-suite/releases/latest/download/distribution.json"
                    .into(),
            ],
            cancel: Arc::new(AtomicBool::new(false)),
        }
        .with_suite_home(suite_home)
    }

    /// Re-derive every path that hangs off the install root.
    ///
    /// Setup calls this after the user picks a location, so the same core can
    /// be built from the anchor and then rebased onto the chosen root before it
    /// takes the lock or writes anything.
    fn with_suite_home(mut self, suite_home: PathBuf) -> Self {
        self.state_path = suite_home.join("setup-state.json");
        self.providers_path = suite_home.join("providers.json");
        self.cli_path = suite_home.join("bin").join(if cfg!(windows) {
            "my-llm-wiki.exe"
        } else {
            "my-llm-wiki"
        });
        self.registry_path = suite_home.join("wikis.json");
        self.suite_home = suite_home;
        self
    }

    /// The one installed copy of the Skills Pack, which every host links to.
    fn canonical_skills_dir(&self) -> PathBuf {
        self.suite_home.join("skills")
    }

    /// Where packs, models, and the Skills Pack actually live.
    pub fn install_root(&self) -> &Path {
        &self.suite_home
    }

    /// The fixed `~/.my-llm-wiki` path. Equal to [`Self::install_root`] unless
    /// the install was moved to another location.
    pub fn install_anchor(&self) -> &Path {
        &self.anchor
    }

    /// Report free space and usability for a location the user is considering.
    pub fn probe_install_root(&self, path: &Path) -> Result<InstallRootProbe> {
        let path = root::requested(&self.anchor, &self.home, Some(path))?;
        let existing = path.join("setup-state.json").is_file();
        // Free space and write access belong to the nearest directory that
        // exists: a root the user is about to create has neither yet.
        let probe_dir = nearest_existing_dir(&path);
        Ok(InstallRootProbe {
            exists: path.is_dir(),
            writable: probe_dir.is_some_and(is_writable_dir),
            free_bytes: probe_dir.and_then(|dir| fs2::available_space(dir).ok()),
            existing_install: existing,
            path,
        })
    }

    pub fn with_cli_source(mut self, source: Option<PathBuf>) -> Self {
        self.cli_source = source;
        self
    }

    /// Share a stop flag with the caller. Long operations poll it between
    /// steps and between download chunks, so setting it ends the run with
    /// [`SetupError::Cancelled`] instead of leaving the caller to wait out a
    /// download that is no longer wanted.
    pub fn with_cancel(mut self, cancel: Arc<AtomicBool>) -> Self {
        self.cancel = cancel;
        self
    }

    fn cancel_check(&self) -> impl Fn() -> bool + use<> {
        let flag = Arc::clone(&self.cancel);
        move || flag.load(Ordering::Relaxed)
    }

    fn ensure_running(&self) -> Result<()> {
        if self.cancel.load(Ordering::Relaxed) {
            return Err(SetupError::Cancelled);
        }
        Ok(())
    }

    pub fn with_manifest_sources(mut self, current: Vec<String>, latest: Vec<String>) -> Self {
        self.current_manifest_sources = current;
        self.latest_manifest_sources = latest;
        self
    }

    pub fn inspect(&self) -> Result<SetupInspection> {
        let state = self.load_state()?;
        let install_id = state.as_ref().map(|state| state.install_id.as_str());
        let wiki_path = state
            .as_ref()
            .map(|state| state.wiki_path.as_path())
            .unwrap_or(&self.wiki_path);
        let hosts = host_definitions(&self.home)
            .into_iter()
            .map(|host| HostInspection {
                id: host.id,
                label: host.label,
                detected: host.detect_dir.is_dir(),
                destinations: bundled_skill_slugs()
                    .into_iter()
                    .map(|slug| {
                        let path = host.skills_dir.join(&slug);
                        SkillDestination {
                            slug,
                            state: destination_state(&path, install_id),
                            path,
                        }
                    })
                    .collect(),
                skills_dir: host.skills_dir,
            })
            .collect();
        Ok(SetupInspection {
            schema: STATE_SCHEMA,
            distribution_version: DISTRIBUTION_VERSION.to_owned(),
            state_path: self.state_path.clone(),
            cli_path: state
                .as_ref()
                .and_then(|state| state.cli_path.clone())
                .or_else(|| self.cli_source.as_ref().map(|_| self.cli_path.clone())),
            hosts,
            wiki: wiki::status(wiki_path, &self.registry_path),
            official_toolchain: self.toolchain_status(state.as_ref()),
            install_root_relocated: self.suite_home != self.anchor,
            install_root: self.suite_home.clone(),
            install_anchor: self.anchor.clone(),
        })
    }

    pub fn status(&self) -> Result<SetupResult> {
        let Some(state) = self.load_state()? else {
            return Ok(SetupResult {
                state: SetupHealth::NotConfigured,
                distribution_version: DISTRIBUTION_VERSION.to_owned(),
                cli_path: None,
                hosts: BTreeMap::new(),
                wiki: wiki::status(&self.wiki_path, &self.registry_path),
                official_toolchain: self.toolchain_status(None),
                packs: BTreeMap::new(),
                model_caches: BTreeMap::new(),
                backups: Vec::new(),
                actions: Vec::new(),
            });
        };
        let hosts = self.host_results(&state);
        let wiki = wiki::status(&state.wiki_path, &self.registry_path);
        let packs = self.pack_statuses(&state);
        let official_toolchain = packs
            .get("toolchain-base")
            .cloned()
            .unwrap_or_else(|| self.toolchain_status(None));
        let healthy = !hosts.is_empty()
            && hosts.values().all(|host| host.healthy)
            && wiki.ready
            && state.cli_path.as_ref().is_none_or(|path| path.is_file())
            && packs.values().all(|pack| pack.healthy)
            && (!state.official_toolchain || official_toolchain.healthy);
        let actions = state.packs.values().flat_map(pack::actions).collect();
        Ok(SetupResult {
            state: if healthy {
                SetupHealth::Ready
            } else {
                SetupHealth::NeedsRepair
            },
            distribution_version: state.distribution_version,
            cli_path: state.cli_path.clone(),
            hosts,
            wiki,
            official_toolchain,
            packs,
            model_caches: BTreeMap::from([(
                ASR_ZH_MODEL_CACHE_ID.into(),
                self.asr_zh_model_cache_status(),
            )]),
            backups: Vec::new(),
            actions,
        })
    }

    pub fn setup(&self, request: SetupRequest) -> Result<SetupResult> {
        self.setup_with_progress(request, |_| {})
    }

    pub fn setup_with_progress(
        &self,
        request: SetupRequest,
        progress: impl Fn(SetupProgress),
    ) -> Result<SetupResult> {
        if request.hosts.is_empty() {
            return Err(SetupError::NoHosts);
        }
        // Settle the install location before anything else touches disk: the
        // lock, the state file, and every pack path below have to belong to the
        // root the user chose, not to the one this core was built from.
        let install_root =
            root::requested(&self.anchor, &self.home, request.install_root.as_deref())?;
        root::ensure_anchor(&self.anchor, &install_root)?;
        self.clone()
            .with_suite_home(install_root)
            .setup_resolved(request, progress)
    }

    fn setup_resolved(
        &self,
        request: SetupRequest,
        progress: impl Fn(SetupProgress),
    ) -> Result<SetupResult> {
        let total = request.hosts.len() as u32 + 2 + u32::from(request.install_official_toolchain);
        report(&progress, "preparing", "正在校验目标与所有权", 0, total);
        let _lock = self.lock()?;
        let existing_state = self.load_state()?;
        let wiki_path = match request.wiki_path.as_deref() {
            Some(path) => self.resolve_wiki_path(path)?,
            None => existing_state
                .as_ref()
                .map(|state| state.wiki_path.clone())
                .unwrap_or_else(|| self.wiki_path.clone()),
        };
        let mut state = existing_state.unwrap_or_else(|| SetupState {
            schema: STATE_SCHEMA,
            install_id: new_install_id(),
            distribution_version: DISTRIBUTION_VERSION.to_owned(),
            skills_pack_version: DISTRIBUTION_VERSION.to_owned(),
            cli_path: None,
            official_toolchain: request.install_official_toolchain,
            hosts: BTreeMap::new(),
            packs: BTreeMap::new(),
            wiki_path: self.wiki_path.clone(),
        });
        state.official_toolchain = request.install_official_toolchain;
        state.wiki_path = wiki_path;
        self.install_cli(&mut state)?;
        let definitions = host_definitions_by_id(&self.home);
        let mut replacements = authorize_replacements(
            &definitions,
            &request.hosts,
            &state.install_id,
            &request.replace,
        )?;
        if !request.install_official_toolchain {
            self.remove_owned_pack(&mut state, "toolchain-base")?;
        }
        let mut backups = Vec::new();
        self.save_state(&state)?;
        let mut completed = 0;
        report(
            &progress,
            "skills",
            "正在展开 Skills Pack",
            completed,
            total,
        );
        let canonical =
            self.install_skills(&state.install_id, |slug, skill_current, skill_total| {
                report_skill_progress(
                    &progress,
                    "安装目录",
                    slug,
                    completed,
                    total,
                    skill_current,
                    skill_total,
                )
            })?;
        completed += 1;
        report(&progress, "skills", "Skills Pack 已展开", completed, total);
        for host_id in request.hosts {
            let host = definitions
                .get(&host_id)
                .ok_or_else(|| SetupError::UnknownHost(host_id.clone()))?;
            report(
                &progress,
                "skills",
                &format!("正在为 {} 激活 Skills Pack", host.label),
                completed,
                total,
            );
            let owned = self.link_host(
                host,
                &state.install_id,
                &canonical,
                &mut replacements,
                &mut backups,
                |slug, skill_current, skill_total| {
                    report_skill_progress(
                        &progress,
                        &host.label,
                        slug,
                        completed,
                        total,
                        skill_current,
                        skill_total,
                    )
                },
            )?;
            state.hosts.insert(host_id, owned);
            self.save_state(&state)?;
            completed += 1;
            report(
                &progress,
                "skills",
                &format!("{} 的 Skills Pack 已激活", host.label),
                completed,
                total,
            );
        }
        if let Some(unused) = replacements.into_iter().next() {
            return Err(SetupError::UnusedReplacement(unused));
        }
        let schema = BUNDLED_SKILLS
            .get_file("my-llm-wiki/assets/schema.md")
            .ok_or_else(|| SetupError::InvalidState("bundled schema.md is missing".into()))?;
        report(
            &progress,
            "wiki",
            "正在初始化 Wiki 与 RAW 目录",
            completed,
            total,
        );
        wiki::ensure(&state.wiki_path, &self.registry_path, schema.contents())?;
        completed += 1;
        report(&progress, "wiki", "Wiki 已初始化", completed, total);
        state.distribution_version = DISTRIBUTION_VERSION.to_owned();
        state.skills_pack_version = DISTRIBUTION_VERSION.to_owned();
        if request.install_official_toolchain {
            report(
                &progress,
                "toolchain",
                "正在获取官方工具链清单",
                completed,
                total,
            );
            let manifest = pack::fetch_manifest(&self.current_manifest_sources)?;
            self.require_current_distribution(&manifest)?;
            let artifact = pack::select_artifact(&manifest, "toolchain-base")?;
            let installed = pack::install_pack_with_progress(
                &self.suite_home,
                artifact,
                &self.cancel_check(),
                |event| {
                    report_pack_progress(
                        &progress,
                        "toolchain",
                        "推荐工具链",
                        completed,
                        total,
                        event,
                    )
                },
            )?;
            state.packs.insert("toolchain-base".into(), installed);
            completed += 1;
            report(
                &progress,
                "toolchain",
                "推荐工具链已通过健康检查",
                completed,
                total,
            );
        }
        self.save_state(&state)?;
        self.prune_active_packs(&state);
        let mut result = self.status()?;
        result.backups = backups;
        Ok(result)
    }

    /// Resolve the wiki volume from the collection root the user selected.
    ///
    /// The user picks the parent directory (e.g. `~/wikis`); the default
    /// `my-llm-wiki` volume is created beneath it. A path that already *is* an
    /// initialized volume (`schema.md` present) or already ends with the volume
    /// name is used verbatim, so re-runs and paths typed by hand never nest a
    /// `my-llm-wiki/my-llm-wiki`.
    fn resolve_wiki_path(&self, path: &Path) -> Result<PathBuf> {
        let root = if path == Path::new("~") {
            self.home.clone()
        } else if let Ok(relative) = path.strip_prefix("~") {
            self.home.join(relative)
        } else if path.is_absolute() {
            path.to_path_buf()
        } else {
            return Err(SetupError::InvalidWikiPath(path.to_path_buf()));
        };
        let already_a_volume = root.join("schema.md").is_file()
            || root.file_name().and_then(|name| name.to_str()) == Some(DEFAULT_WIKI_NAME);
        if already_a_volume {
            Ok(root)
        } else {
            Ok(root.join(DEFAULT_WIKI_NAME))
        }
    }

    /// Give one more agent host the Skills Pack that is already installed.
    ///
    /// `setup` is the first-run decision — install root, Wiki, official
    /// toolchain — and needs the distribution manifest to make it. Adding a
    /// host afterwards decides none of that: the Skills Pack is on disk, so
    /// this stays local and offline, and leaves packs, Wiki, and the hosts it
    /// was not asked about untouched.
    pub fn install_hosts(
        &self,
        hosts: &BTreeSet<String>,
        replace: &BTreeSet<PathBuf>,
    ) -> Result<SetupResult> {
        self.install_hosts_with_progress(hosts, replace, |_| {})
    }

    pub fn install_hosts_with_progress(
        &self,
        hosts: &BTreeSet<String>,
        replace: &BTreeSet<PathBuf>,
        progress: impl Fn(SetupProgress),
    ) -> Result<SetupResult> {
        if hosts.is_empty() {
            return Err(SetupError::NoHosts);
        }
        let total = hosts.len() as u32 + 1;
        report(&progress, "preparing", "正在校验目标与所有权", 0, total);
        let _lock = self.lock()?;
        let mut state = self
            .load_state()?
            .ok_or_else(|| SetupError::InvalidState("setup has not been completed".into()))?;
        let definitions = host_definitions_by_id(&self.home);
        let mut replacements =
            authorize_replacements(&definitions, hosts, &state.install_id, replace)?;
        let mut backups = Vec::new();
        let mut completed = 0;
        report(
            &progress,
            "skills",
            "正在校验 Skills Pack",
            completed,
            total,
        );
        let canonical =
            self.install_skills(&state.install_id, |slug, skill_current, skill_total| {
                report_skill_progress(
                    &progress,
                    "安装目录",
                    slug,
                    completed,
                    total,
                    skill_current,
                    skill_total,
                )
            })?;
        completed += 1;
        for host_id in hosts {
            let host = definitions
                .get(host_id)
                .ok_or_else(|| SetupError::UnknownHost(host_id.clone()))?;
            report(
                &progress,
                "skills",
                &format!("正在为 {} 激活 Skills Pack", host.label),
                completed,
                total,
            );
            let owned = self.link_host(
                host,
                &state.install_id,
                &canonical,
                &mut replacements,
                &mut backups,
                |slug, skill_current, skill_total| {
                    report_skill_progress(
                        &progress,
                        &host.label,
                        slug,
                        completed,
                        total,
                        skill_current,
                        skill_total,
                    )
                },
            )?;
            state.hosts.insert(host_id.clone(), owned);
            // Saved per host, so a failure on the second one leaves the first
            // recorded exactly as it is on disk.
            self.save_state(&state)?;
            completed += 1;
            report(
                &progress,
                "skills",
                &format!("{} 的 Skills Pack 已激活", host.label),
                completed,
                total,
            );
        }
        let mut result = self.status()?;
        result.backups = backups;
        Ok(result)
    }

    pub fn repair(&self) -> Result<SetupResult> {
        self.repair_with_progress(|_| {})
    }

    pub fn repair_with_progress(&self, progress: impl Fn(SetupProgress)) -> Result<SetupResult> {
        let _lock = self.lock()?;
        let mut state = self
            .load_state()?
            .ok_or_else(|| SetupError::InvalidState("setup has not been completed".into()))?;
        self.install_cli(&mut state)?;
        let definitions = host_definitions_by_id(&self.home);
        let host_ids: Vec<_> = state.hosts.keys().cloned().collect();
        let mut pack_ids: BTreeSet<_> = state.packs.keys().cloned().collect();
        if state.official_toolchain {
            pack_ids.insert("toolchain-base".into());
        }
        let total = host_ids.len() as u32 + 2 + pack_ids.len() as u32;
        report(&progress, "preparing", "正在检查当前安装", 0, total);
        let mut completed = 0;
        let mut replacements = BTreeSet::new();
        let mut backups = Vec::new();
        // Prove ownership of every recorded destination before writing
        // anything, so a host that was taken over stops the run instead of
        // stopping it halfway through.
        for host_id in &host_ids {
            let host = definitions
                .get(host_id)
                .ok_or_else(|| SetupError::UnknownHost(host_id.clone()))?;
            for slug in bundled_skill_slugs() {
                let path = host.skills_dir.join(&slug);
                match destination_state(&path, Some(&state.install_id)) {
                    DestinationState::Foreign => return Err(SetupError::OwnershipLost(path)),
                    DestinationState::Absent | DestinationState::Owned => {}
                }
            }
        }
        report(
            &progress,
            "skills",
            "正在校验 Skills Pack",
            completed,
            total,
        );
        let canonical =
            self.install_skills(&state.install_id, |slug, skill_current, skill_total| {
                report_skill_progress(
                    &progress,
                    "安装目录",
                    slug,
                    completed,
                    total,
                    skill_current,
                    skill_total,
                )
            })?;
        completed += 1;
        report(&progress, "skills", "Skills Pack 已校验", completed, total);
        for host_id in host_ids {
            self.ensure_running()?;
            let host = definitions
                .get(&host_id)
                .ok_or_else(|| SetupError::UnknownHost(host_id.clone()))?;
            report(
                &progress,
                "skills",
                &format!("正在校验 {} 的 Skills Pack", host.label),
                completed,
                total,
            );
            let owned = self.link_host(
                host,
                &state.install_id,
                &canonical,
                &mut replacements,
                &mut backups,
                |slug, skill_current, skill_total| {
                    report_skill_progress(
                        &progress,
                        &host.label,
                        slug,
                        completed,
                        total,
                        skill_current,
                        skill_total,
                    )
                },
            )?;
            state.hosts.insert(host_id, owned);
            completed += 1;
            report(
                &progress,
                "skills",
                &format!("{} 的 Skills Pack 已校验", host.label),
                completed,
                total,
            );
        }
        let schema = BUNDLED_SKILLS
            .get_file("my-llm-wiki/assets/schema.md")
            .ok_or_else(|| SetupError::InvalidState("bundled schema.md is missing".into()))?;
        report(
            &progress,
            "wiki",
            "正在校验 Wiki 与 RAW 目录",
            completed,
            total,
        );
        wiki::ensure(&state.wiki_path, &self.registry_path, schema.contents())?;
        completed += 1;
        report(&progress, "wiki", "Wiki 状态已校验", completed, total);
        for id in pack_ids {
            self.ensure_running()?;
            let installed = if let Some(existing) = state.packs.get(&id) {
                if pack::check_owned_pack(existing).is_ok() {
                    None
                } else {
                    Some(pack::install_pack_with_progress(
                        &self.suite_home,
                        &existing.artifact,
                        &self.cancel_check(),
                        |event| {
                            report_pack_progress(
                                &progress,
                                "pack",
                                &format!("{id} 能力包"),
                                completed,
                                total,
                                event,
                            )
                        },
                    )?)
                }
            } else {
                report(
                    &progress,
                    "pack",
                    &format!("正在获取 {id} 能力包清单"),
                    completed,
                    total,
                );
                let manifest = pack::fetch_manifest(&self.current_manifest_sources)?;
                self.require_current_distribution(&manifest)?;
                Some(pack::install_pack_with_progress(
                    &self.suite_home,
                    pack::select_artifact(&manifest, &id)?,
                    &self.cancel_check(),
                    |event| {
                        report_pack_progress(
                            &progress,
                            "pack",
                            &format!("{id} 能力包"),
                            completed,
                            total,
                            event,
                        )
                    },
                )?)
            };
            if let Some(installed) = installed {
                state.packs.insert(id.clone(), installed);
            }
            completed += 1;
            report(
                &progress,
                "pack",
                &format!("{id} 能力包已校验"),
                completed,
                total,
            );
        }
        self.save_state(&state)?;
        self.prune_active_packs(&state);
        self.status()
    }

    pub fn ensure_pack(&self, id: &str) -> Result<SetupResult> {
        self.ensure_pack_with_progress(id, |_| {})
    }

    pub fn ensure_pack_with_progress(
        &self,
        id: &str,
        progress: impl Fn(SetupProgress),
    ) -> Result<SetupResult> {
        report(&progress, "pack", &format!("正在准备 {id} 能力包"), 0, 1);
        let _lock = self.lock()?;
        let mut state = self
            .load_state()?
            .ok_or_else(|| SetupError::InvalidState("setup has not been completed".into()))?;
        let manifest = pack::fetch_manifest(&self.current_manifest_sources)?;
        self.require_current_distribution(&manifest)?;
        let artifact = pack::select_artifact(&manifest, id)?;
        let installed = pack::install_pack_with_progress(
            &self.suite_home,
            artifact,
            &self.cancel_check(),
            |event| report_pack_progress(&progress, "pack", &format!("{id} 能力包"), 0, 1, event),
        )?;
        state.packs.insert(id.to_owned(), installed);
        if id == "toolchain-base" {
            state.official_toolchain = true;
        }
        self.save_state(&state)?;
        self.prune_active_packs(&state);
        report(&progress, "pack", &format!("{id} 能力包已就绪"), 1, 1);
        self.status()
    }

    pub fn prepare_asr_zh(&self) -> Result<SetupResult> {
        self.prepare_asr_zh_with_progress(|_| {})
    }

    /// Install the isolated ASR runtime, download both Chinese transcription
    /// models, then load them from local paths before publishing readiness.
    pub fn prepare_asr_zh_with_progress(
        &self,
        progress: impl Fn(SetupProgress),
    ) -> Result<SetupResult> {
        self.ensure_pack_with_progress("asr-zh", |event| {
            let runtime_ready = event.total > 0 && event.current >= event.total;
            progress(SetupProgress {
                phase: "asr-runtime".into(),
                message: event.message,
                current: u32::from(runtime_ready),
                total: 4,
                detail_percent: event.detail_percent,
            });
        })?;

        let _lock = self.lock()?;
        if self.asr_zh_model_cache_status().ready {
            report(
                &progress,
                "asr-models",
                "中文视频转写运行环境与模型均已就绪",
                4,
                4,
            );
            return self.status();
        }
        let state = self
            .load_state()?
            .ok_or_else(|| SetupError::InvalidState("setup has not been completed".into()))?;
        let pack = state
            .packs
            .get("asr-zh")
            .ok_or_else(|| SetupError::InvalidState("asr-zh pack is not activated".into()))?;
        let (python, environment) = pack::resolve_python_profile(pack, "asr-zh")?;
        let script = self.asr_zh_prefetch_script(&state)?;
        let model_root = self.asr_zh_model_root();

        report(
            &progress,
            "asr-models",
            "正在下载语音分段模型 fsmn-vad",
            1,
            4,
        );
        run_model_download(
            &python,
            &environment,
            &script,
            &model_root,
            "fsmn-vad",
            "语音分段模型 fsmn-vad",
            1,
            &progress,
        )?;
        report(&progress, "asr-models", "fsmn-vad 已下载", 2, 4);

        report(
            &progress,
            "asr-models",
            "正在下载中文转写模型 SenseVoiceSmall",
            2,
            4,
        );
        run_model_download(
            &python,
            &environment,
            &script,
            &model_root,
            "sensevoice",
            "中文转写模型 SenseVoiceSmall",
            2,
            &progress,
        )?;
        report(&progress, "asr-models", "SenseVoiceSmall 已下载", 3, 4);

        report(
            &progress,
            "asr-models",
            "正在离线加载并验证两个转写模型",
            3,
            4,
        );
        run_model_stage(
            &python,
            &environment,
            &script,
            &model_root,
            "verify",
            |event| {
                if let ModelStageEvent::Verify { index, count } = event {
                    progress(SetupProgress {
                        phase: "asr-models".into(),
                        message: format!("正在离线加载并验证第 {index} / {count} 个转写模型"),
                        current: 3,
                        total: 4,
                        detail_percent: Some(step_percent(index.saturating_sub(1), count)),
                    });
                }
            },
        )?;
        if !self.asr_zh_model_cache_status().ready {
            return Err(SetupError::Probe {
                pack: "asr-zh models".into(),
                detail: "offline verification finished without a readiness marker".into(),
            });
        }
        report(
            &progress,
            "asr-models",
            "中文视频转写运行环境与模型均已就绪",
            4,
            4,
        );
        self.status()
    }

    pub fn update(&self, check_only: bool) -> Result<UpdateResult> {
        let manifest = pack::fetch_manifest(&self.latest_manifest_sources)?;
        let state = self.load_state()?;
        let current = state
            .as_ref()
            .map(|state| state.distribution_version.clone())
            .unwrap_or_else(|| DISTRIBUTION_VERSION.to_owned());
        let latest = semver::Version::parse(&manifest.distribution_version)
            .map_err(|err| SetupError::InvalidManifest(err.to_string()))?;
        let running = semver::Version::parse(DISTRIBUTION_VERSION)
            .map_err(|err| SetupError::InvalidState(err.to_string()))?;
        if latest > running {
            return Ok(UpdateResult {
                state: if check_only {
                    UpdateState::Available
                } else {
                    UpdateState::RestartRequired
                },
                current_version: current,
                latest_version: manifest.distribution_version,
                restart_required: !check_only,
            });
        }
        if latest < running {
            return Ok(UpdateResult {
                state: UpdateState::UpToDate,
                current_version: current,
                latest_version: manifest.distribution_version,
                restart_required: false,
            });
        }
        if check_only {
            return Ok(UpdateResult {
                state: if current == manifest.distribution_version {
                    UpdateState::UpToDate
                } else {
                    UpdateState::Available
                },
                current_version: current,
                latest_version: manifest.distribution_version,
                restart_required: false,
            });
        }

        let _lock = self.lock()?;
        let mut state =
            state.ok_or_else(|| SetupError::InvalidState("setup has not been completed".into()))?;
        self.install_cli(&mut state)?;
        let definitions: BTreeMap<_, _> = host_definitions(&self.home)
            .into_iter()
            .map(|host| (host.id.clone(), host))
            .collect();
        let mut replacements = BTreeSet::new();
        let mut backups = Vec::new();
        let canonical = self.install_skills(&state.install_id, |_, _, _| {})?;
        for host_id in state.hosts.keys().cloned().collect::<Vec<_>>() {
            let host = definitions
                .get(&host_id)
                .ok_or_else(|| SetupError::UnknownHost(host_id.clone()))?;
            for slug in bundled_skill_slugs() {
                let path = host.skills_dir.join(slug);
                if destination_state(&path, Some(&state.install_id)) == DestinationState::Foreign {
                    return Err(SetupError::OwnershipLost(path));
                }
            }
            let owned = self.link_host(
                host,
                &state.install_id,
                &canonical,
                &mut replacements,
                &mut backups,
                |_, _, _| {},
            )?;
            state.hosts.insert(host_id, owned);
        }
        let pack_ids: Vec<_> = state.packs.keys().cloned().collect();
        for id in pack_ids {
            let artifact = pack::select_artifact(&manifest, &id)?;
            let installed = pack::install_pack(&self.suite_home, artifact)?;
            state.packs.insert(id, installed);
        }
        state.distribution_version = manifest.distribution_version.clone();
        state.skills_pack_version = manifest.skills_pack_version.clone();
        self.save_state(&state)?;
        self.prune_active_packs(&state);
        Ok(UpdateResult {
            state: UpdateState::Updated,
            current_version: current,
            latest_version: manifest.distribution_version,
            restart_required: false,
        })
    }

    pub fn uninstall(&self, hosts: &BTreeSet<String>, all: bool) -> Result<SetupResult> {
        if hosts.is_empty() && !all {
            return Err(SetupError::UninstallSelectionRequired);
        }
        if !hosts.is_empty() && all {
            return Err(SetupError::ConflictingUninstallSelection);
        }
        let _lock = self.lock()?;
        let mut state = self
            .load_state()?
            .ok_or_else(|| SetupError::InvalidState("setup has not been completed".into()))?;
        let selected: Vec<String> = if all {
            state.hosts.keys().cloned().collect()
        } else {
            hosts.iter().cloned().collect()
        };
        for host_id in &selected {
            let owned = state
                .hosts
                .get(host_id)
                .ok_or_else(|| SetupError::UnknownHost(host_id.clone()))?;
            for skill in owned.skills.values() {
                if destination_state(&skill.path, Some(&state.install_id))
                    != DestinationState::Owned
                {
                    return Err(SetupError::OwnershipLost(skill.path.clone()));
                }
            }
        }
        if all {
            for (id, pack) in &state.packs {
                let expected_root = self.suite_home.join("packs").join(id);
                if !pack.path.starts_with(expected_root.join("versions")) {
                    return Err(SetupError::OwnershipLost(pack.path.clone()));
                }
            }
            if let Some(cli) = state.cli_path.as_ref()
                && cli != &self.cli_path
            {
                return Err(SetupError::OwnershipLost(cli.clone()));
            }
        }
        for host_id in selected {
            let owned = state
                .hosts
                .get(&host_id)
                .ok_or_else(|| SetupError::UnknownHost(host_id.clone()))?;
            for skill in owned.skills.values() {
                remove_installed_skill(&skill.path)?;
            }
            state.hosts.remove(&host_id);
        }
        if all {
            for id in state.packs.keys().cloned().collect::<Vec<_>>() {
                self.remove_owned_pack(&mut state, &id)?;
            }
            // The installed Skills Pack goes only once every host that linked
            // to it has been detached above.
            let skills_dir = self.canonical_skills_dir();
            if skills_dir.is_dir() {
                fs::remove_dir_all(&skills_dir)
                    .map_err(|error| SetupError::io(&skills_dir, error))?;
            }
            if let Some(cli) = state.cli_path.take()
                && cli.exists()
            {
                fs::remove_file(&cli).map_err(|error| SetupError::io(&cli, error))?;
            }
        }
        if state.hosts.is_empty() && state.packs.is_empty() && state.cli_path.is_none() {
            if self.state_path.exists() {
                fs::remove_file(&self.state_path)
                    .map_err(|err| SetupError::io(&self.state_path, err))?;
            }
        } else {
            self.save_state(&state)?;
        }
        self.status()
    }

    pub fn provider_config(&self) -> Result<ProviderConfig> {
        if !self.providers_path.exists() {
            return Ok(ProviderConfig::default());
        }
        let data = fs::read(&self.providers_path)
            .map_err(|error| SetupError::io(&self.providers_path, error))?;
        let config: ProviderConfig = serde_json::from_slice(&data)
            .map_err(|error| SetupError::json(&self.providers_path, error))?;
        validate_provider_config(&config)?;
        Ok(config)
    }

    pub fn save_provider_config(&self, config: &ProviderConfig) -> Result<ProviderConfig> {
        validate_provider_config(config)?;
        let _lock = self.lock()?;
        let data = serde_json::to_vec_pretty(config)
            .map_err(|error| SetupError::json(&self.providers_path, error))?;
        wiki::atomic_write(&self.providers_path, &data)?;
        Ok(config.clone())
    }

    /// Expand the bundled Skills Pack into the install root.
    ///
    /// This is the only copy written to disk; hosts get links to it. One copy
    /// instead of one per host also means a Skills Pack that moves with the
    /// install root when the user puts it on another volume.
    fn install_skills(
        &self,
        install_id: &str,
        progress: impl Fn(&str, u32, u32),
    ) -> Result<BTreeMap<String, CanonicalSkill>> {
        let skills_dir = self.canonical_skills_dir();
        fs::create_dir_all(&skills_dir).map_err(|err| SetupError::io(&skills_dir, err))?;
        let mut installed = BTreeMap::new();
        let slugs = bundled_skill_slugs();
        let total = slugs.len() as u32;
        for (index, slug) in slugs.into_iter().enumerate() {
            progress(&slug, index as u32, total);
            let source = BUNDLED_SKILLS.get_dir(&slug).ok_or_else(|| {
                SetupError::InvalidState(format!("bundled skill missing: {slug}"))
            })?;
            let digest = digest_dir(source);
            let destination = skills_dir.join(&slug);
            let stage =
                skills_dir.join(format!(".my-llm-wiki-stage-{}-{slug}", std::process::id()));
            stage_skill(source, &stage, install_id, &slug, &digest)?;
            swap_dir(&stage, &destination)?;
            installed.insert(
                slug,
                CanonicalSkill {
                    path: destination,
                    digest,
                },
            );
        }
        progress("", total, total);
        Ok(installed)
    }

    /// Point one host's skills directory at the installed Skills Pack.
    ///
    /// A destination that already links to the right place is left alone, so
    /// repeated setup and repair runs are quiet. Anything else the host holds
    /// is treated exactly as before: ours to replace, or foreign and requiring
    /// the caller's explicit per-path authority before it is backed up.
    fn link_host(
        &self,
        host: &HostDefinition,
        install_id: &str,
        canonical: &BTreeMap<String, CanonicalSkill>,
        replacements: &mut BTreeSet<PathBuf>,
        backups: &mut Vec<PathBuf>,
        progress: impl Fn(&str, u32, u32),
    ) -> Result<OwnedHost> {
        fs::create_dir_all(&host.skills_dir)
            .map_err(|err| SetupError::io(&host.skills_dir, err))?;
        let mut skills = BTreeMap::new();
        let total = canonical.len() as u32;
        for (index, (slug, skill)) in canonical.iter().enumerate() {
            progress(slug, index as u32, total);
            let destination = host.skills_dir.join(slug);
            let mode = if link::links_to(&destination, &skill.path) {
                SkillInstallMode::Link
            } else {
                self.attach_skill(host, install_id, slug, skill, replacements, backups)?
            };
            skills.insert(
                slug.clone(),
                OwnedSkill {
                    path: destination,
                    digest: skill.digest.clone(),
                    mode,
                },
            );
        }
        progress("", total, total);
        Ok(OwnedHost {
            skills_dir: host.skills_dir.clone(),
            skills,
        })
    }

    /// Clear one host destination and attach the Skills Pack to it.
    fn attach_skill(
        &self,
        host: &HostDefinition,
        install_id: &str,
        slug: &str,
        skill: &CanonicalSkill,
        replacements: &mut BTreeSet<PathBuf>,
        backups: &mut Vec<PathBuf>,
    ) -> Result<SkillInstallMode> {
        let destination = host.skills_dir.join(slug);
        let current = destination_state(&destination, Some(install_id));
        if current == DestinationState::Foreign && !replacements.remove(&destination) {
            return Err(SetupError::ForeignDestination(destination));
        }
        // `symlink_metadata` rather than `exists`, so a link left dangling by a
        // detached volume is cleared instead of read straight through.
        if fs::symlink_metadata(&destination).is_ok() {
            if current == DestinationState::Foreign {
                let backup = backup_path(&host.skills_dir, slug);
                if let Some(parent) = backup.parent() {
                    fs::create_dir_all(parent).map_err(|err| SetupError::io(parent, err))?;
                }
                fs::rename(&destination, &backup)
                    .map_err(|err| SetupError::io(&destination, err))?;
                backups.push(backup);
            } else {
                remove_installed_skill(&destination)?;
            }
        }
        if link::create_dir_link(&destination, &skill.path).is_ok() {
            return Ok(SkillInstallMode::Link);
        }
        // A destination that cannot hold a link — a host directory on a
        // filesystem without them — still gets a working Skills Pack; it just
        // does not get to share the installed one.
        let source = BUNDLED_SKILLS
            .get_dir(slug)
            .ok_or_else(|| SetupError::InvalidState(format!("bundled skill missing: {slug}")))?;
        let stage = host
            .skills_dir
            .join(format!(".my-llm-wiki-stage-{}-{slug}", std::process::id()));
        stage_skill(source, &stage, install_id, slug, &skill.digest)?;
        swap_dir(&stage, &destination)?;
        Ok(SkillInstallMode::Copy)
    }

    fn install_cli(&self, state: &mut SetupState) -> Result<()> {
        let Some(source) = self.cli_source.as_ref() else {
            return Ok(());
        };
        let data = fs::read(source).map_err(|error| SetupError::io(source, error))?;
        wiki::atomic_write(&self.cli_path, &data)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            fs::set_permissions(&self.cli_path, fs::Permissions::from_mode(0o755))
                .map_err(|error| SetupError::io(&self.cli_path, error))?;
        }
        state.cli_path = Some(self.cli_path.clone());
        Ok(())
    }

    fn remove_owned_pack(&self, state: &mut SetupState, id: &str) -> Result<()> {
        let Some(pack) = state.packs.get(id) else {
            return Ok(());
        };
        let expected_root = self.suite_home.join("packs").join(id);
        if !pack.path.starts_with(expected_root.join("versions")) {
            return Err(SetupError::OwnershipLost(pack.path.clone()));
        }
        if expected_root.exists() {
            fs::remove_dir_all(&expected_root)
                .map_err(|error| SetupError::io(&expected_root, error))?;
        }
        state.packs.remove(id);
        Ok(())
    }

    fn prune_active_packs(&self, state: &SetupState) {
        for pack in state.packs.values() {
            if let Err(error) = pack::prune_pack_versions(&self.suite_home, pack) {
                eprintln!("my-llm-wiki: unable to prune old pack version: {error}");
            }
        }
    }

    fn host_results(&self, state: &SetupState) -> BTreeMap<String, HostResult> {
        // Linked hosts all read the same tree, so hash it once here instead of
        // once per host per skill.
        let skills_dir = self.canonical_skills_dir();
        let canonical: BTreeMap<String, Option<String>> = bundled_skill_slugs()
            .into_iter()
            .map(|slug| {
                let digest = digest_path(&skills_dir.join(&slug));
                (slug, digest)
            })
            .collect();
        state
            .hosts
            .iter()
            .map(|(id, host)| {
                let installed: Vec<_> = host.skills.keys().cloned().collect();
                let healthy = host.skills.iter().all(|(slug, skill)| {
                    let content = match skill.mode {
                        // A link is healthy only when it still resolves to the
                        // installed copy: one that drifted elsewhere could pass
                        // an ownership check on whatever it now points at.
                        SkillInstallMode::Link => {
                            link::links_to(&skill.path, &skills_dir.join(slug))
                                && canonical.get(slug).and_then(Option::as_deref)
                                    == Some(skill.digest.as_str())
                        }
                        SkillInstallMode::Copy => {
                            !link::is_dir_link(&skill.path)
                                && digest_path(&skill.path).as_deref()
                                    == Some(skill.digest.as_str())
                        }
                    };
                    content
                        && destination_state(&skill.path, Some(&state.install_id))
                            == DestinationState::Owned
                });
                (
                    id.clone(),
                    HostResult {
                        skills_dir: host.skills_dir.clone(),
                        installed,
                        healthy,
                    },
                )
            })
            .collect()
    }

    fn toolchain_status(&self, state: Option<&SetupState>) -> PackStatus {
        let pack = state.and_then(|state| state.packs.get("toolchain-base"));
        PackStatus {
            id: "toolchain-base".into(),
            version: pack.map(|pack| pack.version.clone()),
            installed: pack.is_some(),
            healthy: pack.is_some_and(|pack| pack::check_owned_pack(pack).is_ok()),
        }
    }

    fn pack_statuses(&self, state: &SetupState) -> BTreeMap<String, PackStatus> {
        state
            .packs
            .iter()
            .map(|(id, pack)| {
                (
                    id.clone(),
                    PackStatus {
                        id: id.clone(),
                        version: Some(pack.version.clone()),
                        installed: true,
                        healthy: pack::check_owned_pack(pack).is_ok(),
                    },
                )
            })
            .collect()
    }

    fn require_current_distribution(&self, manifest: &DistributionManifest) -> Result<()> {
        if manifest.distribution_version != DISTRIBUTION_VERSION {
            return Err(SetupError::DistributionMismatch {
                expected: DISTRIBUTION_VERSION.to_owned(),
                actual: manifest.distribution_version.clone(),
            });
        }
        Ok(())
    }

    fn lock(&self) -> Result<OperationLock> {
        fs::create_dir_all(&self.suite_home)
            .map_err(|err| SetupError::io(&self.suite_home, err))?;
        let path = self.suite_home.join("setup.lock");
        let file = File::options()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(&path)
            .map_err(|err| SetupError::io(&path, err))?;
        file.try_lock_exclusive().map_err(|_| SetupError::Locked)?;
        Ok(OperationLock(file))
    }

    fn load_state(&self) -> Result<Option<SetupState>> {
        if !self.state_path.exists() {
            return Ok(None);
        }
        let bytes =
            fs::read(&self.state_path).map_err(|err| SetupError::io(&self.state_path, err))?;
        let state: SetupState = serde_json::from_slice(&bytes)
            .map_err(|err| SetupError::json(&self.state_path, err))?;
        if state.schema != STATE_SCHEMA {
            return Err(SetupError::InvalidState(format!(
                "unsupported schema {}",
                state.schema
            )));
        }
        Ok(Some(state))
    }

    fn save_state(&self, state: &SetupState) -> Result<()> {
        let data = serde_json::to_vec_pretty(state)
            .map_err(|err| SetupError::json(&self.state_path, err))?;
        wiki::atomic_write(&self.state_path, &data)
    }

    fn asr_zh_model_root(&self) -> PathBuf {
        self.suite_home.join("models").join(ASR_ZH_MODEL_CACHE_ID)
    }

    fn asr_zh_model_cache_status(&self) -> ModelCacheStatus {
        let root = self.asr_zh_model_root();
        let marker = root.join(ASR_ZH_MODEL_MARKER);
        let ready = fs::read(&marker)
            .ok()
            .and_then(|data| serde_json::from_slice::<AsrZhModelMarker>(&data).ok())
            .is_some_and(|value| {
                value.schema == 1
                    && value.models.get("fsmn-vad").is_some_and(|model| {
                        model.id == ASR_ZH_VAD_ID
                            && model.directory == "fsmn-vad"
                            && root.join(&model.directory).is_dir()
                    })
                    && value.models.get("sensevoice").is_some_and(|model| {
                        model.id == ASR_ZH_SENSEVOICE_ID
                            && model.directory == "SenseVoiceSmall"
                            && root.join(&model.directory).is_dir()
                    })
            });
        ModelCacheStatus {
            id: ASR_ZH_MODEL_CACHE_ID.into(),
            path: root,
            ready,
        }
    }

    fn asr_zh_prefetch_script(&self, state: &SetupState) -> Result<PathBuf> {
        let script = state
            .hosts
            .values()
            .find_map(|host| host.skills.get("my-llm-wiki-video"))
            .map(|skill| skill.path.join("scripts").join("prefetch_asr_zh.py"))
            .ok_or_else(|| {
                SetupError::InvalidState("installed video Skill is unavailable".into())
            })?;
        if !script.is_file() {
            return Err(SetupError::InvalidState(format!(
                "ASR model prefetch script is missing: {}",
                script.display()
            )));
        }
        Ok(script)
    }
}

#[derive(serde::Deserialize)]
struct AsrZhModelMarker {
    schema: u32,
    models: BTreeMap<String, AsrZhModelMarkerEntry>,
}

#[derive(serde::Deserialize)]
struct AsrZhModelMarkerEntry {
    id: String,
    directory: String,
}

/// Run one download stage, turning observed bytes into the same
/// message-plus-percent shape pack downloads already report. Without this the
/// Setup card sat on an indeterminate spinner for the ~1GB SenseVoiceSmall
/// download, which is indistinguishable from a stalled install.
#[allow(clippy::too_many_arguments)]
fn run_model_download(
    python: &[String],
    environment: &BTreeMap<String, String>,
    script: &Path,
    model_root: &Path,
    stage: &str,
    label: &str,
    step: u32,
    progress: &impl Fn(SetupProgress),
) -> Result<()> {
    run_model_stage(python, environment, script, model_root, stage, |event| {
        if let ModelStageEvent::Download {
            downloaded_bytes,
            total_bytes,
        } = event
        {
            progress(SetupProgress {
                phase: "asr-models".into(),
                message: format!(
                    "正在下载{label} · {}",
                    format_transfer(downloaded_bytes, total_bytes)
                ),
                current: step,
                total: 4,
                detail_percent: transfer_percent(downloaded_bytes, total_bytes),
            });
        }
    })
}

/// Cap an in-flight download below 100% so the bar only fills when the stage
/// actually finished: the repository size is an estimate, and a bar that sits
/// at 100% while work continues is the same lie as no bar at all.
fn transfer_percent(downloaded: u64, total: Option<u64>) -> Option<u8> {
    let total = total.filter(|value| *value > 0)?;
    Some((downloaded.saturating_mul(100) / total).min(99) as u8)
}

fn format_transfer(downloaded: u64, total: Option<u64>) -> String {
    match total.filter(|value| *value > 0) {
        Some(total) => format!("{} / {}", format_bytes(downloaded), format_bytes(total)),
        None => format!("已下载 {}", format_bytes(downloaded)),
    }
}

fn format_bytes(value: u64) -> String {
    const MB: f64 = 1024.0 * 1024.0;
    let megabytes = value as f64 / MB;
    if megabytes >= 1024.0 {
        format!("{:.2} GB", megabytes / 1024.0)
    } else {
        format!("{megabytes:.1} MB")
    }
}

/// What the prefetch script reports while a stage runs. Downloading a gigabyte
/// of models is the longest single wait in the whole install, so the script
/// streams these instead of staying silent until it exits.
#[derive(serde::Deserialize)]
#[serde(tag = "event", rename_all = "snake_case")]
enum ModelStageEvent {
    Download {
        downloaded_bytes: u64,
        total_bytes: Option<u64>,
    },
    Verify {
        index: u32,
        count: u32,
    },
}

fn run_model_stage(
    python: &[String],
    environment: &BTreeMap<String, String>,
    script: &Path,
    model_root: &Path,
    stage: &str,
    mut on_event: impl FnMut(ModelStageEvent),
) -> Result<()> {
    let executable = python
        .first()
        .ok_or_else(|| SetupError::InvalidState("asr-zh Python argv is empty".into()))?;
    let mut command = Command::new(executable);
    command
        .args(&python[1..])
        .arg(script)
        .arg("--stage")
        .arg(stage)
        .arg("--model-root")
        .arg(model_root)
        .envs(environment)
        // Asked for through the environment rather than a CLI flag: an older
        // installed copy of the skill script ignores it instead of aborting on
        // an unknown argument.
        .env(ASR_PROGRESS_ENV, "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    process::hide_console(&mut command);
    let mut child = command.spawn().map_err(|error| SetupError::Probe {
        pack: "asr-zh models".into(),
        detail: format!("cannot start model {stage} stage: {error}"),
    })?;

    // stderr carries the tqdm noise and any traceback: drain it on its own
    // thread, otherwise a full pipe buffer would block the child forever while
    // this thread waits for the next progress line.
    let drain = child.stderr.take().map(|mut stderr| {
        std::thread::spawn(move || {
            let mut captured = String::new();
            let _ = stderr.read_to_string(&mut captured);
            captured
        })
    });
    if let Some(stdout) = child.stdout.take() {
        for line in BufReader::new(stdout)
            .lines()
            .map_while(std::result::Result::ok)
        {
            if let Ok(event) = serde_json::from_str::<ModelStageEvent>(&line) {
                on_event(event);
            }
        }
    }
    let status = child.wait().map_err(|error| SetupError::Probe {
        pack: "asr-zh models".into(),
        detail: format!("cannot wait for model {stage} stage: {error}"),
    })?;
    let stderr = drain
        .and_then(|handle| handle.join().ok())
        .unwrap_or_default();
    if status.success() {
        return Ok(());
    }
    let detail = stderr.trim();
    Err(SetupError::Probe {
        pack: "asr-zh models".into(),
        detail: if detail.is_empty() {
            format!("model {stage} stage exited with {status}")
        } else {
            format!("model {stage} stage failed: {}", tail_chars(detail, 4000))
        },
    })
}

fn tail_chars(value: &str, limit: usize) -> String {
    let mut characters: Vec<_> = value.chars().rev().take(limit).collect();
    characters.reverse();
    characters.into_iter().collect()
}

struct OperationLock(File);

impl Drop for OperationLock {
    fn drop(&mut self) {
        let _ = self.0.unlock();
    }
}

fn host_definitions(home: &Path) -> Vec<HostDefinition> {
    [
        ("codex", "Codex", ".codex"),
        ("claude", "Claude", ".claude"),
        ("hermes", "Hermes", ".hermes"),
        ("agents", "Agent Skills", ".agents"),
        ("workbuddy", "WorkBuddy", ".workbuddy"),
        ("openclaw", "OpenClaw", ".openclaw"),
    ]
    .into_iter()
    .map(|(id, label, relative)| HostDefinition {
        id: id.into(),
        label: label.into(),
        detect_dir: home.join(relative),
        skills_dir: home.join(relative).join("skills"),
    })
    .collect()
}

fn host_definitions_by_id(home: &Path) -> BTreeMap<String, HostDefinition> {
    host_definitions(home)
        .into_iter()
        .map(|host| (host.id.clone(), host))
        .collect()
}

/// Check replacement authority against exactly the foreign destinations the
/// selected hosts hold right now, and hand back the authority to spend.
///
/// Both directions matter. A foreign destination without authority stops the
/// run before anything is written, and authority for a path that is not
/// actually foreign is refused rather than carried into the run, where it would
/// sit waiting to authorize a destination that turned foreign in between.
fn authorize_replacements(
    definitions: &BTreeMap<String, HostDefinition>,
    hosts: &BTreeSet<String>,
    install_id: &str,
    replace: &BTreeSet<PathBuf>,
) -> Result<BTreeSet<PathBuf>> {
    let mut expected = BTreeSet::new();
    for host_id in hosts {
        let host = definitions
            .get(host_id)
            .ok_or_else(|| SetupError::UnknownHost(host_id.clone()))?;
        for slug in bundled_skill_slugs() {
            let path = host.skills_dir.join(slug);
            if destination_state(&path, Some(install_id)) == DestinationState::Foreign {
                expected.insert(path);
            }
        }
    }
    if let Some(missing) = expected.difference(replace).next() {
        return Err(SetupError::ForeignDestination(missing.clone()));
    }
    if let Some(unused) = replace.difference(&expected).next() {
        return Err(SetupError::UnusedReplacement(unused.clone()));
    }
    Ok(replace.clone())
}

fn bundled_skill_slugs() -> Vec<String> {
    let mut slugs: Vec<_> = BUNDLED_SKILLS
        .dirs()
        .filter(|dir| {
            dir.files().any(|file| {
                file.path().file_name().and_then(|name| name.to_str()) == Some("SKILL.md")
            })
        })
        .filter_map(|dir| dir.path().file_name()?.to_str().map(str::to_owned))
        .collect();
    slugs.sort();
    slugs
}

fn destination_state(path: &Path, install_id: Option<&str>) -> DestinationState {
    if !path.exists() {
        return DestinationState::Absent;
    }
    let marker_path = path.join(OWNER_FILE);
    let marker = fs::read(&marker_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<OwnershipMarker>(&bytes).ok());
    if marker.is_some_and(|marker| {
        marker.schema == OWNER_SCHEMA && Some(marker.install_id.as_str()) == install_id
    }) {
        DestinationState::Owned
    } else {
        DestinationState::Foreign
    }
}

/// One skill as installed under the install root, which host destinations link
/// to and health checks compare against.
struct CanonicalSkill {
    path: PathBuf,
    digest: String,
}

/// Write a skill and its ownership marker into a staging directory.
fn stage_skill(
    source: &Dir<'_>,
    stage: &Path,
    install_id: &str,
    slug: &str,
    digest: &str,
) -> Result<()> {
    if stage.exists() {
        fs::remove_dir_all(stage).map_err(|err| SetupError::io(stage, err))?;
    }
    write_dir(source, stage)?;
    let marker = OwnershipMarker {
        schema: OWNER_SCHEMA,
        install_id: install_id.to_owned(),
        artifact: slug.to_owned(),
        version: DISTRIBUTION_VERSION.to_owned(),
        digest: digest.to_owned(),
    };
    let marker_path = stage.join(OWNER_FILE);
    let marker_data =
        serde_json::to_vec_pretty(&marker).map_err(|err| SetupError::json(&marker_path, err))?;
    fs::write(&marker_path, marker_data).map_err(|err| SetupError::io(&marker_path, err))
}

/// Move a staged directory into place, keeping whatever was there until the
/// move succeeds so a failure leaves the destination as it was found.
fn swap_dir(stage: &Path, destination: &Path) -> Result<()> {
    let previous = if fs::symlink_metadata(destination).is_ok() {
        let name = destination
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("skill");
        let previous =
            destination.with_file_name(format!(".my-llm-wiki-old-{}-{name}", std::process::id()));
        if fs::symlink_metadata(&previous).is_ok() {
            remove_installed_skill(&previous)?;
        }
        fs::rename(destination, &previous).map_err(|err| SetupError::io(destination, err))?;
        Some(previous)
    } else {
        None
    };
    if let Err(err) = fs::rename(stage, destination) {
        if let Some(previous) = previous.as_ref() {
            let _ = fs::rename(previous, destination);
        }
        return Err(SetupError::io(stage, err));
    }
    if let Some(previous) = previous {
        remove_installed_skill(&previous)?;
    }
    Ok(())
}

/// Remove a skill destination we own.
///
/// A link is detached rather than followed: the target holds the one installed
/// copy every other host is also using, so recursing through it would delete
/// the installation instead of one reference to it.
fn remove_installed_skill(path: &Path) -> Result<()> {
    if link::is_dir_link(path) {
        link::remove_dir_link(path).map_err(|err| SetupError::io(path, err))
    } else {
        fs::remove_dir_all(path).map_err(|err| SetupError::io(path, err))
    }
}

fn nearest_existing_dir(path: &Path) -> Option<&Path> {
    let mut candidate = Some(path);
    while let Some(directory) = candidate {
        if directory.is_dir() {
            return Some(directory);
        }
        candidate = directory.parent();
    }
    None
}

/// Whether a directory accepts new entries, answered by trying rather than by
/// reading permission bits, which do not tell the whole story on Windows.
fn is_writable_dir(path: &Path) -> bool {
    let probe = path.join(format!(".my-llm-wiki-probe-{}", std::process::id()));
    match fs::create_dir(&probe) {
        Ok(()) => {
            let _ = fs::remove_dir(&probe);
            true
        }
        Err(_) => false,
    }
}

fn write_dir(source: &Dir<'_>, destination: &Path) -> Result<()> {
    fs::create_dir_all(destination).map_err(|err| SetupError::io(destination, err))?;
    for dir in source.dirs() {
        let name = dir
            .path()
            .file_name()
            .ok_or_else(|| SetupError::InvalidState("embedded directory has no name".into()))?;
        write_dir(dir, &destination.join(name))?;
    }
    for file in source.files() {
        let name = file
            .path()
            .file_name()
            .ok_or_else(|| SetupError::InvalidState("embedded file has no name".into()))?;
        let path = destination.join(name);
        fs::write(&path, file.contents()).map_err(|err| SetupError::io(&path, err))?;
    }
    Ok(())
}

fn digest_dir(dir: &Dir<'_>) -> String {
    let mut rows = Vec::new();
    collect_embedded_files(dir, dir.path(), &mut rows);
    rows.sort_by(|left, right| left.0.cmp(&right.0));
    let mut hash = Sha256::new();
    for (path, contents) in rows {
        hash.update(path.as_bytes());
        hash.update([0]);
        hash.update(contents);
        hash.update([0]);
    }
    format!("{:x}", hash.finalize())
}

fn collect_embedded_files<'a>(dir: &'a Dir<'a>, root: &Path, rows: &mut Vec<(String, &'a [u8])>) {
    rows.extend(dir.files().map(|file| {
        (
            file.path()
                .strip_prefix(root)
                .unwrap_or(file.path())
                .to_string_lossy()
                .replace('\\', "/"),
            file.contents(),
        )
    }));
    for child in dir.dirs() {
        collect_embedded_files(child, root, rows);
    }
}

fn digest_path(path: &Path) -> Option<String> {
    let mut files = Vec::new();
    collect_disk_files(path, path, &mut files).ok()?;
    files.sort_by(|left, right| left.0.cmp(&right.0));
    let mut hash = Sha256::new();
    for (relative, contents) in files {
        hash.update(relative.as_bytes());
        hash.update([0]);
        hash.update(contents);
        hash.update([0]);
    }
    Some(format!("{:x}", hash.finalize()))
}

fn collect_disk_files(
    root: &Path,
    current: &Path,
    files: &mut Vec<(String, Vec<u8>)>,
) -> std::io::Result<()> {
    for entry in fs::read_dir(current)? {
        let entry = entry?;
        let path = entry.path();
        if path.is_dir() {
            collect_disk_files(root, &path, files)?;
        } else if path.file_name().and_then(|name| name.to_str()) != Some(OWNER_FILE) {
            let relative = path
                .strip_prefix(root)
                .unwrap_or(&path)
                .to_string_lossy()
                .replace('\\', "/");
            files.push((relative, fs::read(&path)?));
        }
    }
    Ok(())
}

fn backup_path(skills_dir: &Path, slug: &str) -> PathBuf {
    let mut candidate = skills_dir
        .join(".my-llm-wiki-backups")
        .join(format!("{slug}-{}", std::process::id()));
    let mut suffix = 1;
    while candidate.exists() {
        candidate = skills_dir
            .join(".my-llm-wiki-backups")
            .join(format!("{slug}-{}-{suffix}", std::process::id()));
        suffix += 1;
    }
    candidate
}

fn new_install_id() -> String {
    let seed = format!(
        "{}:{}:{:?}",
        std::process::id(),
        DISTRIBUTION_VERSION,
        std::time::SystemTime::now()
    );
    let digest = Sha256::digest(seed.as_bytes());
    format!("{:x}", digest)[..24].to_owned()
}

fn report(sink: &impl Fn(SetupProgress), phase: &str, message: &str, current: u32, total: u32) {
    sink(SetupProgress {
        phase: phase.to_owned(),
        message: message.to_owned(),
        current,
        total,
        detail_percent: None,
    });
}

fn report_pack_progress(
    sink: &impl Fn(SetupProgress),
    phase: &str,
    label: &str,
    current: u32,
    total: u32,
    event: pack::PackProgress,
) {
    let (message, detail_percent) = match event {
        pack::PackProgress::CheckingExisting => (format!("正在检查本地{label}"), None),
        pack::PackProgress::Downloading(percent) => (format!("正在下载{label}"), Some(percent)),
        pack::PackProgress::SwitchingSource { remaining } => (
            format!("{label}下载源无响应，正在切换备用源（还有 {remaining} 个）"),
            None,
        ),
        pack::PackProgress::VerifyingArchive => (format!("正在校验{label}下载文件"), None),
        pack::PackProgress::Extracting(percent) => (format!("正在解压{label}"), Some(percent)),
        pack::PackProgress::HealthChecking => (format!("正在检查{label}可用性"), None),
        pack::PackProgress::VerifyingTool {
            name,
            current,
            total: tool_total,
        } => {
            let percent = step_percent(current, tool_total);
            if current >= tool_total {
                (format!("{label}中的工具均已验证"), Some(100))
            } else {
                (
                    format!("正在验证{label}中的{}", friendly_tool_name(&name)),
                    Some(percent),
                )
            }
        }
    };
    sink(SetupProgress {
        phase: phase.to_owned(),
        message,
        current,
        total,
        detail_percent,
    });
}

fn report_skill_progress(
    sink: &impl Fn(SetupProgress),
    host: &str,
    slug: &str,
    current: u32,
    total: u32,
    skill_current: u32,
    skill_total: u32,
) {
    let finished = skill_current >= skill_total;
    sink(SetupProgress {
        phase: "skills".into(),
        message: if finished {
            format!("{host} 的 Skills Pack 即将完成")
        } else {
            format!("正在为 {host} 激活 {slug}")
        },
        current,
        total,
        detail_percent: Some(step_percent(skill_current, skill_total)),
    });
}

fn step_percent(current: u32, total: u32) -> u8 {
    if total == 0 {
        return 100;
    }
    ((current.saturating_mul(100) / total).min(100)) as u8
}

fn friendly_tool_name(name: &str) -> &str {
    match name {
        "python-runtime" => "Python 运行时",
        "node-runtime" => "Node.js / OpenCLI 运行时",
        "markitdown" => "MarkItDown",
        "opencli" => "OpenCLI",
        "yt-dlp" => "yt-dlp",
        "aria2c" => "aria2c",
        "ffmpeg" => "FFmpeg",
        "asr-zh-postcheck" => "FunASR",
        "asr-other-postcheck" => "Whisper",
        _ => name,
    }
}

fn validate_provider_config(config: &ProviderConfig) -> Result<()> {
    if config.schema != PROVIDER_SCHEMA || config.policy != "official-preferred" {
        return Err(SetupError::InvalidProviderConfig(
            "only schema 1 with policy official-preferred is supported".into(),
        ));
    }
    for (id, provider) in &config.providers {
        if matches!(id.as_str(), "official" | "system")
            || id.is_empty()
            || !id
                .bytes()
                .all(|value| value.is_ascii_alphanumeric() || matches!(value, b'-' | b'_'))
        {
            return Err(SetupError::InvalidProviderConfig(format!(
                "invalid custom provider id: {id:?}"
            )));
        }
        for (name, argv) in provider
            .commands
            .iter()
            .chain(provider.python_profiles.iter())
        {
            if name.is_empty()
                || argv.is_empty()
                || argv.iter().any(String::is_empty)
                || !Path::new(&argv[0]).is_absolute()
            {
                return Err(SetupError::InvalidProviderConfig(format!(
                    "{id}/{name} must use a non-empty argv with an absolute executable"
                )));
            }
        }
    }
    for (capability, provider) in &config.overrides {
        if capability.is_empty()
            || (!matches!(provider.as_str(), "official" | "system")
                && !config.providers.contains_key(provider))
        {
            return Err(SetupError::InvalidProviderConfig(format!(
                "override {capability:?} references unknown provider {provider:?}"
            )));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::io::Write as _;

    fn request(host: &str) -> SetupRequest {
        SetupRequest {
            hosts: BTreeSet::from([host.to_owned()]),
            replace: BTreeSet::new(),
            install_official_toolchain: false,
            wiki_path: None,
            install_root: None,
        }
    }

    fn write_manifest(root: &Path, manifest: &DistributionManifest) -> PathBuf {
        let path = root.join("distribution.json");
        fs::write(&path, serde_json::to_vec(manifest).unwrap()).unwrap();
        path
    }

    fn empty_manifest() -> DistributionManifest {
        DistributionManifest {
            schema: 1,
            channel: "stable".into(),
            distribution_version: DISTRIBUTION_VERSION.into(),
            browser_version: DISTRIBUTION_VERSION.into(),
            skills_pack_version: DISTRIBUTION_VERSION.into(),
            pack_version: DISTRIBUTION_VERSION.into(),
            artifacts: Vec::new(),
        }
    }

    #[test]
    fn setup_installs_full_pack_and_initializes_wiki() {
        let temp = tempfile::tempdir().unwrap();
        fs::create_dir(temp.path().join(".codex")).unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        let result = core.setup(request("codex")).unwrap();
        assert_eq!(result.hosts["codex"].installed, bundled_skill_slugs());
        assert!(result.wiki.ready);
        assert!(
            temp.path()
                .join(".codex/skills/my-llm-wiki/SKILL.md")
                .is_file()
        );
        assert_eq!(core.status().unwrap().state, SetupHealth::Ready);
    }

    #[test]
    fn skills_are_installed_once_and_every_host_links_to_that_copy() {
        let temp = tempfile::tempdir().unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        let mut setup = request("codex");
        setup.hosts.insert("claude".into());
        core.setup(setup).unwrap();

        let canonical = temp.path().join(".my-llm-wiki/skills/my-llm-wiki");
        assert!(canonical.join("SKILL.md").is_file());
        assert!(!link::is_dir_link(&canonical));
        for host in [".codex", ".claude"] {
            let destination = temp.path().join(host).join("skills/my-llm-wiki");
            assert!(link::links_to(&destination, &canonical), "{host} is linked");
            assert!(destination.join("SKILL.md").is_file());
        }
        assert_eq!(core.status().unwrap().state, SetupHealth::Ready);
    }

    /// Manifest sources no run may reach. An operation that expects to work
    /// without the network fails loudly here instead of quietly depending on it.
    fn offline(core: SetupCore) -> SetupCore {
        let unreachable = vec!["/nonexistent/distribution.json".to_owned()];
        core.with_manifest_sources(unreachable.clone(), unreachable)
    }

    #[test]
    fn a_host_added_after_setup_joins_the_installed_pack() {
        let temp = tempfile::tempdir().unwrap();
        let core = offline(SetupCore::new(temp.path().to_path_buf()));
        core.setup(request("codex")).unwrap();

        let result = core
            .install_hosts(&BTreeSet::from(["claude".to_owned()]), &BTreeSet::new())
            .unwrap();

        assert_eq!(result.hosts["claude"].installed, bundled_skill_slugs());
        let canonical = temp.path().join(".my-llm-wiki/skills/my-llm-wiki");
        assert!(link::links_to(
            &temp.path().join(".claude/skills/my-llm-wiki"),
            &canonical
        ));
        // The host that was already there keeps its links and its state entry.
        assert!(result.hosts["codex"].healthy);
        assert_eq!(core.status().unwrap().state, SetupHealth::Ready);
    }

    #[test]
    fn adding_a_host_needs_the_same_exact_authority_as_setup() {
        let temp = tempfile::tempdir().unwrap();
        let core = offline(SetupCore::new(temp.path().to_path_buf()));
        core.setup(request("codex")).unwrap();
        let foreign = temp.path().join(".claude/skills/my-llm-wiki");
        fs::create_dir_all(&foreign).unwrap();
        fs::write(foreign.join("user.txt"), "mine").unwrap();
        let claude = BTreeSet::from(["claude".to_owned()]);

        let error = core.install_hosts(&claude, &BTreeSet::new()).unwrap_err();
        assert!(matches!(error, SetupError::ForeignDestination(path) if path == foreign));
        assert!(foreign.join("user.txt").is_file(), "nothing was touched");

        // Authority for a destination that is not foreign is refused too, so a
        // page cannot carry a stale approval into the run.
        let unrelated = temp.path().join(".claude/skills/my-llm-wiki-video");
        assert!(matches!(
            core.install_hosts(&claude, &BTreeSet::from([foreign.clone(), unrelated.clone()])),
            Err(SetupError::UnusedReplacement(path)) if path == unrelated
        ));

        let result = core
            .install_hosts(&claude, &BTreeSet::from([foreign.clone()]))
            .unwrap();
        assert_eq!(result.backups.len(), 1);
        assert!(result.backups[0].join("user.txt").is_file());
        assert!(link::is_dir_link(&foreign));
    }

    #[test]
    fn adding_a_host_requires_an_installation_and_a_known_host() {
        let temp = tempfile::tempdir().unwrap();
        let core = offline(SetupCore::new(temp.path().to_path_buf()));
        let codex = BTreeSet::from(["codex".to_owned()]);
        assert!(matches!(
            core.install_hosts(&codex, &BTreeSet::new()),
            Err(SetupError::InvalidState(_))
        ));

        core.setup(request("codex")).unwrap();
        assert!(matches!(
            core.install_hosts(&BTreeSet::from(["not-an-agent".to_owned()]), &BTreeSet::new()),
            Err(SetupError::UnknownHost(id)) if id == "not-an-agent"
        ));
        assert!(matches!(
            core.install_hosts(&BTreeSet::new(), &BTreeSet::new()),
            Err(SetupError::NoHosts)
        ));
    }

    #[test]
    fn a_removed_host_can_be_added_back_without_touching_the_others() {
        let temp = tempfile::tempdir().unwrap();
        let core = offline(SetupCore::new(temp.path().to_path_buf()));
        let mut setup = request("codex");
        setup.hosts.insert("claude".into());
        core.setup(setup).unwrap();

        core.uninstall(&BTreeSet::from(["codex".to_owned()]), false)
            .unwrap();
        assert!(!temp.path().join(".codex/skills/my-llm-wiki").exists());

        let result = core
            .install_hosts(&BTreeSet::from(["codex".to_owned()]), &BTreeSet::new())
            .unwrap();

        assert_eq!(result.hosts.len(), 2);
        assert!(result.hosts["claude"].healthy);
        assert_eq!(core.status().unwrap().state, SetupHealth::Ready);
    }

    #[test]
    fn a_chosen_install_root_holds_the_install_and_the_anchor_points_at_it() {
        let temp = tempfile::tempdir().unwrap();
        let cli_source = temp.path().join("source-cli");
        fs::write(&cli_source, "cli").unwrap();
        let root = temp.path().join("volume").join("my-llm-wiki");
        let core = SetupCore::new(temp.path().to_path_buf()).with_cli_source(Some(cli_source));
        let mut setup = request("codex");
        setup.install_root = Some(root.clone());

        core.setup(setup).unwrap();

        // Everything large lives at the chosen location...
        assert!(root.join("setup-state.json").is_file());
        assert!(root.join("skills/my-llm-wiki/SKILL.md").is_file());
        assert!(root.join("bin").is_dir());
        // ...and the fixed path every Skill resolves on its own still reaches
        // it, which is the whole point of moving the root behind a link.
        let anchor = temp.path().join(".my-llm-wiki");
        assert!(link::links_to(&anchor, &root));
        assert!(anchor.join("skills/my-llm-wiki/SKILL.md").is_file());
        assert!(link::links_to(
            &temp.path().join(".codex/skills/my-llm-wiki"),
            &root.join("skills/my-llm-wiki")
        ));

        // A core built fresh from the home directory finds the moved root.
        let reopened = SetupCore::new(temp.path().to_path_buf());
        assert_eq!(reopened.install_root(), root);
        assert_eq!(reopened.status().unwrap().state, SetupHealth::Ready);
        let inspection = reopened.inspect().unwrap();
        assert!(inspection.install_root_relocated);
        assert_eq!(inspection.install_root, root);
        assert_eq!(inspection.install_anchor, anchor);
    }

    #[test]
    fn the_default_install_root_uses_no_link() {
        let temp = tempfile::tempdir().unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        core.setup(request("codex")).unwrap();

        let anchor = temp.path().join(".my-llm-wiki");
        assert!(!link::is_dir_link(&anchor));
        assert_eq!(core.install_root(), anchor);
        assert!(!core.inspect().unwrap().install_root_relocated);
    }

    #[test]
    fn an_install_root_holding_unrelated_files_is_refused() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("documents");
        fs::create_dir_all(&root).unwrap();
        fs::write(root.join("notes.txt"), "mine").unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        let mut setup = request("codex");
        setup.install_root = Some(root.clone());

        let error = core.setup(setup).unwrap_err();

        assert!(matches!(error, SetupError::InstallRootOccupied(path) if path == root));
        assert!(root.join("notes.txt").is_file());
        assert!(!temp.path().join(".my-llm-wiki").exists());
    }

    #[test]
    fn uninstalling_one_host_detaches_its_links_and_keeps_the_installed_pack() {
        let temp = tempfile::tempdir().unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        let mut setup = request("codex");
        setup.hosts.insert("claude".into());
        core.setup(setup).unwrap();
        let canonical = temp.path().join(".my-llm-wiki/skills/my-llm-wiki");

        core.uninstall(&BTreeSet::from(["codex".to_owned()]), false)
            .unwrap();

        assert!(!temp.path().join(".codex/skills/my-llm-wiki").exists());
        assert!(
            !link::is_dir_link(&temp.path().join(".codex/skills/my-llm-wiki")),
            "the link entry is gone, not just its target"
        );
        // Detaching one host must not disturb the copy the others still use.
        assert!(canonical.join("SKILL.md").is_file());
        assert!(
            temp.path()
                .join(".claude/skills/my-llm-wiki/SKILL.md")
                .is_file()
        );
    }

    #[test]
    fn uninstalling_everything_removes_the_installed_pack() {
        let temp = tempfile::tempdir().unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        core.setup(request("codex")).unwrap();

        core.uninstall(&BTreeSet::new(), true).unwrap();

        assert!(!temp.path().join(".codex/skills/my-llm-wiki").exists());
        assert!(!temp.path().join(".my-llm-wiki/skills").exists());
    }

    #[test]
    fn a_host_link_pointing_somewhere_else_is_not_healthy() {
        let temp = tempfile::tempdir().unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        core.setup(request("codex")).unwrap();
        assert_eq!(core.status().unwrap().state, SetupHealth::Ready);

        // Re-aim one link at a directory that carries a valid marker copied
        // from the real install: ownership alone would call this healthy.
        let destination = temp.path().join(".codex/skills/my-llm-wiki");
        let decoy = temp.path().join("decoy");
        fs::create_dir_all(&decoy).unwrap();
        fs::copy(
            temp.path()
                .join(".my-llm-wiki/skills/my-llm-wiki")
                .join(OWNER_FILE),
            decoy.join(OWNER_FILE),
        )
        .unwrap();
        remove_installed_skill(&destination).unwrap();
        link::create_dir_link(&destination, &decoy).unwrap();

        assert_eq!(core.status().unwrap().state, SetupHealth::NeedsRepair);
    }

    #[test]
    fn setup_creates_default_volume_under_selected_collection_root() {
        let temp = tempfile::tempdir().unwrap();
        // The user selects a collection root; the my-llm-wiki volume is created
        // beneath it and that collection root is what Setup surfaces back.
        let collection = temp.path().join("knowledge");
        let volume = collection.join("my-llm-wiki");
        let core = SetupCore::new(temp.path().to_path_buf());
        let mut setup = request("codex");
        setup.wiki_path = Some(collection.clone());

        let result = core.setup(setup).unwrap();

        assert_eq!(result.wiki.path, volume);
        assert_eq!(result.wiki.collection_root, collection);
        assert!(result.wiki.ready);
        assert!(volume.join("schema.md").is_file());
        assert_eq!(core.inspect().unwrap().wiki.path, volume);
        assert_eq!(core.inspect().unwrap().wiki.collection_root, collection);
        assert_eq!(core.status().unwrap().wiki.path, volume);
    }

    #[test]
    fn default_inspect_surfaces_collection_root_not_volume() {
        let temp = tempfile::tempdir().unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        let wiki = core.inspect().unwrap().wiki;
        assert_eq!(wiki.collection_root, temp.path().join("wikis"));
        assert_eq!(wiki.path, temp.path().join("wikis/my-llm-wiki"));
    }

    #[test]
    fn setup_expands_home_relative_collection_root() {
        let temp = tempfile::tempdir().unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        let mut setup = request("codex");
        setup.wiki_path = Some(PathBuf::from("~/knowledge/wiki"));

        let result = core.setup(setup).unwrap();

        assert_eq!(
            result.wiki.path,
            temp.path().join("knowledge/wiki/my-llm-wiki")
        );
        assert!(result.wiki.ready);
    }

    #[test]
    fn setup_does_not_nest_when_path_is_already_a_volume() {
        let temp = tempfile::tempdir().unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        // A path that already ends with the volume name is used verbatim, so a
        // hand-typed full path or an older stored path never double-nests.
        let volume = temp.path().join("wikis/my-llm-wiki");
        let mut setup = request("codex");
        setup.wiki_path = Some(volume.clone());

        let result = core.setup(setup).unwrap();

        assert_eq!(result.wiki.path, volume);
        assert!(!volume.join("my-llm-wiki").exists());
    }

    #[test]
    fn setup_rejects_relative_wiki_path() {
        let temp = tempfile::tempdir().unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        let mut setup = request("codex");
        setup.wiki_path = Some(PathBuf::from("relative/wiki"));

        assert!(matches!(
            core.setup(setup),
            Err(SetupError::InvalidWikiPath(path)) if path == Path::new("relative/wiki")
        ));
    }

    #[test]
    fn setup_reports_real_stage_progress() {
        let temp = tempfile::tempdir().unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        let events = RefCell::new(Vec::new());
        core.setup_with_progress(request("codex"), |event| events.borrow_mut().push(event))
            .unwrap();
        let events = events.into_inner();
        assert_eq!((events[0].current, events[0].total), (0, 3));
        assert_eq!(
            (events.last().unwrap().current, events.last().unwrap().total),
            (3, 3)
        );
        assert!(events.iter().any(|event| {
            event.phase == "skills"
                && event.message.contains("my-llm-wiki-video")
                && event.detail_percent.is_some()
        }));
        assert!(
            events
                .iter()
                .any(|event| { event.phase == "skills" && event.detail_percent == Some(0) })
        );
        assert!(
            events
                .iter()
                .any(|event| { event.phase == "skills" && event.detail_percent == Some(100) })
        );
    }

    #[test]
    fn foreign_destination_requires_exact_authority_and_is_backed_up() {
        let temp = tempfile::tempdir().unwrap();
        let foreign = temp.path().join(".codex/skills/my-llm-wiki");
        fs::create_dir_all(&foreign).unwrap();
        fs::write(foreign.join("user.txt"), "mine").unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        let error = core.setup(request("codex")).unwrap_err();
        assert!(matches!(error, SetupError::ForeignDestination(path) if path == foreign));

        let mut request = request("codex");
        request.replace.insert(foreign);
        let result = core.setup(request).unwrap();
        assert_eq!(result.backups.len(), 1);
        assert!(result.backups[0].join("user.txt").is_file());
    }

    #[test]
    fn repair_restores_deleted_owned_skill_but_rejects_taken_ownership() {
        let temp = tempfile::tempdir().unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        core.setup(request("codex")).unwrap();
        let skill = temp.path().join(".codex/skills/my-llm-wiki-x");
        fs::remove_dir_all(&skill).unwrap();
        core.repair().unwrap();
        assert!(skill.join("SKILL.md").is_file());

        // Someone replaced the destination with a directory of their own. The
        // ownership marker is gone with it, so repair refuses rather than
        // overwriting whatever is now there.
        remove_installed_skill(&skill).unwrap();
        fs::create_dir_all(&skill).unwrap();
        fs::write(skill.join("SKILL.md"), "mine").unwrap();
        assert!(matches!(core.repair(), Err(SetupError::OwnershipLost(path)) if path == skill));
        assert_eq!(fs::read_to_string(skill.join("SKILL.md")).unwrap(), "mine");
    }

    #[test]
    fn repair_stops_on_request_and_stays_retryable() {
        let temp = tempfile::tempdir().unwrap();
        let cancel = Arc::new(AtomicBool::new(false));
        let core = SetupCore::new(temp.path().to_path_buf()).with_cancel(Arc::clone(&cancel));
        core.setup(request("codex")).unwrap();
        let skill = temp.path().join(".codex/skills/my-llm-wiki-x");
        fs::remove_dir_all(&skill).unwrap();

        cancel.store(true, Ordering::Relaxed);
        assert!(matches!(core.repair(), Err(SetupError::Cancelled)));

        // Stopping releases the lock and changes nothing else, so the user's
        // retry is an ordinary repair rather than a wedged install.
        cancel.store(false, Ordering::Relaxed);
        assert_eq!(core.repair().unwrap().state, SetupHealth::Ready);
        assert!(skill.join("SKILL.md").is_file());
    }

    #[test]
    fn uninstall_preserves_wiki() {
        let temp = tempfile::tempdir().unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        core.setup(request("codex")).unwrap();
        core.uninstall(&BTreeSet::from(["codex".into()]), false)
            .unwrap();
        assert!(temp.path().join("wikis/my-llm-wiki/schema.md").is_file());
        assert!(!temp.path().join(".codex/skills/my-llm-wiki").exists());
    }

    #[test]
    fn uninstall_requires_scope_and_all_preserves_user_data() {
        let temp = tempfile::tempdir().unwrap();
        let cli_source = temp.path().join("source-cli");
        fs::write(&cli_source, "cli").unwrap();
        let core = SetupCore::new(temp.path().to_path_buf()).with_cli_source(Some(cli_source));
        core.setup(request("codex")).unwrap();
        let provider = ProviderConfig {
            providers: BTreeMap::from([(
                "mine".into(),
                ProviderSpec {
                    commands: BTreeMap::from([(
                        "ffmpeg".into(),
                        vec![temp.path().join("ffmpeg").to_string_lossy().into_owned()],
                    )]),
                    ..ProviderSpec::default()
                },
            )]),
            ..ProviderConfig::default()
        };
        core.save_provider_config(&provider).unwrap();

        assert!(matches!(
            core.uninstall(&BTreeSet::new(), false),
            Err(SetupError::UninstallSelectionRequired)
        ));
        let cli_path = core.cli_path.clone();
        assert!(cli_path.is_file());
        core.uninstall(&BTreeSet::new(), true).unwrap();

        assert!(!cli_path.exists());
        assert!(!temp.path().join(".my-llm-wiki/setup-state.json").exists());
        assert!(temp.path().join(".my-llm-wiki/providers.json").is_file());
        assert!(temp.path().join("wikis/my-llm-wiki/schema.md").is_file());
        assert_eq!(core.provider_config().unwrap(), provider);
    }

    #[test]
    fn provider_config_accepts_structured_argv_and_rejects_unsafe_entries() {
        let temp = tempfile::tempdir().unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        let valid = ProviderConfig {
            overrides: BTreeMap::from([("media.extract-audio".into(), "mine".into())]),
            providers: BTreeMap::from([(
                "mine".into(),
                ProviderSpec {
                    commands: BTreeMap::from([(
                        "ffmpeg".into(),
                        vec![
                            temp.path()
                                .join("media/bin/ffmpeg")
                                .to_string_lossy()
                                .into_owned(),
                            "-nostdin".into(),
                        ],
                    )]),
                    ..ProviderSpec::default()
                },
            )]),
            ..ProviderConfig::default()
        };
        assert_eq!(core.save_provider_config(&valid).unwrap(), valid);
        assert_eq!(core.provider_config().unwrap(), valid);

        let mut invalid = valid;
        invalid
            .providers
            .get_mut("mine")
            .unwrap()
            .commands
            .insert("ffprobe".into(), vec!["ffprobe".into()]);
        assert!(matches!(
            core.save_provider_config(&invalid),
            Err(SetupError::InvalidProviderConfig(_))
        ));
    }

    #[test]
    fn update_reinstalls_the_owned_embedded_skills_pack() {
        let temp = tempfile::tempdir().unwrap();
        let manifest_path = write_manifest(temp.path(), &empty_manifest());
        let core = SetupCore::new(temp.path().to_path_buf()).with_manifest_sources(
            vec![manifest_path.to_string_lossy().into_owned()],
            vec![manifest_path.to_string_lossy().into_owned()],
        );
        core.setup(request("codex")).unwrap();
        let skill_file = temp.path().join(".codex/skills/my-llm-wiki/SKILL.md");
        let expected = fs::read(&skill_file).unwrap();
        fs::write(&skill_file, "locally changed").unwrap();
        assert_eq!(core.status().unwrap().state, SetupHealth::NeedsRepair);

        assert_eq!(core.update(false).unwrap().state, UpdateState::Updated);
        assert_eq!(fs::read(skill_file).unwrap(), expected);
        assert_eq!(core.status().unwrap().state, SetupHealth::Ready);
    }

    #[test]
    fn official_pack_is_verified_activated_and_repaired() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("toolchain.zip");
        let file_contents = b"verified toolchain\n";
        let file = File::create(&archive).unwrap();
        let mut zip = zip::ZipWriter::new(file);
        zip.start_file(
            "bin/tool",
            zip::write::SimpleFileOptions::default().unix_permissions(0o755),
        )
        .unwrap();
        zip.write_all(file_contents).unwrap();
        zip.finish().unwrap();
        let archive_data = fs::read(&archive).unwrap();
        let sha256 = format!("{:x}", Sha256::digest(&archive_data));
        let (platform, architecture) = pack::target();
        let toolchain = PackArtifact {
            id: "toolchain-base".into(),
            version: DISTRIBUTION_VERSION.into(),
            platform,
            architecture,
            sha256,
            size: archive_data.len() as u64,
            installed_size: file_contents.len() as u64,
            urls: vec![archive.to_string_lossy().into_owned()],
            commands: BTreeMap::from([("tool".into(), vec!["{pack}/bin/tool".into()])]),
            python_profiles: BTreeMap::new(),
            environment: BTreeMap::new(),
            capabilities: vec!["test".into()],
            probes: Vec::new(),
            manual_actions: Vec::new(),
        };
        let mut asr = toolchain.clone();
        asr.id = "asr-zh".into();
        let manifest = DistributionManifest {
            schema: 1,
            channel: "stable".into(),
            distribution_version: DISTRIBUTION_VERSION.into(),
            browser_version: DISTRIBUTION_VERSION.into(),
            skills_pack_version: DISTRIBUTION_VERSION.into(),
            pack_version: DISTRIBUTION_VERSION.into(),
            artifacts: vec![toolchain, asr],
        };
        let manifest_path = write_manifest(temp.path(), &manifest);
        let core = SetupCore::new(temp.path().to_path_buf()).with_manifest_sources(
            vec![manifest_path.to_string_lossy().into_owned()],
            vec![manifest_path.to_string_lossy().into_owned()],
        );
        let mut setup = request("codex");
        setup.install_official_toolchain = true;
        let setup_events = RefCell::new(Vec::new());
        let result = core
            .setup_with_progress(setup, |event| setup_events.borrow_mut().push(event))
            .unwrap();
        let setup_events = setup_events.into_inner();
        assert_eq!(result.state, SetupHealth::Ready);
        assert!(result.official_toolchain.healthy);
        assert!(setup_events.iter().any(|event| {
            event.phase == "toolchain"
                && event.message == "正在下载推荐工具链"
                && event.detail_percent == Some(100)
        }));
        assert!(setup_events.iter().any(|event| {
            event.phase == "toolchain"
                && event.message == "正在解压推荐工具链"
                && event.detail_percent == Some(100)
        }));
        assert!(setup_events.iter().any(|event| {
            event.phase == "toolchain"
                && event.message == "正在检查推荐工具链可用性"
                && event.detail_percent.is_none()
        }));
        assert!(setup_events.iter().any(|event| {
            event.phase == "toolchain"
                && event.message == "推荐工具链中的工具均已验证"
                && event.detail_percent == Some(100)
        }));

        let state = core.load_state().unwrap().unwrap();
        let tool = state.packs["toolchain-base"].path.join("bin/tool");
        fs::remove_file(&tool).unwrap();
        assert_eq!(core.status().unwrap().state, SetupHealth::NeedsRepair);
        assert_eq!(core.repair().unwrap().state, SetupHealth::Ready);
        assert!(tool.is_file());

        let result = core.ensure_pack("asr-zh").unwrap();
        assert!(result.packs["asr-zh"].healthy);
        let state = core.load_state().unwrap().unwrap();
        let asr_tool = state.packs["asr-zh"].path.join("bin/tool");
        fs::remove_file(&asr_tool).unwrap();
        assert_eq!(core.status().unwrap().state, SetupHealth::NeedsRepair);
        let repaired = core.repair().unwrap();
        assert_eq!(repaired.state, SetupHealth::Ready);
        assert!(repaired.packs["asr-zh"].healthy);
        assert!(asr_tool.is_file());

        core.uninstall(&BTreeSet::new(), true).unwrap();
        assert!(
            !temp
                .path()
                .join(".my-llm-wiki/packs/toolchain-base")
                .exists()
        );
        assert!(!temp.path().join(".my-llm-wiki/packs/asr-zh").exists());
        assert!(temp.path().join("wikis/my-llm-wiki/schema.md").is_file());
    }

    #[cfg(unix)]
    #[test]
    fn prepares_and_persists_chinese_asr_model_readiness() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("asr-zh.zip");
        let fake_python = br#"#!/bin/sh
shift
stage=""
model_root=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --stage) stage="$2"; shift 2 ;;
    --model-root) model_root="$2"; shift 2 ;;
    *) shift ;;
  esac
done
mkdir -p "$model_root"
progress="${MY_LLM_WIKI_ASR_PROGRESS:-}"
case "$stage" in
  fsmn-vad)
    mkdir -p "$model_root/fsmn-vad"
    if [ -n "$progress" ]; then
      printf '%s\n' '{"event":"download","stage":"fsmn-vad","downloaded_bytes":524288,"total_bytes":1048576}'
    fi
    printf '%s\n' 'downloaded fsmn-vad'
    ;;
  sensevoice)
    mkdir -p "$model_root/SenseVoiceSmall"
    if [ -n "$progress" ]; then
      printf '%s\n' '{"event":"download","stage":"sensevoice","downloaded_bytes":1048576,"total_bytes":null}'
    fi
    ;;
  verify)
    if [ -n "$progress" ]; then
      printf '%s\n' '{"event":"verify","step":"fsmn-vad","index":1,"count":2}'
      printf '%s\n' '{"event":"verify","step":"sensevoice","index":2,"count":2}'
    fi
    printf '%s\n' '{"schema":1,"models":{"fsmn-vad":{"id":"iic/speech_fsmn_vad_zh-cn-16k-common-pytorch","directory":"fsmn-vad"},"sensevoice":{"id":"iic/SenseVoiceSmall","directory":"SenseVoiceSmall"}}}' > "$model_root/.my-llm-wiki-models.json"
    ;;
esac
"#;
        let file = File::create(&archive).unwrap();
        let mut zip = zip::ZipWriter::new(file);
        zip.start_file(
            "bin/python",
            zip::write::SimpleFileOptions::default().unix_permissions(0o755),
        )
        .unwrap();
        zip.write_all(fake_python).unwrap();
        zip.finish().unwrap();
        let archive_data = fs::read(&archive).unwrap();
        let (platform, architecture) = pack::target();
        let manifest = DistributionManifest {
            schema: 1,
            channel: "stable".into(),
            distribution_version: DISTRIBUTION_VERSION.into(),
            browser_version: DISTRIBUTION_VERSION.into(),
            skills_pack_version: DISTRIBUTION_VERSION.into(),
            pack_version: DISTRIBUTION_VERSION.into(),
            artifacts: vec![PackArtifact {
                id: "asr-zh".into(),
                version: DISTRIBUTION_VERSION.into(),
                platform,
                architecture,
                sha256: format!("{:x}", Sha256::digest(&archive_data)),
                size: archive_data.len() as u64,
                installed_size: fake_python.len() as u64,
                urls: vec![archive.to_string_lossy().into_owned()],
                commands: BTreeMap::new(),
                python_profiles: BTreeMap::from([(
                    "asr-zh".into(),
                    vec!["{pack}/bin/python".into()],
                )]),
                environment: BTreeMap::new(),
                capabilities: vec!["transcribe.audio.timestamped".into()],
                probes: Vec::new(),
                manual_actions: Vec::new(),
            }],
        };
        let manifest_path = write_manifest(temp.path(), &manifest);
        let core = SetupCore::new(temp.path().to_path_buf()).with_manifest_sources(
            vec![manifest_path.to_string_lossy().into_owned()],
            vec![manifest_path.to_string_lossy().into_owned()],
        );
        core.setup(request("codex")).unwrap();
        assert!(!core.status().unwrap().model_caches["asr-zh"].ready);
        let events = RefCell::new(Vec::new());

        let result = core
            .prepare_asr_zh_with_progress(|event| events.borrow_mut().push(event))
            .unwrap();

        assert!(result.packs["asr-zh"].healthy);
        assert!(result.model_caches["asr-zh"].ready);
        assert!(events.borrow().iter().any(|event| {
            event.message == "正在下载语音分段模型 fsmn-vad"
                && event.current == 1
                && event.total == 4
        }));
        assert!(events.borrow().iter().any(|event| {
            event.message == "中文视频转写运行环境与模型均已就绪"
                && event.current == 4
                && event.total == 4
        }));
        // A known repository size becomes a real percentage, an unknown one at
        // least keeps the downloaded volume moving on screen.
        assert!(events.borrow().iter().any(|event| {
            event.phase == "asr-models"
                && event.message == "正在下载语音分段模型 fsmn-vad · 0.5 MB / 1.0 MB"
                && event.detail_percent == Some(50)
        }));
        assert!(events.borrow().iter().any(|event| {
            event.phase == "asr-models"
                && event.message == "正在下载中文转写模型 SenseVoiceSmall · 已下载 1.0 MB"
                && event.detail_percent.is_none()
        }));
        assert!(events.borrow().iter().any(|event| {
            event.message == "正在离线加载并验证第 2 / 2 个转写模型"
                && event.detail_percent == Some(50)
        }));

        let model_root = temp.path().join(".my-llm-wiki/models/asr-zh");
        fs::remove_dir_all(model_root.join("SenseVoiceSmall")).unwrap();
        assert!(!core.status().unwrap().model_caches["asr-zh"].ready);
    }
}
