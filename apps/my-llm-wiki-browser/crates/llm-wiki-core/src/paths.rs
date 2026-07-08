//! Unified suite home under `~/.my-llm-wiki/` and one-time migration from the
//! legacy locations.
//!
//! Everything the suite persists lives under a single branded home:
//!
//! ```text
//! ~/.my-llm-wiki/
//!   wikis.json          # wiki registry / routing
//!   connector/          # server-port, token, identity.json, relay-enabled
//!   suite/              # the repo checkout (managed by scripts/install.sh)
//! ```
//!
//! Legacy layouts (`~/.llm-wiki-connector/`, `~/.config/llm-wiki/wikis.json`)
//! are migrated on first launch by [`migrate_legacy`], which **moves** each into
//! the new home. It deliberately leaves **no** symlink behind and nothing reads
//! the old paths anymore: this is a clean cutover, so any component still
//! pointing at a legacy path fails loudly instead of being silently propped up.

use std::path::PathBuf;

fn home() -> Option<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from)
}

/// `~/.my-llm-wiki/`.
pub fn suite_home() -> Option<PathBuf> {
    home().map(|h| h.join(".my-llm-wiki"))
}

/// `~/.my-llm-wiki/connector/` — runtime state (port, token, relay identity).
pub fn connector_dir() -> Option<PathBuf> {
    suite_home().map(|h| h.join("connector"))
}

/// `~/.my-llm-wiki/wikis.json` — the wiki registry.
pub fn registry_path() -> Option<PathBuf> {
    suite_home().map(|h| h.join("wikis.json"))
}

fn legacy_connector_dir() -> Option<PathBuf> {
    home().map(|h| h.join(".llm-wiki-connector"))
}

fn legacy_registry_path() -> Option<PathBuf> {
    home().map(|h| h.join(".config").join("llm-wiki").join("wikis.json"))
}

/// Move `~/.config/llm-wiki/wikis.json` and `~/.llm-wiki-connector/` into
/// `~/.my-llm-wiki/` (no symlink left behind — clean cutover). Idempotent and
/// best-effort: any single failure is logged and skipped, never fatal. Call
/// once, early in startup, before anything reads those paths.
pub fn migrate_legacy() {
    if let (Some(new), Some(old)) = (connector_dir(), legacy_connector_dir()) {
        migrate_path(&old, &new);
    }
    if let (Some(new), Some(old)) = (registry_path(), legacy_registry_path()) {
        migrate_path(&old, &new);
    }
}

fn migrate_path(old: &std::path::Path, new: &std::path::Path) {
    // Already migrated if the destination exists, or the old path is already a
    // symlink (which we would have created on a previous run).
    if new.exists() {
        return;
    }
    let Ok(meta) = std::fs::symlink_metadata(old) else {
        return; // nothing at the old path
    };
    if meta.file_type().is_symlink() {
        return;
    }
    if let Some(parent) = new.parent() {
        if let Err(err) = std::fs::create_dir_all(parent) {
            eprintln!("migrate: cannot create suite home {parent:?}: {err}");
            return;
        }
    }
    if let Err(err) = std::fs::rename(old, new) {
        eprintln!("migrate: rename {old:?} -> {new:?} failed, leaving legacy path in place: {err}");
        return;
    }
    eprintln!("migrate: moved legacy {old:?} into {new:?}");
}
