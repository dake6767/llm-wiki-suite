use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use reqwest::blocking::Client;
use semver::Version;
use sha2::{Digest as _, Sha256};
use wait_timeout::ChildExt as _;
use zip::ZipArchive;

use crate::error::{Result, SetupError};
use crate::model::{DistributionManifest, ManualAction, OwnedPack, PackArtifact, PackManualAction};

const MANIFEST_MAX_BYTES: u64 = 8 * 1024 * 1024;
const ARCHIVE_OVERHEAD_BYTES: u64 = 1;
const PACK_MARKER: &str = ".my-llm-wiki-pack.json";

#[derive(serde::Serialize, serde::Deserialize)]
struct PackMarker {
    schema: u32,
    id: String,
    version: String,
    sha256: String,
}

pub(crate) fn fetch_manifest(sources: &[String]) -> Result<DistributionManifest> {
    let bytes = fetch_from_sources("distribution manifest", sources, MANIFEST_MAX_BYTES)?;
    let manifest: DistributionManifest = serde_json::from_slice(&bytes)
        .map_err(|err| SetupError::InvalidManifest(err.to_string()))?;
    validate_manifest(&manifest)?;
    Ok(manifest)
}

pub(crate) fn validate_manifest(manifest: &DistributionManifest) -> Result<()> {
    if manifest.schema != 1 {
        return Err(SetupError::InvalidManifest(format!(
            "unsupported schema {}",
            manifest.schema
        )));
    }
    if manifest.channel != "stable" {
        return Err(SetupError::InvalidManifest(format!(
            "unsupported channel {}",
            manifest.channel
        )));
    }
    for (label, value) in [
        (
            "distribution_version",
            manifest.distribution_version.as_str(),
        ),
        ("browser_version", manifest.browser_version.as_str()),
        ("skills_pack_version", manifest.skills_pack_version.as_str()),
    ] {
        Version::parse(value).map_err(|err| {
            SetupError::InvalidManifest(format!("invalid {label} {value:?}: {err}"))
        })?;
    }
    if manifest.browser_version != manifest.distribution_version
        || manifest.skills_pack_version != manifest.distribution_version
    {
        return Err(SetupError::InvalidManifest(
            "Browser, Skills Pack, and distribution versions must match".into(),
        ));
    }
    for artifact in &manifest.artifacts {
        if artifact.id.is_empty()
            || artifact.platform.is_empty()
            || artifact.architecture.is_empty()
            || artifact.urls.is_empty()
            || artifact.sha256.len() != 64
            || !artifact
                .sha256
                .bytes()
                .all(|value| value.is_ascii_hexdigit())
            || artifact.size == 0
            || artifact.installed_size == 0
        {
            return Err(SetupError::InvalidManifest(format!(
                "invalid artifact declaration: {}/{}",
                artifact.id, artifact.version
            )));
        }
        Version::parse(&artifact.version).map_err(|err| {
            SetupError::InvalidManifest(format!(
                "invalid artifact version {}: {err}",
                artifact.version
            ))
        })?;
        validate_argv_maps(artifact)?;
        for probe in &artifact.probes {
            if !artifact.commands.contains_key(&probe.command) {
                return Err(SetupError::InvalidManifest(format!(
                    "probe references undeclared command {} in {}",
                    probe.command, artifact.id
                )));
            }
        }
    }
    Ok(())
}

fn validate_argv_maps(artifact: &PackArtifact) -> Result<()> {
    for (name, argv) in artifact
        .commands
        .iter()
        .chain(artifact.python_profiles.iter())
    {
        if name.is_empty() || argv.is_empty() || argv.iter().any(|value| value.is_empty()) {
            return Err(SetupError::InvalidManifest(format!(
                "invalid argv for {} in {}",
                name, artifact.id
            )));
        }
        if !argv[0].starts_with("{pack}/") && !argv[0].starts_with("{pack}\\") {
            return Err(SetupError::InvalidManifest(format!(
                "executable for {} must be rooted in {{pack}}",
                name
            )));
        }
    }
    Ok(())
}

pub(crate) fn select_artifact<'a>(
    manifest: &'a DistributionManifest,
    id: &str,
) -> Result<&'a PackArtifact> {
    let (platform, architecture) = target();
    manifest
        .artifacts
        .iter()
        .find(|artifact| {
            artifact.id == id
                && artifact.platform == platform
                && artifact.architecture == architecture
        })
        .ok_or_else(|| SetupError::PackUnavailable {
            pack: id.to_owned(),
            platform,
            architecture,
        })
}

pub(crate) fn install_pack(suite_home: &Path, artifact: &PackArtifact) -> Result<OwnedPack> {
    let versions = suite_home.join("packs").join(&artifact.id).join("versions");
    let destination = versions.join(&artifact.version);
    if destination.is_dir() && check_pack_installation(&destination, artifact).is_ok() {
        return Ok(owned_pack(destination, artifact));
    }

    let archive = download_archive(suite_home, artifact)?;
    fs::create_dir_all(&versions).map_err(|err| SetupError::io(&versions, err))?;
    let available =
        fs2::available_space(&versions).map_err(|error| SetupError::io(&versions, error))?;
    if available < artifact.installed_size {
        return Err(SetupError::Download {
            label: artifact.id.clone(),
            detail: format!(
                "not enough free space to expand pack: need {} bytes, have {available} bytes",
                artifact.installed_size
            ),
        });
    }
    let stage = versions.join(format!(
        ".{}-stage-{}",
        artifact.version,
        std::process::id()
    ));
    if stage.exists() {
        fs::remove_dir_all(&stage).map_err(|err| SetupError::io(&stage, err))?;
    }
    fs::create_dir_all(&stage).map_err(|err| SetupError::io(&stage, err))?;
    if let Err(error) = extract_zip(&archive, &stage, artifact.installed_size)
        .and_then(|()| check_pack(&stage, artifact))
        .and_then(|()| write_pack_marker(&stage, artifact))
    {
        let _ = fs::remove_dir_all(&stage);
        return Err(error);
    }

    let previous = versions.join(format!(
        ".{}-previous-{}",
        artifact.version,
        std::process::id()
    ));
    if destination.exists() {
        fs::rename(&destination, &previous).map_err(|err| SetupError::io(&destination, err))?;
    }
    if let Err(err) = fs::rename(&stage, &destination) {
        if previous.exists() {
            let _ = fs::rename(&previous, &destination);
        }
        return Err(SetupError::io(&stage, err));
    }
    if previous.exists() {
        fs::remove_dir_all(&previous).map_err(|err| SetupError::io(&previous, err))?;
    }
    Ok(owned_pack(destination, artifact))
}

pub(crate) fn prune_pack_versions(suite_home: &Path, pack: &OwnedPack) -> Result<()> {
    let versions = suite_home
        .join("packs")
        .join(&pack.artifact.id)
        .join("versions");
    if !versions.is_dir() {
        return Ok(());
    }
    for entry in fs::read_dir(&versions).map_err(|err| SetupError::io(&versions, err))? {
        let path = entry.map_err(|err| SetupError::io(&versions, err))?.path();
        if path != pack.path && path.is_dir() {
            fs::remove_dir_all(&path).map_err(|err| SetupError::io(&path, err))?;
        }
    }
    Ok(())
}

fn owned_pack(path: PathBuf, artifact: &PackArtifact) -> OwnedPack {
    OwnedPack {
        version: artifact.version.clone(),
        path,
        digest: artifact.sha256.to_ascii_lowercase(),
        artifact: artifact.clone(),
    }
}

pub(crate) fn check_owned_pack(pack: &OwnedPack) -> Result<()> {
    if pack.digest != pack.artifact.sha256.to_ascii_lowercase() {
        return Err(SetupError::InvalidState(format!(
            "pack digest differs from artifact: {}",
            pack.artifact.id
        )));
    }
    check_pack_installation(&pack.path, &pack.artifact)
}

fn write_pack_marker(root: &Path, artifact: &PackArtifact) -> Result<()> {
    let path = root.join(PACK_MARKER);
    let marker = PackMarker {
        schema: 1,
        id: artifact.id.clone(),
        version: artifact.version.clone(),
        sha256: artifact.sha256.to_ascii_lowercase(),
    };
    let data =
        serde_json::to_vec_pretty(&marker).map_err(|error| SetupError::json(&path, error))?;
    fs::write(&path, data).map_err(|error| SetupError::io(&path, error))
}

fn check_pack_installation(root: &Path, artifact: &PackArtifact) -> Result<()> {
    let path = root.join(PACK_MARKER);
    let data = fs::read(&path).map_err(|error| SetupError::io(&path, error))?;
    let marker: PackMarker =
        serde_json::from_slice(&data).map_err(|error| SetupError::json(&path, error))?;
    if marker.schema != 1
        || marker.id != artifact.id
        || marker.version != artifact.version
        || marker.sha256 != artifact.sha256.to_ascii_lowercase()
    {
        return Err(SetupError::InvalidState(format!(
            "pack marker differs from manifest: {}",
            artifact.id
        )));
    }
    check_pack(root, artifact)
}

pub(crate) fn actions(pack: &OwnedPack) -> Vec<ManualAction> {
    pack.artifact
        .manual_actions
        .iter()
        .map(|action| render_action(action, &pack.path))
        .collect()
}

fn render_action(action: &PackManualAction, root: &Path) -> ManualAction {
    ManualAction {
        id: action.id.clone(),
        title: action.title.clone(),
        detail: action.detail.replace("{pack}", &root.to_string_lossy()),
    }
}

fn check_pack(root: &Path, artifact: &PackArtifact) -> Result<()> {
    for (name, argv) in artifact
        .commands
        .iter()
        .chain(artifact.python_profiles.iter())
    {
        let resolved = resolve_argv(root, argv)?;
        let executable = Path::new(&resolved[0]);
        if !executable.is_file() {
            return Err(SetupError::Probe {
                pack: artifact.id.clone(),
                detail: format!("{} executable is missing: {}", name, executable.display()),
            });
        }
    }
    for probe in &artifact.probes {
        let base = artifact
            .commands
            .get(&probe.command)
            .ok_or_else(|| SetupError::Probe {
                pack: artifact.id.clone(),
                detail: format!("probe command is undeclared: {}", probe.command),
            })?;
        let mut argv = resolve_argv(root, base)?;
        argv.extend(probe.args.iter().map(|value| expand(root, value)));
        let mut command = Command::new(&argv[0]);
        command
            .args(&argv[1..])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        for values in artifact.environment.values() {
            for (key, value) in values {
                command.env(key, expand(root, value));
            }
        }
        let mut child = command.spawn().map_err(|err| SetupError::Probe {
            pack: artifact.id.clone(),
            detail: format!("cannot run {}: {err}", probe.command),
        })?;
        let status =
            child
                .wait_timeout(Duration::from_secs(30))
                .map_err(|err| SetupError::Probe {
                    pack: artifact.id.clone(),
                    detail: format!("cannot wait for {}: {err}", probe.command),
                })?;
        let Some(status) = status else {
            let _ = child.kill();
            let _ = child.wait();
            return Err(SetupError::Probe {
                pack: artifact.id.clone(),
                detail: format!("{} timed out", probe.command),
            });
        };
        if !status.success() {
            return Err(SetupError::Probe {
                pack: artifact.id.clone(),
                detail: format!("{} exited with {status}", probe.command),
            });
        }
    }
    Ok(())
}

fn resolve_argv(root: &Path, argv: &[String]) -> Result<Vec<String>> {
    let canonical_root = root
        .canonicalize()
        .map_err(|err| SetupError::io(root, err))?;
    let mut resolved = Vec::with_capacity(argv.len());
    for value in argv {
        let expanded = expand(root, value);
        if value.starts_with("{pack}/") || value.starts_with("{pack}\\") {
            let path = PathBuf::from(&expanded);
            let canonical = path
                .canonicalize()
                .map_err(|err| SetupError::io(&path, err))?;
            if canonical != canonical_root && !canonical.starts_with(&canonical_root) {
                return Err(SetupError::Probe {
                    pack: root.display().to_string(),
                    detail: format!("command path escapes pack: {}", path.display()),
                });
            }
            resolved.push(canonical.to_string_lossy().into_owned());
        } else {
            resolved.push(expanded);
        }
    }
    Ok(resolved)
}

fn expand(root: &Path, value: &str) -> String {
    value.replace("{pack}", &root.to_string_lossy())
}

fn download_archive(suite_home: &Path, artifact: &PackArtifact) -> Result<PathBuf> {
    let downloads = suite_home.join("downloads");
    fs::create_dir_all(&downloads).map_err(|err| SetupError::io(&downloads, err))?;
    let destination = downloads.join(format!("{}.zip", artifact.sha256.to_ascii_lowercase()));
    if destination.is_file() {
        if verify_sha256(&destination, &artifact.sha256).is_ok() {
            return Ok(destination);
        }
        fs::remove_file(&destination).map_err(|err| SetupError::io(&destination, err))?;
    }
    let required = artifact
        .size
        .checked_add(artifact.installed_size)
        .ok_or_else(|| SetupError::InvalidManifest("pack size overflows u64".into()))?;
    let available =
        fs2::available_space(&downloads).map_err(|error| SetupError::io(&downloads, error))?;
    if available < required {
        return Err(SetupError::Download {
            label: artifact.id.clone(),
            detail: format!("not enough free space: need {required} bytes, have {available} bytes"),
        });
    }
    let temporary = downloads.join(format!(".{}.part", artifact.sha256));
    download_from_sources(
        &artifact.id,
        &artifact.urls,
        &temporary,
        artifact.size,
        &artifact.sha256,
    )?;
    fs::rename(&temporary, &destination).map_err(|err| SetupError::io(&temporary, err))?;
    Ok(destination)
}

fn download_from_sources(
    label: &str,
    sources: &[String],
    destination: &Path,
    exact_bytes: u64,
    expected_sha256: &str,
) -> Result<()> {
    let client = download_client(label)?;
    let mut errors = Vec::new();
    for source in sources {
        match stream_source(&client, source, destination, exact_bytes, expected_sha256) {
            Ok(()) => return Ok(()),
            Err(error) => {
                let _ = fs::remove_file(destination);
                errors.push(format!("{source}: {error}"));
            }
        }
    }
    Err(SetupError::Download {
        label: label.to_owned(),
        detail: errors.join("; "),
    })
}

fn stream_source(
    client: &Client,
    source: &str,
    destination: &Path,
    exact_bytes: u64,
    expected_sha256: &str,
) -> std::result::Result<(), String> {
    let reader: Box<dyn Read> = if let Some(path) = source.strip_prefix("file://") {
        Box::new(File::open(path).map_err(|error| error.to_string())?)
    } else if Path::new(source).is_file() {
        Box::new(File::open(source).map_err(|error| error.to_string())?)
    } else {
        let response = client
            .get(source)
            .send()
            .and_then(|response| response.error_for_status())
            .map_err(|error| error.to_string())?;
        if response
            .content_length()
            .is_some_and(|size| size != exact_bytes)
        {
            return Err(format!("response size differs from {exact_bytes} bytes"));
        }
        Box::new(response)
    };
    let mut reader = reader;
    let mut output = File::create(destination).map_err(|error| error.to_string())?;
    let mut hash = Sha256::new();
    let mut total = 0u64;
    let mut buffer = [0u8; 1024 * 1024];
    loop {
        let read = reader
            .read(&mut buffer)
            .map_err(|error| error.to_string())?;
        if read == 0 {
            break;
        }
        total = total
            .checked_add(read as u64)
            .ok_or_else(|| "download size overflow".to_owned())?;
        if total > exact_bytes {
            return Err(format!("content exceeds {exact_bytes} bytes"));
        }
        hash.update(&buffer[..read]);
        output
            .write_all(&buffer[..read])
            .map_err(|error| error.to_string())?;
    }
    output.sync_all().map_err(|error| error.to_string())?;
    if total != exact_bytes {
        return Err(format!("expected {exact_bytes} bytes, got {total}"));
    }
    let actual = format!("{:x}", hash.finalize());
    if actual != expected_sha256.to_ascii_lowercase() {
        return Err(format!(
            "SHA-256 mismatch: expected {}, got {actual}",
            expected_sha256.to_ascii_lowercase()
        ));
    }
    Ok(())
}

fn fetch_from_sources(label: &str, sources: &[String], max_bytes: u64) -> Result<Vec<u8>> {
    let client = download_client(label)?;
    let mut errors = Vec::new();
    for source in sources {
        match read_source(&client, source, max_bytes) {
            Ok(bytes) => return Ok(bytes),
            Err(error) => errors.push(format!("{source}: {error}")),
        }
    }
    Err(SetupError::Download {
        label: label.to_owned(),
        detail: errors.join("; "),
    })
}

fn download_client(label: &str) -> Result<Client> {
    Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(300))
        .user_agent(format!("my-llm-wiki-setup/{0}", env!("CARGO_PKG_VERSION")))
        .build()
        .map_err(|err| SetupError::Download {
            label: label.to_owned(),
            detail: err.to_string(),
        })
}

fn read_source(
    client: &Client,
    source: &str,
    max_bytes: u64,
) -> std::result::Result<Vec<u8>, String> {
    if let Some(path) = source.strip_prefix("file://") {
        return read_limited(File::open(path).map_err(|err| err.to_string())?, max_bytes);
    }
    let path = Path::new(source);
    if path.is_file() {
        return read_limited(File::open(path).map_err(|err| err.to_string())?, max_bytes);
    }
    let response = client
        .get(source)
        .send()
        .and_then(|response| response.error_for_status())
        .map_err(|err| err.to_string())?;
    if response
        .content_length()
        .is_some_and(|size| size > max_bytes)
    {
        return Err(format!("declared response exceeds {max_bytes} bytes"));
    }
    read_limited(response, max_bytes)
}

fn read_limited(mut reader: impl Read, max_bytes: u64) -> std::result::Result<Vec<u8>, String> {
    let mut bytes = Vec::new();
    reader
        .by_ref()
        .take(max_bytes + ARCHIVE_OVERHEAD_BYTES)
        .read_to_end(&mut bytes)
        .map_err(|err| err.to_string())?;
    if bytes.len() as u64 > max_bytes {
        return Err(format!("content exceeds {max_bytes} bytes"));
    }
    Ok(bytes)
}

fn verify_sha256(path: &Path, expected: &str) -> Result<()> {
    let mut file = File::open(path).map_err(|err| SetupError::io(path, err))?;
    let mut hash = Sha256::new();
    std::io::copy(&mut file, &mut HashWriter(&mut hash))
        .map_err(|err| SetupError::io(path, err))?;
    let actual = format!("{:x}", hash.finalize());
    if actual != expected.to_ascii_lowercase() {
        return Err(SetupError::Checksum {
            path: path.to_path_buf(),
            expected: expected.to_ascii_lowercase(),
            actual,
        });
    }
    Ok(())
}

struct HashWriter<'a>(&'a mut Sha256);

impl Write for HashWriter<'_> {
    fn write(&mut self, buffer: &[u8]) -> std::io::Result<usize> {
        self.0.update(buffer);
        Ok(buffer.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

fn extract_zip(archive: &Path, destination: &Path, expected_size: u64) -> Result<()> {
    let file = File::open(archive).map_err(|err| SetupError::io(archive, err))?;
    let mut zip =
        ZipArchive::new(file).map_err(|err| SetupError::InvalidManifest(err.to_string()))?;
    let actual_size = (0..zip.len()).try_fold(0u64, |total, index| {
        let file = zip
            .by_index(index)
            .map_err(|err| SetupError::InvalidManifest(err.to_string()))?;
        total.checked_add(file.size()).ok_or_else(|| {
            SetupError::InvalidManifest("archive expanded size overflows u64".into())
        })
    })?;
    if actual_size != expected_size {
        return Err(SetupError::InvalidManifest(format!(
            "archive expanded size mismatch: expected {expected_size}, got {actual_size}"
        )));
    }
    for index in 0..zip.len() {
        let mut entry = zip
            .by_index(index)
            .map_err(|err| SetupError::InvalidManifest(err.to_string()))?;
        let Some(relative) = entry.enclosed_name() else {
            return Err(SetupError::UnsafeArchive {
                archive: archive.to_path_buf(),
                member: entry.name().to_owned(),
            });
        };
        let path = destination.join(relative);
        if entry.is_dir() {
            fs::create_dir_all(&path).map_err(|err| SetupError::io(&path, err))?;
            continue;
        }
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|err| SetupError::io(parent, err))?;
        }
        let mut output = File::create(&path).map_err(|err| SetupError::io(&path, err))?;
        std::io::copy(&mut entry, &mut output).map_err(|err| SetupError::io(&path, err))?;
        #[cfg(unix)]
        if let Some(mode) = entry.unix_mode() {
            use std::os::unix::fs::PermissionsExt as _;
            fs::set_permissions(&path, fs::Permissions::from_mode(mode & 0o777))
                .map_err(|err| SetupError::io(&path, err))?;
        }
    }
    Ok(())
}

pub(crate) fn target() -> (String, String) {
    let platform = match std::env::consts::OS {
        "macos" => "darwin",
        value => value,
    };
    let architecture = match std::env::consts::ARCH {
        "x86_64" => "x64",
        "aarch64" => "arm64",
        value => value,
    };
    (platform.to_owned(), architecture.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolves_every_declared_pack_path_and_rejects_missing_members() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().join("pack");
        let runtime = root.join("runtime");
        let runner = root.join("documents/runner.py");
        fs::create_dir_all(&runtime).unwrap();
        fs::create_dir_all(runner.parent().unwrap()).unwrap();
        fs::write(runtime.join("python"), b"python").unwrap();
        fs::write(&runner, b"runner").unwrap();
        let argv = vec![
            "{pack}/runtime/python".into(),
            "{pack}/documents/runner.py".into(),
            "--version".into(),
        ];

        let resolved = resolve_argv(&root, &argv).unwrap();
        assert_eq!(
            Path::new(&resolved[0]),
            runtime.join("python").canonicalize().unwrap()
        );
        assert_eq!(Path::new(&resolved[1]), runner.canonicalize().unwrap());
        assert_eq!(resolved[2], "--version");

        fs::remove_file(root.join("documents/runner.py")).unwrap();
        assert!(resolve_argv(&root, &argv).is_err());
    }

    #[test]
    fn rejects_declared_pack_paths_that_escape_the_pack() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().join("pack");
        fs::create_dir_all(&root).unwrap();
        fs::write(temporary.path().join("outside"), b"outside").unwrap();
        let argv = vec!["{pack}/../outside".into()];

        assert!(matches!(
            resolve_argv(&root, &argv),
            Err(SetupError::Probe { .. })
        ));
    }
}
