use std::path::PathBuf;

#[derive(Debug, thiserror::Error)]
pub enum SetupError {
    #[error("home directory is unavailable")]
    HomeUnavailable,
    #[error("unknown agent host: {0}")]
    UnknownHost(String),
    #[error("select at least one agent host")]
    NoHosts,
    #[error("uninstall requires one or more --host values or an explicit --all")]
    UninstallSelectionRequired,
    #[error("--all cannot be combined with individual uninstall hosts")]
    ConflictingUninstallSelection,
    #[error("provider configuration is invalid: {0}")]
    InvalidProviderConfig(String),
    #[error("wiki path must be absolute or start with ~/: {0}")]
    InvalidWikiPath(PathBuf),
    #[error("install root must be an absolute directory outside ~/.my-llm-wiki: {0}")]
    InvalidInstallRoot(PathBuf),
    #[error(
        "{0} is not empty and does not hold a My LLM Wiki installation; choose an empty folder"
    )]
    InstallRootOccupied(PathBuf),
    #[error(
        "{0} already holds an installation; uninstall it or remove the directory before installing elsewhere"
    )]
    InstallAnchorOccupied(PathBuf),
    #[error("{anchor} already points at {current}, not {requested}")]
    InstallRootConflict {
        anchor: PathBuf,
        current: PathBuf,
        requested: PathBuf,
    },
    #[error("foreign skill destination requires explicit replacement authority: {0}")]
    ForeignDestination(PathBuf),
    #[error("replacement authority was not used: {0}")]
    UnusedReplacement(PathBuf),
    #[error("owned destination no longer has valid ownership: {0}")]
    OwnershipLost(PathBuf),
    #[error("setup state is invalid: {0}")]
    InvalidState(String),
    #[error("setup operation is already running")]
    Locked,
    #[error("operation stopped on request")]
    Cancelled,
    #[error("cannot download {label}: {detail}")]
    Download { label: String, detail: String },
    #[error("invalid distribution manifest: {0}")]
    InvalidManifest(String),
    #[error("distribution {actual} does not match this Browser build {expected}")]
    DistributionMismatch { expected: String, actual: String },
    #[error("release has no {pack} artifact for {platform}/{architecture}")]
    PackUnavailable {
        pack: String,
        platform: String,
        architecture: String,
    },
    #[error("SHA-256 mismatch for {path}: expected {expected}, got {actual}")]
    Checksum {
        path: PathBuf,
        expected: String,
        actual: String,
    },
    #[error("unsafe archive member in {archive}: {member}")]
    UnsafeArchive { archive: PathBuf, member: String },
    #[error("pack health probe failed for {pack}: {detail}")]
    Probe { pack: String, detail: String },
    #[error("I/O error at {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("JSON error at {path}: {source}")]
    Json {
        path: PathBuf,
        #[source]
        source: serde_json::Error,
    },
}

impl SetupError {
    pub(crate) fn io(path: impl Into<PathBuf>, source: std::io::Error) -> Self {
        Self::Io {
            path: path.into(),
            source,
        }
    }

    pub(crate) fn json(path: impl Into<PathBuf>, source: serde_json::Error) -> Self {
        Self::Json {
            path: path.into(),
            source,
        }
    }
}

pub type Result<T> = std::result::Result<T, SetupError>;
