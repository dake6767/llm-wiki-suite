//! Directory links, the one primitive that lets a single installed tree serve
//! several locations.
//!
//! Two places need it. The suite home anchor (`~/.my-llm-wiki`) points at the
//! install root the user chose, so Skills running under a third-party agent
//! host keep resolving `Path.home() / ".my-llm-wiki"` without knowing anything
//! about that choice. Each host's `skills/<slug>` points at the one installed
//! copy under the install root, so the Skills Pack exists once instead of once
//! per host.
//!
//! Windows uses junctions rather than symbolic links: `CreateSymbolicLinkW`
//! needs either an elevated process or Developer Mode, while a junction needs
//! neither. Reading and detecting are already handled by `std` — it reports
//! `IO_REPARSE_TAG_MOUNT_POINT` as a symlink and `read_link` understands it —
//! so only creation needs platform code.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

/// Whether `path` is a link rather than a real directory.
///
/// Uses `symlink_metadata` so the link itself is inspected instead of whatever
/// it points at, and answers `false` for a path that does not exist.
pub fn is_dir_link(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|metadata| metadata.file_type().is_symlink())
}

/// The target of a directory link, or `None` when `path` is not a link.
///
/// The target is returned in a form that compares against paths the rest of
/// the crate builds: Windows reparse points store an NT-namespace path that
/// `std` hands back as a `\\?\` verbatim path, which would never equal the
/// plain `D:\...` the user picked.
pub fn read_dir_link(path: &Path) -> Option<PathBuf> {
    if !is_dir_link(path) {
        return None;
    }
    fs::read_link(path).ok().map(strip_verbatim)
}

/// Whether `link` is a link that resolves to `target`.
///
/// Canonicalizing both sides is the reliable comparison, because it settles
/// case, short names, and prefix differences at once. A dangling link cannot be
/// canonicalized, so the stored target is compared directly as a fallback and
/// the answer is then simply "no" for anything that no longer exists.
pub fn links_to(link: &Path, target: &Path) -> bool {
    let Some(stored) = read_dir_link(link) else {
        return false;
    };
    match (fs::canonicalize(link), fs::canonicalize(target)) {
        (Ok(left), Ok(right)) => left == right,
        _ => stored == strip_verbatim(target.to_path_buf()),
    }
}

/// Point `link` at `target`, creating the parent directory if needed.
///
/// `target` must already exist: a junction to a missing directory is accepted
/// by Windows but resolves to nothing, and failing here instead produces an
/// error the caller can fall back from.
pub fn create_dir_link(link: &Path, target: &Path) -> io::Result<()> {
    if !target.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!("link target is not a directory: {}", target.display()),
        ));
    }
    if let Some(parent) = link.parent() {
        fs::create_dir_all(parent)?;
    }
    create(link, target)
}

/// Remove the link entry and nothing else.
///
/// This never recurses: the target holds the only installed copy of whatever
/// is linked, so following the link while deleting would destroy the install
/// instead of detaching one reference to it. `remove_dir` on Windows and
/// `remove_file` on Unix both operate on the link itself.
pub fn remove_dir_link(link: &Path) -> io::Result<()> {
    if !is_dir_link(link) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("not a directory link: {}", link.display()),
        ));
    }
    #[cfg(windows)]
    {
        fs::remove_dir(link)
    }
    #[cfg(not(windows))]
    {
        fs::remove_file(link)
    }
}

#[cfg(windows)]
fn create(link: &Path, target: &Path) -> io::Result<()> {
    junction::create(target, link)
}

#[cfg(not(windows))]
fn create(link: &Path, target: &Path) -> io::Result<()> {
    std::os::unix::fs::symlink(target, link)
}

#[cfg(windows)]
fn strip_verbatim(path: PathBuf) -> PathBuf {
    let Some(text) = path.to_str() else {
        return path;
    };
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{rest}"));
    }
    match text.strip_prefix(r"\\?\") {
        // Only a drive path survives dropping the prefix; device paths such as
        // \\?\Volume{...} mean something different without it.
        Some(rest) if rest.as_bytes().get(1) == Some(&b':') => PathBuf::from(rest),
        _ => path,
    }
}

#[cfg(not(windows))]
fn strip_verbatim(path: PathBuf) -> PathBuf {
    path
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn creates_reads_and_detects_a_directory_link() {
        let temp = tempfile::tempdir().expect("temp dir");
        let target = temp.path().join("target");
        fs::create_dir_all(&target).expect("target");
        fs::write(target.join("file.txt"), b"payload").expect("payload");
        let link = temp.path().join("link");

        create_dir_link(&link, &target).expect("create link");

        assert!(is_dir_link(&link));
        assert!(!is_dir_link(&target));
        assert!(links_to(&link, &target));
        assert_eq!(
            fs::read(link.join("file.txt")).expect("read through link"),
            b"payload"
        );
    }

    #[test]
    fn removing_a_link_leaves_the_target_intact() {
        let temp = tempfile::tempdir().expect("temp dir");
        let target = temp.path().join("target");
        fs::create_dir_all(&target).expect("target");
        fs::write(target.join("file.txt"), b"payload").expect("payload");
        let link = temp.path().join("link");
        create_dir_link(&link, &target).expect("create link");

        remove_dir_link(&link).expect("remove link");

        assert!(!link.exists());
        assert!(!is_dir_link(&link));
        assert!(target.join("file.txt").is_file());
    }

    #[test]
    fn refuses_to_remove_a_real_directory() {
        let temp = tempfile::tempdir().expect("temp dir");
        let real = temp.path().join("real");
        fs::create_dir_all(&real).expect("real");

        let error = remove_dir_link(&real).expect_err("real directory rejected");

        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        assert!(real.is_dir());
    }

    #[test]
    fn refuses_a_missing_target() {
        let temp = tempfile::tempdir().expect("temp dir");
        let error = create_dir_link(&temp.path().join("link"), &temp.path().join("missing"))
            .expect_err("missing target rejected");
        assert_eq!(error.kind(), io::ErrorKind::NotFound);
    }

    #[test]
    fn reports_no_target_for_a_real_directory() {
        let temp = tempfile::tempdir().expect("temp dir");
        let real = temp.path().join("real");
        fs::create_dir_all(&real).expect("real");
        assert!(read_dir_link(&real).is_none());
        assert!(!links_to(&real, &real));
    }
}
