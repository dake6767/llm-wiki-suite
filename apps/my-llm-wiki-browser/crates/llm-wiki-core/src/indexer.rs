use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use regex::Regex;

use crate::parser::{ParsedMarkdown, extract_wikilink_targets, parse_file};
use crate::{PageRecord, Result, SearchDoc, WikiEntry};

#[derive(Debug, Clone, PartialEq)]
pub struct WikiIndex {
    pub entry: WikiEntry,
    pub pages: BTreeMap<String, PageRecord>,
    pub by_slug: BTreeMap<String, String>,
    pub backlinks: BTreeMap<String, Vec<String>>,
}

impl WikiIndex {
    pub fn resolve(&self, target: &str) -> Option<&str> {
        let target = target.trim().trim_matches('/');
        if let Some((path, _)) = self.pages.get_key_value(target) {
            return Some(path.as_str());
        }
        let base = target.rsplit('/').next().unwrap_or(target);
        self.by_slug.get(base).map(String::as_str)
    }
}

#[derive(Debug, Clone, Default)]
pub struct IndexManager {
    pub wikis: BTreeMap<String, WikiIndex>,
}

impl IndexManager {
    pub fn build(entries: impl IntoIterator<Item = WikiEntry>) -> Result<Self> {
        let mut manager = Self::default();
        for entry in entries {
            manager.rebuild_wiki(entry)?;
        }
        Ok(manager)
    }

    pub fn rebuild_wiki(&mut self, entry: WikiEntry) -> Result<()> {
        let index = build_wiki_index(entry)?;
        self.wikis.insert(index.entry.key.clone(), index);
        Ok(())
    }

    pub fn get(&self, key: &str) -> Option<&WikiIndex> {
        self.wikis.get(key)
    }
}

pub fn build_wiki_index(entry: WikiEntry) -> Result<WikiIndex> {
    let mut index = WikiIndex {
        entry,
        pages: BTreeMap::new(),
        by_slug: BTreeMap::new(),
        backlinks: BTreeMap::new(),
    };

    for md in markdown_files(&index.entry.wiki_dir)? {
        let rel = md
            .strip_prefix(&index.entry.wiki_dir)
            .unwrap_or(&md)
            .to_string_lossy()
            .replace('\\', "/");
        let path = rel.strip_suffix(".md").unwrap_or(&rel).to_string();
        let parsed = parse_file(&md).or_else(|_| {
            let body = fs::read_to_string(&md)?;
            Ok::<ParsedMarkdown, crate::Error>(ParsedMarkdown {
                frontmatter: BTreeMap::new(),
                body,
            })
        })?;

        let slug = Path::new(&path)
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or(&path)
            .to_string();
        let fallback_type = path
            .split_once('/')
            .map(|(head, _)| head)
            .unwrap_or("page")
            .to_string();
        let title = json_string(&parsed.frontmatter, "title").unwrap_or_else(|| slug.clone());
        let page_type = json_string(&parsed.frontmatter, "type").unwrap_or(fallback_type);
        let tags = json_string_list(&parsed.frontmatter, "tags");
        let sources = json_string_list(&parsed.frontmatter, "sources");
        let mtime = md
            .metadata()
            .ok()
            .and_then(|meta| meta.modified().ok())
            .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|duration| duration.as_secs_f64());

        let record = PageRecord {
            path: path.clone(),
            slug: slug.clone(),
            title,
            page_type,
            tags,
            updated: json_string(&parsed.frontmatter, "updated"),
            created: json_string(&parsed.frontmatter, "created"),
            frontmatter: parsed.frontmatter,
            targets: extract_wikilink_targets(&parsed.body),
            body: parsed.body,
            sources,
            mtime,
        };
        index.by_slug.entry(slug).or_insert_with(|| path.clone());
        index.pages.insert(path, record);
    }

    let mut backlinks: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for (path, record) in &index.pages {
        for target in &record.targets {
            if let Some(hit) = index.resolve(target)
                && hit != path
            {
                backlinks
                    .entry(hit.to_string())
                    .or_default()
                    .insert(path.clone());
            }
        }
    }
    index.backlinks = backlinks
        .into_iter()
        .map(|(key, values)| (key, values.into_iter().collect()))
        .collect();

    Ok(index)
}

impl WikiIndex {
    pub fn search_docs(&self) -> Vec<SearchDoc> {
        self.pages
            .values()
            .map(|record| SearchDoc {
                wiki: self.entry.key.clone(),
                path: record.path.clone(),
                slug: record.slug.clone(),
                title: record.title.clone(),
                page_type: record.page_type.clone(),
                tags: record.tags.clone(),
                text: to_plain_text(&record.body),
            })
            .collect()
    }
}

pub fn to_plain_text(body: &str) -> String {
    let mut text = body.to_string();
    for (pattern, replacement) in [
        (r"!?\[\[([^\[\]|]+?)(?:\|([^\[\]]+?))?\]\]", "$2$1"),
        (r"!\[[^\]]*\]\([^)]*\)", ""),
        (r"\[([^\]]+)\]\([^)]*\)", "$1"),
        (r"`{1,3}", ""),
        (r"(?m)^[#>\-\*\+\s]{0,6}", ""),
        (r"[*_~]", ""),
    ] {
        let re = Regex::new(pattern).expect("valid plain-text regex");
        text = re.replace_all(&text, replacement).to_string();
    }
    Regex::new(r"\n{2,}")
        .expect("valid newline regex")
        .replace_all(text.trim(), "\n")
        .to_string()
}

fn markdown_files(root: &Path) -> Result<Vec<PathBuf>> {
    let mut out = Vec::new();
    collect_markdown_files(root, &mut out)?;
    out.sort();
    Ok(out)
}

fn collect_markdown_files(dir: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        if path.is_dir() {
            collect_markdown_files(&path, out)?;
        } else if path
            .extension()
            .and_then(|ext| ext.to_str())
            .is_some_and(|ext| ext.eq_ignore_ascii_case("md"))
        {
            out.push(path);
        }
    }
    Ok(())
}

fn json_string(map: &BTreeMap<String, serde_json::Value>, key: &str) -> Option<String> {
    match map.get(key)? {
        serde_json::Value::String(value) => Some(value.clone()),
        serde_json::Value::Null => None,
        value => Some(value.to_string()),
    }
}

fn json_string_list(map: &BTreeMap<String, serde_json::Value>, key: &str) -> Vec<String> {
    match map.get(key) {
        Some(serde_json::Value::Array(values)) => values
            .iter()
            .filter_map(|value| match value {
                serde_json::Value::String(value) => Some(value.clone()),
                serde_json::Value::Null => None,
                value => Some(value.to_string()),
            })
            .collect(),
        Some(serde_json::Value::String(value)) => vec![value.clone()],
        Some(serde_json::Value::Null) | None => Vec::new(),
        Some(value) => vec![value.to_string()],
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_pages_and_backlinks() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("demo");
        let wiki_dir = root.join("wiki");
        fs::create_dir_all(wiki_dir.join("entities")).unwrap();
        fs::write(
            wiki_dir.join("entities").join("claude.md"),
            "---\ntitle: Claude\ntags: [ai]\n---\nhello",
        )
        .unwrap();
        fs::write(wiki_dir.join("mcp.md"), "see [[entities/claude|Claude]]").unwrap();

        let entry = WikiEntry {
            key: "demo".to_string(),
            name: "demo".to_string(),
            description: None,
            root_dir: root.clone(),
            wiki_dir,
            raw_dir: root.join("raw"),
            assets_dir: root.join("raw").join("assets"),
            default: false,
        };
        let index = build_wiki_index(entry).unwrap();
        assert_eq!(index.resolve("claude"), Some("entities/claude"));
        assert_eq!(index.backlinks["entities/claude"], ["mcp"]);
    }
}
