use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

pub const STATE_SCHEMA: u32 = 1;
pub const OWNER_SCHEMA: u32 = 1;
pub const PROVIDER_SCHEMA: u32 = 1;
pub const OWNER_FILE: &str = ".my-llm-wiki-owner.json";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HostDefinition {
    pub id: String,
    pub label: String,
    pub detect_dir: PathBuf,
    pub skills_dir: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum DestinationState {
    Absent,
    Owned,
    Foreign,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SkillDestination {
    pub slug: String,
    pub path: PathBuf,
    pub state: DestinationState,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HostInspection {
    pub id: String,
    pub label: String,
    pub detected: bool,
    pub skills_dir: PathBuf,
    pub destinations: Vec<SkillDestination>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SetupInspection {
    pub schema: u32,
    pub distribution_version: String,
    pub state_path: PathBuf,
    pub cli_path: Option<PathBuf>,
    pub hosts: Vec<HostInspection>,
    pub wiki: WikiStatus,
    pub official_toolchain: PackStatus,
    /// Where packs, models, and the Skills Pack actually live.
    pub install_root: PathBuf,
    /// The fixed `~/.my-llm-wiki` path every Skill resolves on its own.
    pub install_anchor: PathBuf,
    /// True when the anchor is a link because the root was moved off the home
    /// volume. The Setup page shows the real location in that case.
    pub install_root_relocated: bool,
}

/// How much room a candidate install root has, and whether it can be used.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct InstallRootProbe {
    pub path: PathBuf,
    pub exists: bool,
    pub writable: bool,
    /// `None` when the volume cannot be queried, e.g. the path is unreachable.
    pub free_bytes: Option<u64>,
    /// True when the directory already carries a suite installation, which
    /// setup reuses rather than treating as a conflict.
    pub existing_install: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SetupRequest {
    pub hosts: BTreeSet<String>,
    #[serde(default)]
    pub replace: BTreeSet<PathBuf>,
    #[serde(default = "default_true")]
    pub install_official_toolchain: bool,
    #[serde(default)]
    pub wiki_path: Option<PathBuf>,
    /// Where to install packs, models, and the Skills Pack. `None` keeps the
    /// default `~/.my-llm-wiki`; anything else makes the anchor a link.
    #[serde(default)]
    pub install_root: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SetupProgress {
    pub phase: String,
    pub message: String,
    pub current: u32,
    pub total: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail_percent: Option<u8>,
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SetupResult {
    pub state: SetupHealth,
    pub distribution_version: String,
    pub cli_path: Option<PathBuf>,
    pub hosts: BTreeMap<String, HostResult>,
    pub wiki: WikiStatus,
    pub official_toolchain: PackStatus,
    #[serde(default)]
    pub packs: BTreeMap<String, PackStatus>,
    #[serde(default)]
    pub model_caches: BTreeMap<String, ModelCacheStatus>,
    #[serde(default)]
    pub backups: Vec<PathBuf>,
    #[serde(default)]
    pub actions: Vec<ManualAction>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum SetupHealth {
    Ready,
    NotConfigured,
    NeedsRepair,
    ActionRequired,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HostResult {
    pub skills_dir: PathBuf,
    pub installed: Vec<String>,
    pub healthy: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct WikiStatus {
    /// The wiki volume itself — where `schema.md`, `wiki/`, and `raw/` live.
    pub path: PathBuf,
    /// The parent directory that holds one or more wiki volumes. This is what
    /// the user picks and sees in Setup; the default `my-llm-wiki` volume is
    /// created beneath it, and later siblings (e.g. `ai-wiki`) join it here.
    pub collection_root: PathBuf,
    pub registry_path: PathBuf,
    pub ready: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PackStatus {
    pub id: String,
    pub version: Option<String>,
    pub installed: bool,
    pub healthy: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ModelCacheStatus {
    pub id: String,
    pub path: PathBuf,
    pub ready: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ManualAction {
    pub id: String,
    pub title: String,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SetupState {
    pub schema: u32,
    pub install_id: String,
    pub distribution_version: String,
    pub skills_pack_version: String,
    pub cli_path: Option<PathBuf>,
    pub official_toolchain: bool,
    pub hosts: BTreeMap<String, OwnedHost>,
    pub packs: BTreeMap<String, OwnedPack>,
    pub wiki_path: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct OwnedHost {
    pub skills_dir: PathBuf,
    pub skills: BTreeMap<String, OwnedSkill>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct OwnedSkill {
    pub path: PathBuf,
    pub digest: String,
    /// Whether `path` is a link to the shared copy under the install root or a
    /// standalone copy. Links are the norm; a filesystem that cannot hold one
    /// falls back to copying and says so here.
    #[serde(default)]
    pub mode: SkillInstallMode,
}

#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum SkillInstallMode {
    #[default]
    Link,
    Copy,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct OwnedPack {
    pub version: String,
    pub path: PathBuf,
    pub digest: String,
    pub artifact: PackArtifact,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DistributionManifest {
    pub schema: u32,
    pub channel: String,
    pub distribution_version: String,
    pub browser_version: String,
    pub skills_pack_version: String,
    pub pack_version: String,
    pub artifacts: Vec<PackArtifact>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PackArtifact {
    pub id: String,
    pub version: String,
    pub platform: String,
    pub architecture: String,
    pub sha256: String,
    pub size: u64,
    pub installed_size: u64,
    pub urls: Vec<String>,
    #[serde(default)]
    pub commands: BTreeMap<String, Vec<String>>,
    #[serde(default)]
    pub python_profiles: BTreeMap<String, Vec<String>>,
    #[serde(default)]
    pub environment: BTreeMap<String, BTreeMap<String, String>>,
    #[serde(default)]
    pub capabilities: Vec<String>,
    #[serde(default)]
    pub probes: Vec<PackProbe>,
    #[serde(default)]
    pub manual_actions: Vec<PackManualAction>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PackProbe {
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PackManualAction {
    pub id: String,
    pub title: String,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct UpdateResult {
    pub state: UpdateState,
    pub current_version: String,
    pub latest_version: String,
    pub restart_required: bool,
    /// The Skills Pack moves on its own cadence, so it reports separately: a
    /// Browser that is up to date can still have skills to install.
    #[serde(default)]
    pub skills: SkillsUpdate,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct SkillsUpdate {
    pub state: SkillsUpdateState,
    pub installed_version: String,
    #[serde(default)]
    pub latest_version: Option<String>,
    /// Display-only text from the release, already reduced to plain text.
    #[serde(default)]
    pub notes: Option<String>,
    /// Skills that carried local edits and were moved aside before the official
    /// copy went in. Surfaced so the edits are recoverable rather than lost.
    #[serde(default)]
    pub backups: Vec<PathBuf>,
}

#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum SkillsUpdateState {
    #[default]
    UpToDate,
    Available,
    Updated,
    /// A newer pack exists but declares a Browser floor this build is below.
    /// The Browser updates first; the pack follows on the next check.
    BlockedByApp,
    /// No usable signal: offline, or every source answered with something that
    /// failed validation.
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ProviderConfig {
    pub schema: u32,
    pub policy: String,
    #[serde(default)]
    pub overrides: BTreeMap<String, String>,
    #[serde(default)]
    pub providers: BTreeMap<String, ProviderSpec>,
}

impl Default for ProviderConfig {
    fn default() -> Self {
        Self {
            schema: PROVIDER_SCHEMA,
            policy: "official-preferred".into(),
            overrides: BTreeMap::new(),
            providers: BTreeMap::new(),
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct ProviderSpec {
    #[serde(default)]
    pub commands: BTreeMap<String, Vec<String>>,
    #[serde(default)]
    pub python_profiles: BTreeMap<String, Vec<String>>,
    #[serde(default)]
    pub environment: BTreeMap<String, BTreeMap<String, String>>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum UpdateState {
    UpToDate,
    Available,
    RestartRequired,
    Updated,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct OwnershipMarker {
    pub schema: u32,
    pub install_id: String,
    pub artifact: String,
    pub version: String,
    pub digest: String,
}
