mod error;
mod mcp_bridge;
mod model;
mod pack;
mod wiki;

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::path::{Path, PathBuf};

pub use error::{Result, SetupError};
use fs2::FileExt as _;
use include_dir::{Dir, include_dir};
pub use mcp_bridge::{McpBridgeOptions, run as run_mcp_bridge};
pub use model::*;
use sha2::{Digest as _, Sha256};

static BUNDLED_SKILLS: Dir<'_> = include_dir!("$CARGO_MANIFEST_DIR/../../../../skills");

const DISTRIBUTION_VERSION: &str = env!("CARGO_PKG_VERSION");
const DEFAULT_WIKI_RELATIVE: &str = "wikis/my-llm-wiki";

pub struct SetupCore {
    home: PathBuf,
    suite_home: PathBuf,
    state_path: PathBuf,
    providers_path: PathBuf,
    cli_source: Option<PathBuf>,
    cli_path: PathBuf,
    wiki_path: PathBuf,
    registry_path: PathBuf,
    current_manifest_sources: Vec<String>,
    latest_manifest_sources: Vec<String>,
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
        let suite_home = home.join(".my-llm-wiki");
        let version = DISTRIBUTION_VERSION;
        Self {
            state_path: suite_home.join("setup-state.json"),
            providers_path: suite_home.join("providers.json"),
            cli_path: suite_home
                .join("bin")
                .join(if cfg!(windows) { "my-llm-wiki.exe" } else { "my-llm-wiki" }),
            cli_source: None,
            registry_path: suite_home.join("wikis.json"),
            wiki_path: home.join(DEFAULT_WIKI_RELATIVE),
            suite_home,
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
        }
    }

    pub fn with_cli_source(mut self, source: Option<PathBuf>) -> Self {
        self.cli_source = source;
        self
    }

    pub fn with_manifest_sources(mut self, current: Vec<String>, latest: Vec<String>) -> Self {
        self.current_manifest_sources = current;
        self.latest_manifest_sources = latest;
        self
    }

    pub fn inspect(&self) -> Result<SetupInspection> {
        let state = self.load_state()?;
        let install_id = state.as_ref().map(|state| state.install_id.as_str());
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
            wiki: wiki::status(&self.wiki_path, &self.registry_path),
            official_toolchain: self.toolchain_status(state.as_ref()),
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
        let total = request.hosts.len() as u32 + 1 + u32::from(request.install_official_toolchain);
        report(&progress, "preparing", "正在校验目标与所有权", 0, total);
        let _lock = self.lock()?;
        let mut state = self.load_state()?.unwrap_or_else(|| SetupState {
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
        self.install_cli(&mut state)?;
        let definitions: BTreeMap<_, _> = host_definitions(&self.home)
            .into_iter()
            .map(|host| (host.id.clone(), host))
            .collect();
        let mut expected_replacements = BTreeSet::new();
        for host_id in &request.hosts {
            let host = definitions
                .get(host_id)
                .ok_or_else(|| SetupError::UnknownHost(host_id.clone()))?;
            for slug in bundled_skill_slugs() {
                let path = host.skills_dir.join(slug);
                if destination_state(&path, Some(&state.install_id)) == DestinationState::Foreign {
                    expected_replacements.insert(path);
                }
            }
        }
        if let Some(missing) = expected_replacements.difference(&request.replace).next() {
            return Err(SetupError::ForeignDestination(missing.clone()));
        }
        if let Some(unused) = request.replace.difference(&expected_replacements).next() {
            return Err(SetupError::UnusedReplacement(unused.clone()));
        }
        if !request.install_official_toolchain {
            self.remove_owned_pack(&mut state, "toolchain-base")?;
        }
        let mut replacements = request.replace;
        let mut backups = Vec::new();
        self.save_state(&state)?;
        let mut completed = 0;
        for host_id in request.hosts {
            let host = definitions
                .get(&host_id)
                .ok_or_else(|| SetupError::UnknownHost(host_id.clone()))?;
            let owned =
                self.install_host(host, &state.install_id, &mut replacements, &mut backups)?;
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
        wiki::ensure(&state.wiki_path, &self.registry_path, schema.contents())?;
        completed += 1;
        report(&progress, "wiki", "Wiki 已初始化", completed, total);
        state.distribution_version = DISTRIBUTION_VERSION.to_owned();
        state.skills_pack_version = DISTRIBUTION_VERSION.to_owned();
        if request.install_official_toolchain {
            let manifest = pack::fetch_manifest(&self.current_manifest_sources)?;
            self.require_current_distribution(&manifest)?;
            let artifact = pack::select_artifact(&manifest, "toolchain-base")?;
            let installed = pack::install_pack(&self.suite_home, artifact)?;
            state.packs.insert("toolchain-base".into(), installed);
            completed += 1;
            report(
                &progress,
                "toolchain",
                "官方工具链已通过健康检查",
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

    pub fn repair(&self) -> Result<SetupResult> {
        self.repair_with_progress(|_| {})
    }

    pub fn repair_with_progress(&self, progress: impl Fn(SetupProgress)) -> Result<SetupResult> {
        let _lock = self.lock()?;
        let mut state = self
            .load_state()?
            .ok_or_else(|| SetupError::InvalidState("setup has not been completed".into()))?;
        self.install_cli(&mut state)?;
        let definitions: BTreeMap<_, _> = host_definitions(&self.home)
            .into_iter()
            .map(|host| (host.id.clone(), host))
            .collect();
        let host_ids: Vec<_> = state.hosts.keys().cloned().collect();
        let mut pack_ids: BTreeSet<_> = state.packs.keys().cloned().collect();
        if state.official_toolchain {
            pack_ids.insert("toolchain-base".into());
        }
        let total = host_ids.len() as u32 + 1 + pack_ids.len() as u32;
        report(&progress, "preparing", "正在检查当前安装", 0, total);
        let mut completed = 0;
        let mut replacements = BTreeSet::new();
        let mut backups = Vec::new();
        for host_id in host_ids {
            let host = definitions
                .get(&host_id)
                .ok_or_else(|| SetupError::UnknownHost(host_id.clone()))?;
            for slug in bundled_skill_slugs() {
                let path = host.skills_dir.join(&slug);
                match destination_state(&path, Some(&state.install_id)) {
                    DestinationState::Foreign => return Err(SetupError::OwnershipLost(path)),
                    DestinationState::Absent | DestinationState::Owned => {}
                }
            }
            let owned =
                self.install_host(host, &state.install_id, &mut replacements, &mut backups)?;
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
        wiki::ensure(&state.wiki_path, &self.registry_path, schema.contents())?;
        completed += 1;
        report(&progress, "wiki", "Wiki 状态已校验", completed, total);
        for id in pack_ids {
            let installed = if let Some(existing) = state.packs.get(&id) {
                if pack::check_owned_pack(existing).is_ok() {
                    None
                } else {
                    Some(pack::install_pack(&self.suite_home, &existing.artifact)?)
                }
            } else {
                let manifest = pack::fetch_manifest(&self.current_manifest_sources)?;
                self.require_current_distribution(&manifest)?;
                Some(pack::install_pack(
                    &self.suite_home,
                    pack::select_artifact(&manifest, &id)?,
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
        let installed = pack::install_pack(&self.suite_home, artifact)?;
        state.packs.insert(id.to_owned(), installed);
        if id == "toolchain-base" {
            state.official_toolchain = true;
        }
        self.save_state(&state)?;
        self.prune_active_packs(&state);
        report(&progress, "pack", &format!("{id} 能力包已就绪"), 1, 1);
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
            let owned =
                self.install_host(host, &state.install_id, &mut replacements, &mut backups)?;
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
                fs::remove_dir_all(&skill.path).map_err(|err| SetupError::io(&skill.path, err))?;
            }
            state.hosts.remove(&host_id);
        }
        if all {
            for id in state.packs.keys().cloned().collect::<Vec<_>>() {
                self.remove_owned_pack(&mut state, &id)?;
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

    fn install_host(
        &self,
        host: &HostDefinition,
        install_id: &str,
        replacements: &mut BTreeSet<PathBuf>,
        backups: &mut Vec<PathBuf>,
    ) -> Result<OwnedHost> {
        fs::create_dir_all(&host.skills_dir)
            .map_err(|err| SetupError::io(&host.skills_dir, err))?;
        let mut skills = BTreeMap::new();
        for slug in bundled_skill_slugs() {
            let destination = host.skills_dir.join(&slug);
            let current = destination_state(&destination, Some(install_id));
            if current == DestinationState::Foreign && !replacements.remove(&destination) {
                return Err(SetupError::ForeignDestination(destination));
            }
            let source = BUNDLED_SKILLS.get_dir(&slug).ok_or_else(|| {
                SetupError::InvalidState(format!("bundled skill missing: {slug}"))
            })?;
            let digest = digest_dir(source);
            let stage = host
                .skills_dir
                .join(format!(".my-llm-wiki-stage-{}-{slug}", std::process::id()));
            if stage.exists() {
                fs::remove_dir_all(&stage).map_err(|err| SetupError::io(&stage, err))?;
            }
            write_dir(source, &stage)?;
            let marker = OwnershipMarker {
                schema: OWNER_SCHEMA,
                install_id: install_id.to_owned(),
                artifact: slug.clone(),
                version: DISTRIBUTION_VERSION.to_owned(),
                digest: digest.clone(),
            };
            let marker_path = stage.join(OWNER_FILE);
            let marker_data = serde_json::to_vec_pretty(&marker)
                .map_err(|err| SetupError::json(&marker_path, err))?;
            fs::write(&marker_path, marker_data)
                .map_err(|err| SetupError::io(&marker_path, err))?;

            let old = if destination.exists() {
                let old = if current == DestinationState::Foreign {
                    backup_path(&host.skills_dir, &slug)
                } else {
                    host.skills_dir
                        .join(format!(".my-llm-wiki-old-{}-{slug}", std::process::id()))
                };
                if let Some(parent) = old.parent() {
                    fs::create_dir_all(parent).map_err(|err| SetupError::io(parent, err))?;
                }
                fs::rename(&destination, &old).map_err(|err| SetupError::io(&destination, err))?;
                Some((old, current == DestinationState::Foreign))
            } else {
                None
            };
            if let Err(err) = fs::rename(&stage, &destination) {
                if let Some((old, _)) = old.as_ref() {
                    let _ = fs::rename(old, &destination);
                }
                return Err(SetupError::io(&stage, err));
            }
            if let Some((old, foreign)) = old {
                if foreign {
                    backups.push(old);
                } else {
                    fs::remove_dir_all(&old).map_err(|err| SetupError::io(&old, err))?;
                }
            }
            skills.insert(
                slug,
                OwnedSkill {
                    path: destination,
                    digest,
                },
            );
        }
        Ok(OwnedHost {
            skills_dir: host.skills_dir.clone(),
            skills,
        })
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
        state
            .hosts
            .iter()
            .map(|(id, host)| {
                let installed: Vec<_> = host.skills.keys().cloned().collect();
                let healthy = host.skills.values().all(|skill| {
                    destination_state(&skill.path, Some(&state.install_id))
                        == DestinationState::Owned
                        && digest_path(&skill.path).as_deref() == Some(skill.digest.as_str())
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
    });
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
    fn setup_reports_real_stage_progress() {
        let temp = tempfile::tempdir().unwrap();
        let core = SetupCore::new(temp.path().to_path_buf());
        let events = RefCell::new(Vec::new());
        core.setup_with_progress(request("codex"), |event| events.borrow_mut().push(event))
            .unwrap();
        let events = events.into_inner();
        assert_eq!(
            events
                .iter()
                .map(|event| event.phase.as_str())
                .collect::<Vec<_>>(),
            ["preparing", "skills", "wiki"]
        );
        assert_eq!((events[0].current, events[0].total), (0, 2));
        assert_eq!((events[2].current, events[2].total), (2, 2));
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
        fs::remove_file(skill.join(OWNER_FILE)).unwrap();
        assert!(matches!(core.repair(), Err(SetupError::OwnershipLost(path)) if path == skill));
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
        let cli_path = temp.path().join(".my-llm-wiki/bin/my-llm-wiki");
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
                        vec!["/opt/media/bin/ffmpeg".into(), "-nostdin".into()],
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
            artifacts: vec![toolchain, asr],
        };
        let manifest_path = write_manifest(temp.path(), &manifest);
        let core = SetupCore::new(temp.path().to_path_buf()).with_manifest_sources(
            vec![manifest_path.to_string_lossy().into_owned()],
            vec![manifest_path.to_string_lossy().into_owned()],
        );
        let mut setup = request("codex");
        setup.install_official_toolchain = true;
        let result = core.setup(setup).unwrap();
        assert_eq!(result.state, SetupHealth::Ready);
        assert!(result.official_toolchain.healthy);

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
}
