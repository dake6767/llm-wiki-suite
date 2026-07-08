use std::env;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CoreConfig {
    pub registry_path: PathBuf,
    pub wiki_dirs: Vec<PathBuf>,
    pub cache_dir: PathBuf,
}

impl CoreConfig {
    pub fn from_env() -> Self {
        let home = env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
        let registry_path = env::var_os("LLM_WIKI_REGISTRY")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".my-llm-wiki").join("wikis.json"));
        let wiki_dirs = env::var("LLM_WIKI_DIR")
            .unwrap_or_default()
            .split(',')
            .flat_map(env::split_paths)
            .collect();
        let cache_dir = env::var_os("LLM_WIKI_WEB_CACHE")
            .map(PathBuf::from)
            .unwrap_or_else(|| default_cache_dir(&home));

        Self {
            registry_path,
            wiki_dirs,
            cache_dir,
        }
    }
}

fn default_cache_dir(home: &Path) -> PathBuf {
    home.join(".cache").join("llm-wiki-web")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_cache_dir_is_under_home() {
        assert_eq!(
            default_cache_dir(Path::new("/Users/demo")),
            PathBuf::from("/Users/demo/.cache/llm-wiki-web")
        );
    }
}
