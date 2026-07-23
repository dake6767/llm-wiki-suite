use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};

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

#[derive(Debug, Default, Serialize, Deserialize)]
struct Registry {
    #[serde(default)]
    wikis: Vec<RegistryEntry>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct RegistryEntry {
    path: PathBuf,
    name: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    description: String,
    #[serde(default)]
    default: bool,
}

pub(crate) fn status(root: &Path, registry_path: &Path) -> WikiStatus {
    WikiStatus {
        path: root.to_path_buf(),
        registry_path: registry_path.to_path_buf(),
        ready: root.join("schema.md").is_file() && root.join("wiki").is_dir(),
    }
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

fn register(root: &Path, registry_path: &Path) -> Result<()> {
    let mut registry = if registry_path.exists() {
        let data = fs::read(registry_path).map_err(|err| SetupError::io(registry_path, err))?;
        serde_json::from_slice(&data).map_err(|err| SetupError::json(registry_path, err))?
    } else {
        Registry::default()
    };
    let name = root
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("my-llm-wiki")
        .to_owned();
    let mut found = false;
    for entry in &mut registry.wikis {
        if entry.path == root {
            entry.name.clone_from(&name);
            entry.default = true;
            found = true;
        } else {
            entry.default = false;
        }
    }
    if !found {
        registry.wikis.push(RegistryEntry {
            path: root.to_path_buf(),
            name,
            description: String::new(),
            default: true,
        });
    }
    let contents =
        serde_json::to_vec_pretty(&registry).map_err(|err| SetupError::json(registry_path, err))?;
    atomic_write(registry_path, &contents)
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
