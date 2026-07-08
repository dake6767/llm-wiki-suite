use std::path::PathBuf;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("wiki not found: {0}")]
    WikiNotFound(String),

    #[error("page not found in {wiki}: {path}")]
    PageNotFound { wiki: String, path: String },

    #[error("invalid registry: {0}")]
    InvalidRegistry(String),

    #[error("path escapes root: {path}")]
    PathEscapesRoot { path: PathBuf },

    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),

    #[error("yaml error: {0}")]
    Yaml(#[from] serde_yaml::Error),
}

pub type Result<T> = std::result::Result<T, Error>;
