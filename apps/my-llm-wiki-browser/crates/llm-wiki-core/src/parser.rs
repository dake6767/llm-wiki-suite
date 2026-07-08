use std::collections::BTreeMap;
use std::path::Path;
use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value as JsonValue;

use crate::Result;

pub const IMAGE_EXTS: &[&str] = &["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif"];

#[derive(Debug, Clone, PartialEq)]
pub struct ParsedMarkdown {
    pub frontmatter: BTreeMap<String, JsonValue>,
    pub body: String,
}

fn wikilink_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(!)?\[\[([^\[\]|]+?)(?:\|([^\[\]]+?))?\]\]").expect("valid wikilink regex")
    })
}

pub fn parse_file(path: &Path) -> Result<ParsedMarkdown> {
    parse_markdown(&std::fs::read_to_string(path)?)
}

pub fn parse_markdown(text: &str) -> Result<ParsedMarkdown> {
    if !text.starts_with("---\n") {
        return Ok(ParsedMarkdown {
            frontmatter: BTreeMap::new(),
            body: text.to_string(),
        });
    }

    let rest = &text[4..];
    let Some(end) = rest.find("\n---") else {
        return Ok(ParsedMarkdown {
            frontmatter: BTreeMap::new(),
            body: text.to_string(),
        });
    };

    let yaml = &rest[..end];
    let after_marker = &rest[end + "\n---".len()..];
    let body = after_marker
        .strip_prefix("\r\n")
        .or_else(|| after_marker.strip_prefix('\n'))
        .unwrap_or(after_marker)
        .to_string();

    Ok(ParsedMarkdown {
        frontmatter: parse_frontmatter(yaml)?,
        body,
    })
}

pub fn parse_frontmatter(yaml: &str) -> Result<BTreeMap<String, JsonValue>> {
    if yaml.trim().is_empty() {
        return Ok(BTreeMap::new());
    }
    let parsed: serde_yaml::Value = serde_yaml::from_str(yaml)?;
    let json = serde_json::to_value(parsed)?;
    let JsonValue::Object(map) = json else {
        return Ok(BTreeMap::new());
    };
    Ok(map.into_iter().collect())
}

pub fn extract_wikilink_targets(body: &str) -> Vec<String> {
    wikilink_re()
        .captures_iter(body)
        .filter_map(|caps| {
            let target = caps.get(2)?.as_str();
            if caps.get(1).is_some() || is_image_target(target) {
                None
            } else {
                Some(normalize_target(target))
            }
        })
        .filter(|target| !target.is_empty())
        .collect()
}

pub fn is_image_target(target: &str) -> bool {
    Path::new(target)
        .extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| {
            IMAGE_EXTS
                .iter()
                .any(|known| ext.eq_ignore_ascii_case(known))
        })
}

pub fn normalize_target(target: &str) -> String {
    let target = target.trim();
    target
        .strip_suffix(".md")
        .or_else(|| target.strip_suffix(".MD"))
        .unwrap_or(target)
        .to_string()
}

pub fn normalize_page_path(path: &str) -> String {
    let mut path = path.trim().trim_matches('/').to_string();
    if let Some(stripped) = path.strip_suffix(".md") {
        path = stripped.to_string();
    }
    if let Some(stripped) = path.strip_prefix("page/") {
        path = stripped.to_string();
    }
    path
}

pub fn rewrite_body(
    body: &str,
    wiki_key: &str,
    mut resolver: impl FnMut(&str) -> Option<String>,
) -> String {
    let rewritten = wikilink_re().replace_all(body, |caps: &regex::Captures| {
        let raw_target = caps.get(2).map(|m| m.as_str().trim()).unwrap_or_default();
        let label = caps
            .get(3)
            .map(|m| m.as_str().trim())
            .filter(|label| !label.is_empty())
            .unwrap_or(raw_target);

        if is_image_target(raw_target) {
            return format!("![{label}]({})", asset_url(wiki_key, raw_target));
        }

        let target = normalize_target(raw_target);
        if let Some(hit) = resolver(&target) {
            return format!("[{label}](/w/{wiki_key}/page/{hit})");
        }
        format!(
            "[{label}](/w/{wiki_key}/search?q={} \"未找到页面，点击搜索\")",
            urlencoding::encode(label)
        )
    });

    asset_url_re()
        .replace_all(&rewritten, |caps: &regex::Captures| {
            let reference = caps.get(1).map(|m| m.as_str()).unwrap_or_default();
            format!("]({})", asset_url(wiki_key, reference))
        })
        .to_string()
}

fn asset_url_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r#"\]\(\s*([^\)\s]*assets/[^\)\s]+?)\s*(?:"[^"]*")?\)"#)
            .expect("valid asset url regex")
    })
}

fn asset_url(wiki_key: &str, reference: &str) -> String {
    let name = Path::new(reference)
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or(reference);
    format!(
        "/api/v1/wikis/{wiki_key}/assets/{}",
        urlencoding::encode(name)
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extract_targets_ignores_images() {
        let body = "见 [[entities/claude|Claude]] 与 [[mcp]] ![[pic.png]]";
        assert_eq!(extract_wikilink_targets(body), ["entities/claude", "mcp"]);
    }

    #[test]
    fn parse_yaml_frontmatter() {
        let parsed = parse_markdown("---\ntitle: Claude\ntags: [ai, agent]\n---\nbody").unwrap();
        assert_eq!(parsed.body, "body");
        assert_eq!(parsed.frontmatter["title"], "Claude");
    }

    #[test]
    fn normalize_accepts_web_and_markdown_paths() {
        assert_eq!(
            normalize_page_path("/page/entities/claude.md"),
            "entities/claude"
        );
    }

    #[test]
    fn rewrite_links_and_assets() {
        let body = "[[entities/claude|Claude]] ![[pic.png]] ![x](../../assets/foo.webp)";
        let out = rewrite_body(body, "demo", |target| {
            (target == "entities/claude").then(|| target.to_string())
        });
        assert!(out.contains("[Claude](/w/demo/page/entities/claude)"));
        assert!(out.contains("/api/v1/wikis/demo/assets/pic.png"));
        assert!(out.contains("/api/v1/wikis/demo/assets/foo.webp"));
    }
}
