use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::config::CoreConfig;
use crate::{Error, Result, WikiEntry};

#[derive(Debug, Deserialize)]
struct RegistryFile {
    #[serde(default)]
    wikis: Vec<RegistryItem>,
}

#[derive(Debug, Deserialize)]
struct RegistryItem {
    path: PathBuf,
    name: Option<String>,
    description: Option<String>,
    #[serde(default)]
    default: bool,
}

pub fn load_registry(config: &CoreConfig) -> Result<BTreeMap<String, WikiEntry>> {
    let mut entries = discover_wikis(&config.wiki_dirs);

    if config.registry_path.exists() {
        let text = fs::read_to_string(&config.registry_path)?;
        let data: RegistryFile =
            serde_json::from_str(&text).map_err(|err| Error::InvalidRegistry(err.to_string()))?;
        for item in data.wikis {
            let root = expand_home(item.path);
            if !(root.join("wiki")).is_dir() {
                continue;
            }
            let key = root
                .file_name()
                .and_then(|name| name.to_str())
                .ok_or_else(|| Error::InvalidRegistry(format!("invalid wiki path: {root:?}")))?
                .to_string();
            entries.insert(
                key.clone(),
                WikiEntry {
                    key: key.clone(),
                    name: item.name.unwrap_or_else(|| key.clone()),
                    description: item.description,
                    wiki_dir: root.join("wiki"),
                    raw_dir: root.join("raw"),
                    assets_dir: root.join("raw").join("assets"),
                    root_dir: root,
                    default: item.default,
                },
            );
        }
    }

    Ok(entries)
}

fn discover_wikis(roots: &[PathBuf]) -> BTreeMap<String, WikiEntry> {
    let mut found = BTreeMap::new();
    for root in roots {
        let root = expand_home(root);
        if !root.is_dir() {
            continue;
        }
        if let Some(entry) = wiki_root_from(&root) {
            found.insert(entry.key.clone(), entry);
            continue;
        }
        let Ok(children) = fs::read_dir(&root) else {
            continue;
        };
        for child in children.flatten() {
            if let Some(entry) = wiki_root_from(&child.path()) {
                found.insert(entry.key.clone(), entry);
            }
        }
    }
    found
}

fn wiki_root_from(root: &Path) -> Option<WikiEntry> {
    if !root.join("wiki").is_dir() {
        return None;
    }
    let key = root.file_name()?.to_str()?.to_string();
    Some(WikiEntry {
        key: key.clone(),
        name: key,
        description: None,
        wiki_dir: root.join("wiki"),
        raw_dir: root.join("raw"),
        assets_dir: root.join("raw").join("assets"),
        root_dir: root.to_path_buf(),
        default: false,
    })
}

fn expand_home(path: impl AsRef<Path>) -> PathBuf {
    let path = path.as_ref();
    let Some(text) = path.to_str() else {
        return path.to_path_buf();
    };
    if text == "~" {
        return crate::paths::home_dir().unwrap_or_else(|| path.to_path_buf());
    }
    if let Some(rest) = text.strip_prefix("~/") {
        return crate::paths::home_dir()
            .unwrap_or_else(|| PathBuf::from("~"))
            .join(rest);
    }
    path.to_path_buf()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registry_overrides_discovered_metadata() {
        let tmp = tempfile::tempdir().unwrap();
        let parent = tmp.path().join("parent");
        let wiki = parent.join("demo");
        fs::create_dir_all(wiki.join("wiki")).unwrap();

        let registry = tmp.path().join("wikis.json");
        fs::write(
            &registry,
            format!(
                r#"{{"wikis":[{{"path":"{}","name":"Demo Wiki","description":"desc","default":true}}]}}"#,
                wiki.display()
            ),
        )
        .unwrap();

        let config = CoreConfig {
            registry_path: registry,
            wiki_dirs: vec![parent],
            cache_dir: tmp.path().join(".cache"),
        };
        let entries = load_registry(&config).unwrap();
        let entry = entries.get("demo").unwrap();
        assert_eq!(entry.name, "Demo Wiki");
        assert_eq!(entry.description.as_deref(), Some("desc"));
        assert!(entry.default);
    }
}
