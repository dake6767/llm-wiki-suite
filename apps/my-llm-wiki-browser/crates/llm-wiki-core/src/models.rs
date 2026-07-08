use std::collections::BTreeMap;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct WikiEntry {
    pub key: String,
    pub name: String,
    pub description: Option<String>,
    pub root_dir: PathBuf,
    pub wiki_dir: PathBuf,
    pub raw_dir: PathBuf,
    pub assets_dir: PathBuf,
    pub default: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct WikiSummary {
    pub key: String,
    pub name: String,
    pub description: Option<String>,
    pub default: bool,
    pub page_count: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct PageRecord {
    pub path: String,
    pub slug: String,
    pub title: String,
    pub page_type: String,
    pub tags: Vec<String>,
    pub updated: Option<String>,
    pub created: Option<String>,
    pub frontmatter: BTreeMap<String, serde_json::Value>,
    pub body: String,
    pub targets: Vec<String>,
    pub sources: Vec<String>,
    pub mtime: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SearchResult {
    pub wiki: String,
    pub path: String,
    pub slug: String,
    pub title: String,
    pub page_type: String,
    pub snippet: String,
    pub score: f64,
}
