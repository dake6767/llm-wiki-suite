use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use reqwest::StatusCode;
use reqwest::blocking::Client;
use reqwest::header::{CONTENT_RANGE, RANGE};
use semver::Version;
use sha2::{Digest as _, Sha256};
use wait_timeout::ChildExt as _;
use zip::ZipArchive;

use crate::error::{Result, SetupError};
use crate::model::{DistributionManifest, ManualAction, OwnedPack, PackArtifact, PackManualAction};

const MANIFEST_MAX_BYTES: u64 = 8 * 1024 * 1024;
const ARCHIVE_OVERHEAD_BYTES: u64 = 1;
const PACK_IO_BUFFER_BYTES: usize = 1024 * 1024;
const PACK_MARKER: &str = ".my-llm-wiki-pack.json";
// `reqwest::blocking` applies its client timeout to the header phase and then
// again to every single `Read::read` call, so this is a stall budget, not a
// total transfer budget. A slow but live link keeps downloading indefinitely
// while a dead socket fails within one budget instead of hanging for hours.
// Never raise it into "whole download" territory: a stalled read blocks the
// worker thread and no progress event, health check, or stop request can be
// observed until it returns.
const NETWORK_STALL_TIMEOUT: Duration = Duration::from_secs(90);
const METADATA_STALL_TIMEOUT: Duration = Duration::from_secs(45);

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum PackProgress {
    CheckingExisting,
    Downloading(u8),
    /// One download source failed; `remaining` alternates are still untried.
    SwitchingSource {
        remaining: usize,
    },
    VerifyingArchive,
    Extracting(u8),
    HealthChecking,
    VerifyingTool {
        name: String,
        current: u32,
        total: u32,
    },
}

/// Returns true once the caller has asked the running operation to stop. Long
/// loops poll it between chunks so a stop request lands without waiting for the
/// whole pack.
pub(crate) type Cancel<'a> = &'a dyn Fn() -> bool;

pub(crate) fn never_cancelled() -> impl Fn() -> bool {
    || false
}

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
        ("pack_version", manifest.pack_version.as_str()),
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
        if artifact.version != manifest.pack_version {
            return Err(SetupError::InvalidManifest(format!(
                "artifact version {} differs from pack version {}",
                artifact.version, manifest.pack_version
            )));
        }
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
    install_pack_with_progress(suite_home, artifact, &never_cancelled(), |_| {})
}

pub(crate) fn install_pack_with_progress(
    suite_home: &Path,
    artifact: &PackArtifact,
    cancel: Cancel<'_>,
    progress: impl Fn(PackProgress),
) -> Result<OwnedPack> {
    let versions = suite_home.join("packs").join(&artifact.id).join("versions");
    let destination = versions.join(&artifact.version);
    progress(PackProgress::CheckingExisting);
    if destination.is_dir() && check_pack_installation(&destination, artifact).is_ok() {
        return Ok(owned_pack(destination, artifact));
    }

    let archive = download_archive(suite_home, artifact, cancel, &progress)?;
    if cancel() {
        return Err(SetupError::Cancelled);
    }
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
    progress(PackProgress::Extracting(0));
    let prepared = extract_zip(&archive, &stage, artifact.installed_size, |percent| {
        progress(PackProgress::Extracting(percent))
    })
    .and_then(|()| {
        progress(PackProgress::HealthChecking);
        check_pack_with_progress(&stage, artifact, &progress)
    })
    .and_then(|()| write_pack_marker(&stage, artifact));
    if let Err(error) = prepared {
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
    check_pack_with_progress(root, artifact, &|_| {})
}

fn check_pack_with_progress(
    root: &Path,
    artifact: &PackArtifact,
    progress: &impl Fn(PackProgress),
) -> Result<()> {
    let total = artifact
        .commands
        .len()
        .saturating_add(artifact.python_profiles.len())
        .saturating_add(artifact.probes.len()) as u32;
    let mut completed = 0u32;
    for (name, argv) in artifact
        .commands
        .iter()
        .chain(artifact.python_profiles.iter())
    {
        progress(PackProgress::VerifyingTool {
            name: name.clone(),
            current: completed,
            total,
        });
        let resolved = resolve_argv(root, argv)?;
        let executable = Path::new(&resolved[0]);
        if !executable.is_file() {
            return Err(SetupError::Probe {
                pack: artifact.id.clone(),
                detail: format!("{} executable is missing: {}", name, executable.display()),
            });
        }
        completed += 1;
    }
    for probe in &artifact.probes {
        progress(PackProgress::VerifyingTool {
            name: probe.command.clone(),
            current: completed,
            total,
        });
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
        crate::process::hide_console(&mut command);
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
        completed += 1;
    }
    if total > 0 {
        progress(PackProgress::VerifyingTool {
            name: String::new(),
            current: total,
            total,
        });
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

pub(crate) fn resolve_python_profile(
    pack: &OwnedPack,
    profile: &str,
) -> Result<(Vec<String>, BTreeMap<String, String>)> {
    let raw = pack
        .artifact
        .python_profiles
        .get(profile)
        .ok_or_else(|| SetupError::Probe {
            pack: pack.artifact.id.clone(),
            detail: format!("pack does not declare Python profile {profile}"),
        })?;
    let argv = resolve_argv(&pack.path, raw)?;
    let environment = pack
        .artifact
        .environment
        .get(profile)
        .into_iter()
        .flatten()
        .map(|(key, value)| (key.clone(), expand(&pack.path, value)))
        .collect();
    Ok((argv, environment))
}

fn expand(root: &Path, value: &str) -> String {
    value.replace("{pack}", &root.to_string_lossy())
}

fn download_archive(
    suite_home: &Path,
    artifact: &PackArtifact,
    cancel: Cancel<'_>,
    progress: &impl Fn(PackProgress),
) -> Result<PathBuf> {
    let downloads = suite_home.join("downloads");
    fs::create_dir_all(&downloads).map_err(|err| SetupError::io(&downloads, err))?;
    let destination = downloads.join(format!("{}.zip", artifact.sha256.to_ascii_lowercase()));
    if destination.is_file() {
        progress(PackProgress::VerifyingArchive);
        if verify_sha256(&destination, &artifact.sha256).is_ok() {
            return Ok(destination);
        }
        fs::remove_file(&destination).map_err(|err| SetupError::io(&destination, err))?;
    }
    let temporary = downloads.join(format!(".{}.part", artifact.sha256));
    let partial_size = match temporary.metadata() {
        Ok(metadata) if metadata.len() == artifact.size => {
            progress(PackProgress::VerifyingArchive);
            if verify_sha256(&temporary, &artifact.sha256).is_ok() {
                fs::rename(&temporary, &destination)
                    .map_err(|err| SetupError::io(&temporary, err))?;
                return Ok(destination);
            }
            fs::remove_file(&temporary).map_err(|err| SetupError::io(&temporary, err))?;
            0
        }
        Ok(metadata) if metadata.len() < artifact.size => metadata.len(),
        Ok(_) => {
            fs::remove_file(&temporary).map_err(|err| SetupError::io(&temporary, err))?;
            0
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => 0,
        Err(error) => return Err(SetupError::io(&temporary, error)),
    };
    let remaining_download = artifact.size.saturating_sub(partial_size);
    let required = remaining_download
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
    download_from_sources(
        &artifact.id,
        &artifact.urls,
        &temporary,
        artifact.size,
        &artifact.sha256,
        cancel,
        progress,
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
    cancel: Cancel<'_>,
    progress: &impl Fn(PackProgress),
) -> Result<()> {
    let client = download_client(label, NETWORK_STALL_TIMEOUT)?;
    let mut errors = Vec::new();
    for (index, source) in sources.iter().enumerate() {
        if cancel() {
            return Err(SetupError::Cancelled);
        }
        let partial = destination.metadata().map(|item| item.len()).unwrap_or(0);
        progress(PackProgress::Downloading(percent(partial, exact_bytes)));
        let mut result = stream_source(
            &client,
            source,
            destination,
            exact_bytes,
            expected_sha256,
            cancel,
            progress,
        );
        // A complete-size .part with a bad digest cannot be resumed. Discard
        // only that corrupt file and retry this source once from byte zero.
        // Short partials survive network errors and process restarts.
        if result.is_err()
            && !cancel()
            && destination
                .metadata()
                .is_ok_and(|metadata| metadata.len() == exact_bytes)
        {
            let _ = fs::remove_file(destination);
            progress(PackProgress::Downloading(0));
            result = stream_source(
                &client,
                source,
                destination,
                exact_bytes,
                expected_sha256,
                cancel,
                progress,
            );
        }
        match result {
            Ok(()) => return Ok(()),
            Err(error) => {
                if cancel() {
                    return Err(SetupError::Cancelled);
                }
                errors.push(format!("{source}: {error}"));
                // Falling back is invisible from the outside: the byte counter
                // stops and the stalled source has already eaten a stall
                // budget. Say so, so the wait reads as work rather than a hang.
                let remaining = sources.len() - index - 1;
                if remaining > 0 {
                    progress(PackProgress::SwitchingSource { remaining });
                }
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
    cancel: Cancel<'_>,
    progress: &impl Fn(PackProgress),
) -> std::result::Result<(), String> {
    let mut offset = destination.metadata().map(|item| item.len()).unwrap_or(0);
    if offset > exact_bytes {
        fs::remove_file(destination).map_err(|error| error.to_string())?;
        offset = 0;
    }
    let reader: Box<dyn Read> = if let Some(path) = source.strip_prefix("file://") {
        local_source_reader(Path::new(path), offset)?
    } else if Path::new(source).is_file() {
        local_source_reader(Path::new(source), offset)?
    } else {
        let mut request = client.get(source);
        if offset > 0 {
            request = request.header(RANGE, format!("bytes={offset}-"));
        }
        let response = request.send().map_err(|error| error.to_string())?;
        if offset > 0 && response.status() == StatusCode::PARTIAL_CONTENT {
            let expected_length = exact_bytes - offset;
            if response
                .content_length()
                .is_some_and(|size| size != expected_length)
            {
                return Err(format!(
                    "resumed response size differs from {expected_length} bytes"
                ));
            }
            let range = response
                .headers()
                .get(CONTENT_RANGE)
                .and_then(|value| value.to_str().ok())
                .ok_or_else(|| "resumed response omitted Content-Range".to_owned())?;
            if !valid_content_range(range, offset, exact_bytes) {
                return Err(format!("unexpected Content-Range {range:?}"));
            }
        } else if offset > 0 && response.status().is_success() {
            // This source does not support byte ranges. Its full response is
            // still usable, but it must replace rather than append to .part.
            offset = 0;
        }
        let response = response
            .error_for_status()
            .map_err(|error| error.to_string())?;
        if response
            .content_length()
            .is_some_and(|size| size != exact_bytes - offset)
        {
            return Err(format!(
                "response size differs from {} bytes",
                exact_bytes - offset
            ));
        }
        Box::new(response)
    };
    let mut reader = reader;
    let mut hash = Sha256::new();
    if offset > 0 {
        let partial = File::open(destination).map_err(|error| error.to_string())?;
        let mut prefix = partial.take(offset);
        std::io::copy(&mut prefix, &mut HashWriter(&mut hash))
            .map_err(|error| error.to_string())?;
    }
    let mut output = if offset == 0 {
        File::create(destination).map_err(|error| error.to_string())?
    } else {
        OpenOptions::new()
            .append(true)
            .open(destination)
            .map_err(|error| error.to_string())?
    };
    let mut total = offset;
    let mut reported_percent = percent(offset, exact_bytes);
    progress(PackProgress::Downloading(reported_percent));
    // Setup runs on a Tokio blocking worker whose stack is only a few MiB on
    // desktop platforms. Keep the large transfer buffer on the heap.
    let mut buffer = vec![0u8; PACK_IO_BUFFER_BYTES];
    loop {
        if cancel() {
            // The .part file and its byte count survive, so the next attempt
            // resumes here rather than starting the pack over.
            return Err("stopped on request".to_owned());
        }
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
        let percent = percent(total, exact_bytes);
        if percent != reported_percent {
            reported_percent = percent;
            progress(PackProgress::Downloading(percent));
        }
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

fn local_source_reader(path: &Path, offset: u64) -> std::result::Result<Box<dyn Read>, String> {
    let mut file = File::open(path).map_err(|error| error.to_string())?;
    if file.metadata().map_err(|error| error.to_string())?.len() < offset {
        return Err(format!(
            "source is shorter than the existing {offset}-byte partial download"
        ));
    }
    file.seek(SeekFrom::Start(offset))
        .map_err(|error| error.to_string())?;
    Ok(Box::new(file))
}

fn valid_content_range(value: &str, offset: u64, total: u64) -> bool {
    let Some((range, declared_total)) = value
        .strip_prefix("bytes ")
        .and_then(|value| value.split_once('/'))
    else {
        return false;
    };
    let Some((start, end)) = range.split_once('-') else {
        return false;
    };
    start.parse::<u64>().ok() == Some(offset)
        && end.parse::<u64>().ok() == total.checked_sub(1)
        && declared_total.parse::<u64>().ok() == Some(total)
}

fn fetch_from_sources(label: &str, sources: &[String], max_bytes: u64) -> Result<Vec<u8>> {
    let client = download_client(label, METADATA_STALL_TIMEOUT)?;
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

fn download_client(label: &str, timeout: Duration) -> Result<Client> {
    Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(timeout)
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

fn extract_zip(
    archive: &Path,
    destination: &Path,
    expected_size: u64,
    progress: impl Fn(u8),
) -> Result<()> {
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
    let mut extracted_size = 0u64;
    let mut reported_percent = 0;
    // Keep enough stack available for zip's central-directory parser. Official
    // toolchain archives contain thousands of entries.
    let mut buffer = vec![0u8; PACK_IO_BUFFER_BYTES];
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
        loop {
            let read = entry
                .read(&mut buffer)
                .map_err(|err| SetupError::io(&path, err))?;
            if read == 0 {
                break;
            }
            output
                .write_all(&buffer[..read])
                .map_err(|err| SetupError::io(&path, err))?;
            extracted_size = extracted_size.checked_add(read as u64).ok_or_else(|| {
                SetupError::InvalidManifest("archive expanded size overflows u64".into())
            })?;
            let percent = percent(extracted_size, expected_size);
            if percent != reported_percent {
                reported_percent = percent;
                progress(percent);
            }
        }
        #[cfg(unix)]
        if let Some(mode) = entry.unix_mode() {
            use std::os::unix::fs::PermissionsExt as _;
            fs::set_permissions(&path, fs::Permissions::from_mode(mode & 0o777))
                .map_err(|err| SetupError::io(&path, err))?;
        }
    }
    Ok(())
}

fn percent(current: u64, total: u64) -> u8 {
    if total == 0 {
        return 0;
    }
    ((u128::from(current) * 100 / u128::from(total)).min(100)) as u8
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
    use std::cell::{Cell, RefCell};
    use std::net::TcpListener;
    use std::thread;

    #[test]
    fn rejects_manifests_without_an_explicit_pack_version() {
        let legacy = serde_json::json!({
            "schema": 1,
            "channel": "stable",
            "distribution_version": "2.0.7",
            "browser_version": "2.0.7",
            "skills_pack_version": "2.0.7",
            "artifacts": [],
        });

        assert!(serde_json::from_value::<DistributionManifest>(legacy).is_err());
    }

    #[test]
    fn accepts_an_independent_pack_version_and_rejects_artifact_drift() {
        let mut manifest = DistributionManifest {
            schema: 1,
            channel: "stable".into(),
            distribution_version: "2.0.7".into(),
            browser_version: "2.0.7".into(),
            skills_pack_version: "2.0.7".into(),
            pack_version: "2.0.6".into(),
            artifacts: vec![PackArtifact {
                id: "toolchain-base".into(),
                version: "2.0.6".into(),
                platform: "darwin".into(),
                architecture: "arm64".into(),
                sha256: "0".repeat(64),
                size: 1,
                installed_size: 1,
                urls: vec!["https://example.invalid/pack.zip".into()],
                commands: Default::default(),
                python_profiles: Default::default(),
                environment: Default::default(),
                capabilities: vec![],
                probes: vec![],
                manual_actions: vec![],
            }],
        };

        validate_manifest(&manifest).unwrap();
        manifest.artifacts[0].version = "2.0.5".into();
        assert!(matches!(
            validate_manifest(&manifest),
            Err(SetupError::InvalidManifest(_))
        ));
    }

    #[test]
    fn extracts_large_central_directory_on_worker_sized_stack() {
        const ENTRY_COUNT: usize = 12_500;
        const CONSTRAINED_STACK_BYTES: usize = 1024 * 1024;

        let temporary = tempfile::tempdir().unwrap();
        let archive = temporary.path().join("many-entries.zip");
        let destination = temporary.path().join("extracted");
        let file = File::create(&archive).unwrap();
        let mut writer = zip::ZipWriter::new(file);
        let options = zip::write::SimpleFileOptions::default();
        for index in 0..ENTRY_COUNT {
            writer
                .start_file(format!("payload-{index:05}"), options)
                .unwrap();
        }
        writer.finish().unwrap();

        let worker = std::thread::Builder::new()
            .stack_size(CONSTRAINED_STACK_BYTES)
            .spawn(move || extract_zip(&archive, &destination, 0, |_| {}))
            .unwrap();

        worker.join().unwrap().unwrap();
    }

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

    #[test]
    fn stops_a_download_between_chunks_and_keeps_the_partial() {
        let temporary = tempfile::tempdir().unwrap();
        let source = temporary.path().join("source.zip");
        let destination = temporary.path().join("download.part");
        let payload = vec![7u8; PACK_IO_BUFFER_BYTES * 3];
        fs::write(&source, &payload).unwrap();
        let digest = format!("{:x}", Sha256::digest(&payload));
        // Stop only once bytes are already on disk, which is the case that
        // matters: stopping must not throw away what has been downloaded.
        let polls = Cell::new(0u32);
        let cancel = || {
            polls.set(polls.get() + 1);
            polls.get() > 2
        };

        let error = download_from_sources(
            "test-pack",
            &[source.to_string_lossy().into_owned()],
            &destination,
            payload.len() as u64,
            &digest,
            &cancel,
            &|_| {},
        )
        .unwrap_err();

        assert!(matches!(error, SetupError::Cancelled));
        let partial = fs::metadata(&destination).unwrap().len();
        assert!(partial > 0 && partial < payload.len() as u64);
    }

    #[test]
    fn reports_the_switch_to_a_backup_download_source() {
        let temporary = tempfile::tempdir().unwrap();
        let source = temporary.path().join("source.zip");
        let destination = temporary.path().join("download.part");
        let payload = b"payload served by the backup source";
        fs::write(&source, payload).unwrap();
        let digest = format!("{:x}", Sha256::digest(payload));
        let reported = RefCell::new(Vec::new());

        download_from_sources(
            "test-pack",
            &[
                // Refused immediately, standing in for an unreachable CDN.
                "http://127.0.0.1:1/pack.zip".to_owned(),
                source.to_string_lossy().into_owned(),
            ],
            &destination,
            payload.len() as u64,
            &digest,
            &never_cancelled(),
            &|event| reported.borrow_mut().push(event),
        )
        .unwrap();

        assert!(
            reported
                .borrow()
                .contains(&PackProgress::SwitchingSource { remaining: 1 })
        );
        assert_eq!(fs::read(destination).unwrap(), payload);
    }

    #[test]
    fn resumes_a_partial_pack_from_a_local_source() {
        let temporary = tempfile::tempdir().unwrap();
        let source = temporary.path().join("source.zip");
        let destination = temporary.path().join("download.part");
        let payload = b"verified pack payload used for resume";
        fs::write(&source, payload).unwrap();
        fs::write(&destination, &payload[..13]).unwrap();
        let digest = format!("{:x}", Sha256::digest(payload));
        let reported = RefCell::new(Vec::new());

        download_from_sources(
            "test-pack",
            &[source.to_string_lossy().into_owned()],
            &destination,
            payload.len() as u64,
            &digest,
            &never_cancelled(),
            &|event| {
                if let PackProgress::Downloading(percent) = event {
                    reported.borrow_mut().push(percent);
                }
            },
        )
        .unwrap();

        assert_eq!(fs::read(destination).unwrap(), payload);
        assert!(
            reported
                .borrow()
                .first()
                .is_some_and(|percent| *percent > 0)
        );
        assert_eq!(reported.borrow().last(), Some(&100));
    }

    #[test]
    fn discards_a_complete_corrupt_partial_and_retries_once() {
        let temporary = tempfile::tempdir().unwrap();
        let source = temporary.path().join("source.zip");
        let destination = temporary.path().join("download.part");
        let payload = b"0123456789abcdef";
        fs::write(&source, payload).unwrap();
        fs::write(&destination, b"xxxx").unwrap();
        let digest = format!("{:x}", Sha256::digest(payload));

        download_from_sources(
            "test-pack",
            &[source.to_string_lossy().into_owned()],
            &destination,
            payload.len() as u64,
            &digest,
            &never_cancelled(),
            &|_| {},
        )
        .unwrap();

        assert_eq!(fs::read(destination).unwrap(), payload);
    }

    #[test]
    fn validates_exact_http_content_ranges() {
        assert!(valid_content_range("bytes 1024-4095/4096", 1024, 4096));
        assert!(!valid_content_range("bytes 0-4095/4096", 1024, 4096));
        assert!(!valid_content_range("bytes 1024-2047/4096", 1024, 4096));
        assert!(!valid_content_range("items 1024-4095/4096", 1024, 4096));
    }

    #[test]
    fn resumes_an_http_pack_with_a_range_request() {
        let temporary = tempfile::tempdir().unwrap();
        let destination = temporary.path().join("download.part");
        let payload = b"http range resume payload".to_vec();
        let offset = 7usize;
        fs::write(&destination, &payload[..offset]).unwrap();
        let digest = format!("{:x}", Sha256::digest(&payload));

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server_payload = payload.clone();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 4096];
            let size = stream.read(&mut request).unwrap();
            let request = String::from_utf8_lossy(&request[..size]);
            assert!(
                request
                    .lines()
                    .any(|line| line.eq_ignore_ascii_case(&format!("range: bytes={offset}-")))
            );
            let body = &server_payload[offset..];
            let response = format!(
                "HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\nContent-Range: bytes {offset}-{}/{}\r\nConnection: close\r\n\r\n",
                body.len(),
                server_payload.len() - 1,
                server_payload.len(),
            );
            stream.write_all(response.as_bytes()).unwrap();
            stream.write_all(body).unwrap();
        });

        download_from_sources(
            "test-pack",
            &[format!("http://{address}/pack.zip")],
            &destination,
            payload.len() as u64,
            &digest,
            &never_cancelled(),
            &|_| {},
        )
        .unwrap();
        server.join().unwrap();

        assert_eq!(fs::read(destination).unwrap(), payload);
    }
}
