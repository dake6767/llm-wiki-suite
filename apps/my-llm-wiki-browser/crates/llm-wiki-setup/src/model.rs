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
