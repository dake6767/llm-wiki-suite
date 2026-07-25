//! The Skills Pack release channel.
//!
//! Skills move on their own cadence: a fix to a capture SOP should not require
//! rebuilding, signing and reinstalling the Browser. The Browser ships a
//! baseline copy so a first install works offline, then upgrades from here.
//!
//! Everything this module reads off the network is untrusted input, and it ends
//! up steering an install into directories that agents execute from. Two rules
//! carry that weight:
//!
//! 1. **Download URLs are derived here, never read from the payload.** A
//!    tampered signal can lie about a version or a digest, but it cannot point
//!    the download at a host of its choosing.
//! 2. **Every field is re-derived rather than passed through.** Versions are
//!    parsed as semver and re-serialized, digests are checked for shape, and the
//!    release notes are stripped to plain text — so text that survives into the
//!    UI is text this module chose to emit.

use semver::Version;
use serde::{Deserialize, Serialize};

use crate::error::{Result, SetupError};
use crate::pack::{self, ArchiveSpec};

/// The published payload is a handful of small fields; anything larger is a
/// sign we are not talking to the endpoint we think we are.
const VERSION_MAX_BYTES: u64 = 16 * 1024;
/// Schema 1 only carried a version to display. Schema 2 adds the payload
/// digest, because the Browser now installs the pack instead of describing it.
const VERSION_SCHEMA: u32 = 2;
const NOTES_MAX_CHARS: usize = 2000;

pub(crate) fn version_sources() -> Vec<String> {
    vec![
        "https://wiki.htmlgo.to/_skills/version.json".into(),
        "https://github.com/dake6767/llm-wiki-suite/releases/download/skills-latest/skills-version.json"
            .into(),
    ]
}

/// Templates for where an archive can be fetched, in preference order.
///
/// These are configuration, expanded with nothing but a version this module
/// already parsed — see rule 1. The published payload never contributes a URL.
pub(crate) fn archive_sources() -> Vec<String> {
    vec![
        "https://wiki.htmlgo.to/_skills/dl/{version}".into(),
        "https://github.com/dake6767/llm-wiki-suite/releases/download/skills-v{version}/skills-{version}.zip"
            .into(),
    ]
}

fn expand(templates: &[String], version: &str) -> Vec<String> {
    templates
        .iter()
        .map(|template| template.replace("{version}", version))
        .collect()
}

/// The payload exactly as published. Never used outside validation.
#[derive(Deserialize)]
struct PublishedVersion {
    schema: u32,
    pack_version: String,
    sha256: String,
    size: u64,
    installed_size: u64,
    #[serde(default)]
    min_app_version: Option<String>,
    #[serde(default)]
    source_commit: Option<String>,
    #[serde(default)]
    pack_notes: Option<String>,
}

/// A validated Skills Pack release.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SkillsRelease {
    pub pack_version: String,
    pub sha256: String,
    pub size: u64,
    pub installed_size: u64,
    /// The lowest Browser version able to install this pack. `None` means no
    /// floor beyond the schema itself.
    pub min_app_version: Option<String>,
    pub source_commit: Option<String>,
    /// Display-only text. Plain, bounded, and never interpolated into a command
    /// or a prompt.
    pub notes: Option<String>,
    pub urls: Vec<String>,
}

impl SkillsRelease {
    pub(crate) fn archive_spec(&self) -> ArchiveSpec<'_> {
        ArchiveSpec {
            label: "skills",
            urls: &self.urls,
            sha256: &self.sha256,
            size: self.size,
            installed_size: self.installed_size,
        }
    }
}

/// Fetch the current release signal, trying each source in turn.
///
/// A source that answers with something invalid is treated exactly like one
/// that did not answer: we move on to the next. Only when no source produces a
/// valid signal does this fail, so a bad edge cache cannot strand the Browser
/// on a stale pack.
pub(crate) fn fetch_release(sources: &[String], archives: &[String]) -> Result<SkillsRelease> {
    let mut last = None;
    for source in sources {
        let attempt = pack::fetch_from_sources(
            "skills version",
            std::slice::from_ref(source),
            VERSION_MAX_BYTES,
        )
        .and_then(|bytes| parse_release(&bytes, archives));
        match attempt {
            Ok(release) => return Ok(release),
            Err(error) => last = Some(error),
        }
    }
    Err(last.unwrap_or_else(|| {
        SetupError::InvalidManifest("no skills version source configured".into())
    }))
}

pub(crate) fn parse_release(bytes: &[u8], archives: &[String]) -> Result<SkillsRelease> {
    let published: PublishedVersion = serde_json::from_slice(bytes)
        .map_err(|err| SetupError::InvalidManifest(format!("skills version: {err}")))?;
    if published.schema != VERSION_SCHEMA {
        return Err(SetupError::InvalidManifest(format!(
            "unsupported skills version schema {}",
            published.schema
        )));
    }
    let pack_version = parse_version("pack_version", &published.pack_version)?;
    let min_app_version = published
        .min_app_version
        .as_deref()
        .map(|value| parse_version("min_app_version", value))
        .transpose()?;
    if published.sha256.len() != 64
        || !published
            .sha256
            .bytes()
            .all(|value| value.is_ascii_hexdigit())
    {
        return Err(SetupError::InvalidManifest(
            "skills version: sha256 must be 64 hex characters".into(),
        ));
    }
    if published.size == 0 || published.installed_size == 0 {
        return Err(SetupError::InvalidManifest(
            "skills version: archive sizes must be non-zero".into(),
        ));
    }
    Ok(SkillsRelease {
        urls: expand(archives, &pack_version),
        sha256: published.sha256.to_ascii_lowercase(),
        size: published.size,
        installed_size: published.installed_size,
        // A malformed commit is dropped rather than rejected: it is provenance
        // the UI may show, not something the install depends on.
        source_commit: published.source_commit.filter(|value| {
            value.len() == 40 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
        }),
        notes: published.pack_notes.as_deref().and_then(plain_text),
        min_app_version,
        pack_version,
    })
}

/// Parse a version and hand back our own rendering of it. Only something that
/// survives a semver round trip reaches any caller, so no text smuggled into a
/// version field can reach a UI string or a URL.
fn parse_version(label: &str, value: &str) -> Result<String> {
    Version::parse(value)
        .map(|version| version.to_string())
        .map_err(|err| SetupError::InvalidManifest(format!("skills version: {label}: {err}")))
}

/// Reduce published notes to bounded plain text. Control characters go first —
/// this string reaches a terminal as readily as a web view.
fn plain_text(value: &str) -> Option<String> {
    let cleaned: String = value
        .chars()
        .map(|character| {
            if character == '\n' || !character.is_control() {
                character
            } else {
                ' '
            }
        })
        .take(NOTES_MAX_CHARS)
        .collect();
    let trimmed = cleaned.trim();
    (!trimmed.is_empty()).then(|| trimmed.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse_release_for_test(bytes: &[u8]) -> Result<SkillsRelease> {
        parse_release(bytes, &archive_sources())
    }

    fn payload(overrides: serde_json::Value) -> Vec<u8> {
        let mut value = serde_json::json!({
            "schema": 2,
            "pack_version": "2.1.0",
            "sha256": "a".repeat(64),
            "size": 301937u64,
            "installed_size": 755702u64,
        });
        let object = value.as_object_mut().expect("object");
        for (key, entry) in overrides.as_object().expect("object") {
            if entry.is_null() {
                object.remove(key);
            } else {
                object.insert(key.clone(), entry.clone());
            }
        }
        serde_json::to_vec(&value).expect("serialize")
    }

    #[test]
    fn derives_download_urls_instead_of_trusting_the_payload() {
        let release = parse_release_for_test(&payload(serde_json::json!({
            "urls": ["https://attacker.invalid/evil.zip"],
        })))
        .expect("valid");
        assert_eq!(
            release.urls,
            vec![
                "https://wiki.htmlgo.to/_skills/dl/2.1.0".to_owned(),
                "https://github.com/dake6767/llm-wiki-suite/releases/download/skills-v2.1.0/skills-2.1.0.zip"
                    .to_owned(),
            ]
        );
    }

    #[test]
    fn rejects_another_schema() {
        assert!(parse_release_for_test(&payload(serde_json::json!({"schema": 1}))).is_err());
        assert!(parse_release_for_test(&payload(serde_json::json!({"schema": 3}))).is_err());
    }

    #[test]
    fn rejects_a_version_carrying_extra_text() {
        assert!(
            parse_release_for_test(&payload(
                serde_json::json!({"pack_version": "2.1.0; rm -rf /"})
            ))
            .is_err()
        );
    }

    #[test]
    fn rejects_a_malformed_digest_or_size() {
        assert!(parse_release_for_test(&payload(serde_json::json!({"sha256": "abc"}))).is_err());
        assert!(
            parse_release_for_test(&payload(serde_json::json!({"sha256": "z".repeat(64)}))).is_err()
        );
        assert!(parse_release_for_test(&payload(serde_json::json!({"size": 0}))).is_err());
        assert!(parse_release_for_test(&payload(serde_json::json!({"installed_size": 0}))).is_err());
    }

    #[test]
    fn drops_a_malformed_commit_but_keeps_the_release() {
        let release =
            parse_release_for_test(&payload(serde_json::json!({"source_commit": "nope"}))).expect("valid");
        assert!(release.source_commit.is_none());
        let release = parse_release_for_test(&payload(serde_json::json!({"source_commit": "b".repeat(40)})))
            .expect("valid");
        assert_eq!(release.source_commit.as_deref(), Some("b".repeat(40).as_str()));
    }

    #[test]
    fn strips_control_characters_and_bounds_notes() {
        let release = parse_release_for_test(&payload(serde_json::json!({
            "pack_notes": "  fixed \u{1b}[31mpreflight\u{7}  ",
        })))
        .expect("valid");
        assert_eq!(release.notes.as_deref(), Some("fixed  [31mpreflight"));

        let release = parse_release_for_test(&payload(serde_json::json!({
            "pack_notes": "x".repeat(NOTES_MAX_CHARS + 500),
        })))
        .expect("valid");
        assert_eq!(release.notes.expect("notes").chars().count(), NOTES_MAX_CHARS);
    }

    #[test]
    fn an_oversized_response_never_reaches_the_parser() {
        // The byte cap lives in the fetch path; the parser still refuses the
        // shape it would produce, so both layers say no.
        assert!(parse_release_for_test(b"[]").is_err());
        assert!(parse_release_for_test(&vec![b'x'; 32 * 1024]).is_err());
    }
}
