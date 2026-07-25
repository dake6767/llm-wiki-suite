use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::error::{Result, SetupError};
use crate::model::WikiStatus;

const PURPOSE: &str = "# Project Purpose\n\n## Goal\n\n<!-- What are you trying to understand or build? -->\n\n## Key Questions\n\n1.\n2.\n3.\n\n## Scope\n\n**In scope:**\n-\n\n**Out of scope:**\n-\n\n## Thesis\n\n> TBD\n";
const INDEX: &str = "# Wiki Index\n\n## Entities\n\n## Concepts\n\n## Sources\n\n## Queries\n\n## Comparisons\n\n## Synthesis\n";
const OVERVIEW: &str = "---\ntype: overview\ntitle: Project Overview\ntags: []\nrelated: []\n---\n\n# Overview\n\n<!-- Provide a high-level summary of what this wiki covers and its current state. -->\n";
const SUBDIRS: &[&str] = &[
    "raw/sources",
    "raw/assets",
    "wiki/entities",
    "wiki/concepts",
    "wiki/sources",
    "wiki/queries",
    "wiki/comparisons",
    "wiki/synthesis",
];
static TEMPORARY_SEQUENCE: AtomicU64 = AtomicU64::new(0);
/// Schema version of `wikis.json`, matching the Skill's `wikis.py`.
const REGISTRY_VERSION: u64 = 1;

#[derive(Debug, Default, Serialize, Deserialize)]
struct Registry {
    #[serde(default)]
    wikis: Vec<RegistryEntry>,
    /// Everything else the file carries — `version`, or keys a newer Skill
    /// writes. Kept so registering a wiki never drops what we do not model.
    #[serde(flatten)]
    extra: Map<String, Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct RegistryEntry {
    path: PathBuf,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    name: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    description: String,
    #[serde(default)]
    default: bool,
    #[serde(flatten)]
    extra: Map<String, Value>,
}

/// Whether `root` holds an initialized wiki volume.
pub(crate) fn is_volume(root: &Path) -> bool {
    root.join("schema.md").is_file() && root.join("wiki").is_dir()
}

pub(crate) fn status(root: &Path, registry_path: &Path) -> WikiStatus {
    WikiStatus {
        // The collection root is the volume's parent; fall back to the volume
        // itself only for a filesystem root with no parent.
        collection_root: root.parent().unwrap_or(root).to_path_buf(),
        path: root.to_path_buf(),
        registry_path: registry_path.to_path_buf(),
        ready: is_volume(root),
    }
}

/// A wiki the user already keeps: the registered default when it exists on
/// disk, otherwise the first registered volume that does.
///
/// Repair uses this to re-point an install whose recorded volume is gone,
/// instead of resurrecting a volume the user deleted. An unreadable registry
/// simply yields nothing — it is the user's file, and reading it must never
/// turn into an error the installer reports.
pub(crate) fn registered_volume(registry_path: &Path) -> Option<PathBuf> {
    let registry: Registry = fs::read(registry_path)
        .ok()
        .and_then(|data| serde_json::from_slice(&data).ok())?;
    let existing: Vec<_> = registry
        .wikis
        .into_iter()
        .map(|entry| (expand_home(&entry.path), entry.default))
        .filter(|(path, _)| is_volume(path))
        .collect();
    existing
        .iter()
        .find(|(_, is_default)| *is_default)
        .or_else(|| existing.first())
        .map(|(path, _)| path.clone())
}

pub(crate) fn ensure(root: &Path, registry_path: &Path, schema: &[u8]) -> Result<WikiStatus> {
    if !root.join("schema.md").exists() {
        for relative in SUBDIRS {
            let dir = root.join(relative);
            fs::create_dir_all(&dir).map_err(|err| SetupError::io(&dir, err))?;
            let keep = dir.join(".gitkeep");
            if !keep.exists() {
                fs::write(&keep, []).map_err(|err| SetupError::io(&keep, err))?;
            }
        }
        write_new(&root.join("schema.md"), schema)?;
        write_new(&root.join("purpose.md"), PURPOSE.as_bytes())?;
        write_new(&root.join("wiki/index.md"), INDEX.as_bytes())?;
        write_new(
            &root.join("wiki/log.md"),
            b"# Research Log\n\n- Project created\n",
        )?;
        write_new(&root.join("wiki/overview.md"), OVERVIEW.as_bytes())?;
        write_new(
            &root.join(".obsidian/app.json"),
            br#"{
  "attachmentFolderPath": "raw/assets",
  "userIgnoreFilters": [".cache", ".llm-wiki", ".superpowers"],
  "useMarkdownLinks": false,
  "newLinkFormat": "shortest",
  "showUnsupportedFiles": false
}
"#,
        )?;
        write_new(
            &root.join(".obsidian/appearance.json"),
            b"{\n  \"baseFontSize\": 16,\n  \"theme\": \"obsidian\"\n}\n",
        )?;
    }
    register(root, registry_path)?;
    Ok(status(root, registry_path))
}

fn write_new(path: &Path, contents: &[u8]) -> Result<()> {
    if path.exists() {
        return Ok(());
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|err| SetupError::io(parent, err))?;
    }
    fs::write(path, contents).map_err(|err| SetupError::io(path, err))
}

/// Add this wiki to the shared routing registry without disturbing what is
/// already in it.
///
/// `wikis.json` is the user's routing table: the agent picks a wiki by matching
/// its `description`, and `default` decides where an unclassified capture
/// lands. Both belong to the user (the Skill's `wikis.py register` follows the
/// same rule), so registering is an insert and never an edit — an already
/// registered path is left exactly as it is, a new one claims `default` only
/// when it is the only wiki there, and no other entry is touched.
fn register(root: &Path, registry_path: &Path) -> Result<()> {
    let mut registry = if registry_path.exists() {
        let data = fs::read(registry_path).map_err(|err| SetupError::io(registry_path, err))?;
        serde_json::from_slice(&data).map_err(|err| SetupError::json(registry_path, err))?
    } else {
        Registry::default()
    };
    if registry
        .wikis
        .iter()
        .any(|entry| same_volume(&expand_home(&entry.path), root))
    {
        return Ok(());
    }
    let name = root
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("my-llm-wiki")
        .to_owned();
    // A lone wiki is the natural default; once the user keeps several, which
    // one is default is their decision to make.
    let default = registry.wikis.is_empty();
    registry.wikis.push(RegistryEntry {
        path: root.to_path_buf(),
        name,
        description: String::new(),
        default,
        extra: Map::new(),
    });
    registry
        .extra
        .entry("version".to_owned())
        .or_insert_with(|| Value::from(REGISTRY_VERSION));
    let contents =
        serde_json::to_vec_pretty(&registry).map_err(|err| SetupError::json(registry_path, err))?;
    atomic_write(registry_path, &contents)
}

/// Whether two registered paths name the same wiki. Entries are written by
/// several tools, so compare through symlinks when both sides exist and fall
/// back to a literal match when they do not.
fn same_volume(left: &Path, right: &Path) -> bool {
    if left == right {
        return true;
    }
    match (left.canonicalize(), right.canonicalize()) {
        (Ok(left), Ok(right)) => left == right,
        _ => false,
    }
}

/// Expand a leading `~`, which a hand-edited registry may carry.
fn expand_home(path: &Path) -> PathBuf {
    let Ok(relative) = path.strip_prefix("~") else {
        return path.to_path_buf();
    };
    match dirs::home_dir() {
        Some(home) => home.join(relative),
        None => path.to_path_buf(),
    }
}

pub(crate) fn atomic_write(path: &Path, contents: &[u8]) -> Result<()> {
    let parent = path.parent().ok_or_else(|| {
        SetupError::InvalidState(format!("path has no parent: {}", path.display()))
    })?;
    fs::create_dir_all(parent).map_err(|err| SetupError::io(parent, err))?;
    let temporary = parent.join(format!(
        ".{}.tmp-{}-{}",
        path.file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("state"),
        std::process::id(),
        TEMPORARY_SEQUENCE.fetch_add(1, Ordering::Relaxed),
    ));
    let mut output = fs::File::create(&temporary).map_err(|err| SetupError::io(&temporary, err))?;
    use std::io::Write as _;
    output
        .write_all(contents)
        .map_err(|err| SetupError::io(&temporary, err))?;
    output
        .sync_all()
        .map_err(|err| SetupError::io(&temporary, err))?;
    drop(output);
    replace_file(&temporary, path)?;
    #[cfg(unix)]
    fs::File::open(parent)
        .and_then(|directory| directory.sync_all())
        .map_err(|err| SetupError::io(parent, err))?;
    Ok(())
}

#[cfg(not(windows))]
fn replace_file(temporary: &Path, destination: &Path) -> Result<()> {
    fs::rename(temporary, destination).map_err(|err| SetupError::io(temporary, err))
}

#[cfg(windows)]
fn replace_file(temporary: &Path, destination: &Path) -> Result<()> {
    if !destination.exists() {
        return fs::rename(temporary, destination).map_err(|err| SetupError::io(temporary, err));
    }
    use std::os::windows::ffi::OsStrExt as _;
    use windows_sys::Win32::Storage::FileSystem::ReplaceFileW;

    let destination_wide: Vec<u16> = destination
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let temporary_wide: Vec<u16> = temporary
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let replaced = unsafe {
        ReplaceFileW(
            destination_wide.as_ptr(),
            temporary_wide.as_ptr(),
            std::ptr::null(),
            0,
            std::ptr::null(),
            std::ptr::null(),
        )
    };
    if replaced == 0 {
        return Err(SetupError::io(destination, std::io::Error::last_os_error()));
    }
    Ok(())
}
