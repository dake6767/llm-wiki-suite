//! Where the suite installs itself, and how everything else still finds it.
//!
//! Two paths matter and they are not the same path:
//!
//! * the **anchor**, always `~/.my-llm-wiki`, which never moves;
//! * the **install root**, which is whatever directory the user picked.
//!
//! When the two differ the anchor becomes a link to the root. That indirection
//! is what makes a custom location workable at all: Skills run as separate
//! processes started by third-party agent hosts, so they cannot inherit a
//! choice made in the Browser UI, and they resolve paths like
//! `Path.home() / ".my-llm-wiki" / "models" / "asr-zh"` on their own. Walking
//! through the anchor, that resolution lands on the chosen drive with no code
//! in those scripts knowing the root moved. The same holds for an MCP server
//! already registered against `%USERPROFILE%\.my-llm-wiki\bin\my-llm-wiki.exe`.
//!
//! State records real root paths rather than anchor paths, so ownership checks
//! stay consistent and a rebuilt anchor — after a Windows reinstall creates a
//! fresh profile, say — reattaches an existing install without rewriting it.

use std::fs;
use std::path::{Path, PathBuf};

use crate::error::{Result, SetupError};
use crate::link;

/// The directory name of the anchor inside the user's home.
pub const ANCHOR_NAME: &str = ".my-llm-wiki";

/// Resolve the anchor to the install root it stands for.
///
/// A plain directory is its own root, which is the default install and the
/// behaviour every existing path in the crate already assumes.
pub fn resolve(anchor: &Path) -> PathBuf {
    link::read_dir_link(anchor).unwrap_or_else(|| anchor.to_path_buf())
}

/// Expand and validate an install root the user asked for.
///
/// Returns the anchor itself when no root was requested, which keeps the
/// default install a plain directory with no link involved.
pub fn requested(anchor: &Path, home: &Path, requested: Option<&Path>) -> Result<PathBuf> {
    let Some(path) = requested else {
        return Ok(anchor.to_path_buf());
    };
    let expanded = if path == Path::new("~") {
        home.to_path_buf()
    } else if let Ok(relative) = path.strip_prefix("~") {
        home.join(relative)
    } else if path.is_absolute() {
        path.to_path_buf()
    } else {
        return Err(SetupError::InvalidInstallRoot(path.to_path_buf()));
    };
    let normalized = normalize(expanded);
    if normalized.file_name().is_none() {
        // A volume root such as `D:\` has nowhere to put the install and
        // nothing to remove on uninstall.
        return Err(SetupError::InvalidInstallRoot(normalized));
    }
    // Nesting the root inside the anchor makes the anchor a link into itself.
    if normalized != anchor && normalized.starts_with(anchor) {
        return Err(SetupError::InvalidInstallRoot(normalized));
    }
    Ok(normalized)
}

/// Make the anchor stand for `root`, creating the root if it is new.
///
/// Nothing here deletes user data. An anchor already holding an install is a
/// hard error the caller surfaces, because silently moving gigabytes of packs
/// or dropping them is not a decision this layer gets to make.
pub fn ensure_anchor(anchor: &Path, root: &Path) -> Result<()> {
    if root == anchor {
        // The anchor is the suite's own directory, so whatever it already
        // holds — logs, connector state, a registry left by an uninstall — is
        // ours to keep using.
        return fs::create_dir_all(root).map_err(|error| SetupError::io(root, error));
    }
    if !usable(root) {
        return Err(SetupError::InstallRootOccupied(root.to_path_buf()));
    }
    fs::create_dir_all(root).map_err(|error| SetupError::io(root, error))?;
    if let Some(current) = link::read_dir_link(anchor) {
        return if same_dir(&current, root) {
            Ok(())
        } else {
            Err(SetupError::InstallRootConflict {
                anchor: anchor.to_path_buf(),
                current,
                requested: root.to_path_buf(),
            })
        };
    }
    if anchor.exists() {
        if !is_empty_dir(anchor) {
            return Err(SetupError::InstallAnchorOccupied(anchor.to_path_buf()));
        }
        fs::remove_dir(anchor).map_err(|error| SetupError::io(anchor, error))?;
    }
    link::create_dir_link(anchor, root).map_err(|error| SetupError::io(anchor, error))
}

/// Whether an install can be placed at `root` without disturbing what is there.
///
/// A directory the user already filled with unrelated files is refused rather
/// than merged into, because setup writes and replaces whole subtrees such as
/// `skills/` beneath it.
pub fn usable(root: &Path) -> bool {
    !root.exists() || is_suite_root(root) || is_empty_dir(root)
}

/// Whether `root` already carries an installation this build can adopt — the
/// case after a Windows reinstall leaves the data volume intact.
pub fn is_suite_root(root: &Path) -> bool {
    root.join("setup-state.json").is_file()
}

fn is_empty_dir(path: &Path) -> bool {
    fs::read_dir(path).is_ok_and(|mut entries| entries.next().is_none())
}

fn same_dir(left: &Path, right: &Path) -> bool {
    match (fs::canonicalize(left), fs::canonicalize(right)) {
        (Ok(left), Ok(right)) => left == right,
        _ => left == right,
    }
}

/// Drop `.` and `..` without touching the filesystem.
///
/// The path may not exist yet, so `canonicalize` is not available; this only
/// has to make comparisons such as the containment check above meaningful.
fn normalize(path: PathBuf) -> PathBuf {
    use std::path::Component;
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                if !out.pop() {
                    out.push(Component::ParentDir);
                }
            }
            other => out.push(other),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_plain_anchor_is_its_own_root() {
        let temp = tempfile::tempdir().expect("temp dir");
        let anchor = temp.path().join(ANCHOR_NAME);
        fs::create_dir_all(&anchor).expect("anchor");
        assert_eq!(resolve(&anchor), anchor);
    }

    #[test]
    fn a_linked_anchor_resolves_to_the_install_root() {
        let temp = tempfile::tempdir().expect("temp dir");
        let anchor = temp.path().join(ANCHOR_NAME);
        let root = temp.path().join("volume").join("my-llm-wiki");

        ensure_anchor(&anchor, &root).expect("anchor");

        assert!(link::is_dir_link(&anchor));
        assert!(same_dir(&resolve(&anchor), &root));
    }

    #[test]
    fn re_pointing_the_anchor_at_the_same_root_is_idempotent() {
        let temp = tempfile::tempdir().expect("temp dir");
        let anchor = temp.path().join(ANCHOR_NAME);
        let root = temp.path().join("volume").join("my-llm-wiki");

        ensure_anchor(&anchor, &root).expect("anchor");
        ensure_anchor(&anchor, &root).expect("anchor again");

        assert!(link::is_dir_link(&anchor));
    }

    #[test]
    fn an_anchor_pointing_elsewhere_is_a_conflict() {
        let temp = tempfile::tempdir().expect("temp dir");
        let anchor = temp.path().join(ANCHOR_NAME);
        let first = temp.path().join("first");
        let second = temp.path().join("second");
        ensure_anchor(&anchor, &first).expect("anchor");

        let error = ensure_anchor(&anchor, &second).expect_err("conflict");

        assert!(matches!(error, SetupError::InstallRootConflict { .. }));
        assert!(link::links_to(&anchor, &first));
    }

    #[test]
    fn an_anchor_holding_data_is_never_replaced() {
        let temp = tempfile::tempdir().expect("temp dir");
        let anchor = temp.path().join(ANCHOR_NAME);
        fs::create_dir_all(&anchor).expect("anchor");
        fs::write(anchor.join("setup-state.json"), b"{}").expect("state");

        let error = ensure_anchor(&anchor, &temp.path().join("elsewhere")).expect_err("occupied");

        assert!(matches!(error, SetupError::InstallAnchorOccupied(_)));
        assert!(anchor.join("setup-state.json").is_file());
    }

    #[test]
    fn a_root_holding_unrelated_files_is_refused() {
        let temp = tempfile::tempdir().expect("temp dir");
        let anchor = temp.path().join(ANCHOR_NAME);
        let root = temp.path().join("documents");
        fs::create_dir_all(&root).expect("root");
        fs::write(root.join("notes.txt"), b"mine").expect("notes");

        let error = ensure_anchor(&anchor, &root).expect_err("occupied");

        assert!(matches!(error, SetupError::InstallRootOccupied(_)));
        assert!(root.join("notes.txt").is_file());
        assert!(!anchor.exists());
    }

    #[test]
    fn a_root_holding_a_previous_install_is_adopted() {
        let temp = tempfile::tempdir().expect("temp dir");
        let anchor = temp.path().join(ANCHOR_NAME);
        let root = temp.path().join("volume").join("my-llm-wiki");
        fs::create_dir_all(&root).expect("root");
        fs::write(root.join("setup-state.json"), b"{}").expect("state");

        ensure_anchor(&anchor, &root).expect("adopted");

        assert!(link::links_to(&anchor, &root));
        assert!(root.join("setup-state.json").is_file());
    }

    #[test]
    fn an_empty_anchor_makes_way_for_a_link() {
        let temp = tempfile::tempdir().expect("temp dir");
        let anchor = temp.path().join(ANCHOR_NAME);
        fs::create_dir_all(&anchor).expect("anchor");
        let root = temp.path().join("elsewhere");

        ensure_anchor(&anchor, &root).expect("anchor");

        assert!(link::links_to(&anchor, &root));
    }

    #[test]
    fn a_relative_request_is_rejected() {
        let temp = tempfile::tempdir().expect("temp dir");
        let anchor = temp.path().join(ANCHOR_NAME);
        let error = requested(&anchor, temp.path(), Some(Path::new("relative/path")))
            .expect_err("relative rejected");
        assert!(matches!(error, SetupError::InvalidInstallRoot(_)));
    }

    #[test]
    fn a_request_inside_the_anchor_is_rejected() {
        let temp = tempfile::tempdir().expect("temp dir");
        let anchor = temp.path().join(ANCHOR_NAME);
        let error = requested(&anchor, temp.path(), Some(&anchor.join("nested")))
            .expect_err("nested rejected");
        assert!(matches!(error, SetupError::InvalidInstallRoot(_)));
    }

    #[test]
    fn no_request_means_the_anchor_itself() {
        let temp = tempfile::tempdir().expect("temp dir");
        let anchor = temp.path().join(ANCHOR_NAME);
        assert_eq!(
            requested(&anchor, temp.path(), None).expect("default"),
            anchor
        );
    }

    #[test]
    fn a_tilde_request_expands_against_home() {
        let temp = tempfile::tempdir().expect("temp dir");
        let anchor = temp.path().join(ANCHOR_NAME);
        let resolved =
            requested(&anchor, temp.path(), Some(Path::new("~/volume/wiki"))).expect("expanded");
        assert_eq!(resolved, temp.path().join("volume").join("wiki"));
    }
}
