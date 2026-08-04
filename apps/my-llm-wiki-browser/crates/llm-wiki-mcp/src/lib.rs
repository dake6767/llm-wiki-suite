//! MCP 工具契约：名称、描述、入参 schema 与参数结构。
//!
//! 只定义「对外长什么样」，不含任何执行逻辑——HTTP 端点（llm-wiki-server::mcp）
//! 与未来可能的 stdio 入口共用这一份契约，保证两种接入方式的工具面完全一致。

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub enum ToolName {
    ListWikis,
    SearchWiki,
    RetrieveContext,
    ReadPage,
    ReadPages,
    ReadRaw,
    ListWikiTree,
}

/// read_pages 的预算默认值与上限：默认贴合「top-8 检索 → 读 3–5 页」的
/// 检索纪律，clamp 防止一次调用重新造出整库级大上下文。
pub const READ_PAGES_DEFAULT_MAX_PAGES: usize = 5;
pub const READ_PAGES_MAX_PAGES_CEILING: usize = 20;
pub const READ_PAGES_DEFAULT_CHARS_PER_PAGE: usize = 6_000;
pub const READ_PAGES_CHARS_PER_PAGE_CEILING: usize = 20_000;
pub const READ_PAGES_DEFAULT_TOTAL_CHARS: usize = 24_000;
pub const READ_PAGES_TOTAL_CHARS_CEILING: usize = 100_000;

/// retrieve_context 的图遍历边界。默认一跳足以补齐显式关联页，二跳上限则允许
/// 多跳问题显式请求更深探索，同时阻止高连接页面把候选集合无限放大。
pub const RETRIEVE_CONTEXT_DEFAULT_SEED_LIMIT: usize = 5;
pub const RETRIEVE_CONTEXT_SEED_LIMIT_CEILING: usize = 20;
pub const RETRIEVE_CONTEXT_DEFAULT_MAX_DEPTH: usize = 1;
pub const RETRIEVE_CONTEXT_MAX_DEPTH_CEILING: usize = 2;
pub const RETRIEVE_CONTEXT_DEFAULT_MAX_NODES: usize = 12;
pub const RETRIEVE_CONTEXT_MAX_NODES_CEILING: usize = 50;

impl ToolName {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::ListWikis => "list_wikis",
            Self::SearchWiki => "search_wiki",
            Self::RetrieveContext => "retrieve_context",
            Self::ReadPage => "read_page",
            Self::ReadPages => "read_pages",
            Self::ReadRaw => "read_raw",
            Self::ListWikiTree => "list_wiki_tree",
        }
    }

    pub fn parse(name: &str) -> Option<Self> {
        TOOL_NAMES
            .iter()
            .copied()
            .find(|tool| tool.as_str() == name)
    }

    /// 面向 agent 的工具说明：讲清参数来源与工具间的接力关系
    /// （search 的 path 喂给 read_page；page 的 sources 喂给 read_raw）。
    pub fn description(self) -> &'static str {
        match self {
            Self::ListWikis => {
                "List all wikis in this LLM Wiki library (key, name, description, page count). \
                 Call this first to discover valid `wiki` keys for the other tools."
            }
            Self::SearchWiki => {
                "Full-text search compiled wiki pages. Omit `wiki` to search ALL wikis at \
                 once (each hit then tells you which wiki it came from) — prefer this when \
                 unsure which wiki holds the answer, instead of searching wikis one by one. \
                 Returns hits with wiki, path, title, type, snippet and score. Feed a hit's \
                 `wiki` + `path` to read_page for the full content. Optional `type`/`tag` \
                 filters narrow the candidate set."
            }
            Self::RetrieveContext => {
                "Retrieve a bounded answer context with hybrid full-text + wikilink graph search. \
                 Full-text hits become seeds, then resolved outgoing links and backlinks are \
                 expanded within the same wiki up to `maxDepth` (default 1, max 2). Results \
                 include bodies, relation provenance and scores under strict node and character \
                 budgets. Omit `wiki` to seed from all wikis; graph traversal never crosses wikis."
            }
            Self::ReadPage => {
                "Read one compiled wiki page as markdown: frontmatter metadata, body with \
                 [[wikilinks]], plus resolved outgoing links and backlinks. `path` is a \
                 page path as returned by search_wiki or list_wiki_tree, e.g. 'concepts/mcp'. \
                 To pack several candidate pages into a bounded context, prefer read_pages."
            }
            Self::ReadPages => {
                "Batch-read several wiki pages under a hard context budget: at most \
                 `maxPages` pages, each body truncated to `maxCharsPerPage` characters, \
                 the whole result capped at `maxTotalChars`. Use this after search_wiki \
                 to pack the top candidates without re-creating a wiki-sized context; \
                 over-budget pages are truncated (flagged) or listed in `omitted`."
            }
            Self::ReadRaw => {
                "Read an original captured source under the wiki's raw/ layer (immutable \
                 originals the pages cite in their `sources` field). Pass a page's source \
                 path directly; canonical paths such as 'raw/sources/web/some-article.md' \
                 and legacy paths such as 'web/some-article.md' are both accepted. Use to \
                 re-check a page's claims."
            }
            Self::ListWikiTree => {
                "List every page of one wiki grouped by type (entities/concepts/sources/…), \
                 each with path and title. Good for orientation before targeted reads."
            }
        }
    }

    /// MCP tools/list 用的 inputSchema（JSON Schema 子集）。
    pub fn input_schema(self) -> Value {
        let wiki = json!({"type": "string", "description": "Wiki key, as returned by list_wikis."});
        match self {
            Self::ListWikis => json!({"type": "object", "properties": {}}),
            Self::SearchWiki => json!({
                "type": "object",
                "properties": {
                    "wiki": {"type": "string", "description": "Wiki key to search. Omit to search all wikis at once."},
                    "query": {"type": "string", "description": "Full-text query keywords."},
                    "type": {"type": "string", "description": "Optional page type filter, e.g. 'concepts'."},
                    "tag": {"type": "string", "description": "Optional tag filter."},
                    "limit": {"type": "integer", "description": "Max hits, default 8, max 50."}
                },
                "required": ["query"]
            }),
            Self::RetrieveContext => json!({
                "type": "object",
                "properties": {
                    "wiki": {"type": "string", "description": "Optional wiki key. Omit to seed from all wikis."},
                    "query": {"type": "string", "description": "Full-text query used to select seed pages."},
                    "type": {"type": "string", "description": "Optional page type filter for seed search only."},
                    "tag": {"type": "string", "description": "Optional tag filter for seed search only."},
                    "seedLimit": {"type": "integer", "description": format!("Max full-text seed pages (default {RETRIEVE_CONTEXT_DEFAULT_SEED_LIMIT}, max {RETRIEVE_CONTEXT_SEED_LIMIT_CEILING}).")},
                    "maxDepth": {"type": "integer", "description": format!("Wikilink traversal depth (default {RETRIEVE_CONTEXT_DEFAULT_MAX_DEPTH}, max {RETRIEVE_CONTEXT_MAX_DEPTH_CEILING}; 0 disables expansion).")},
                    "maxNodes": {"type": "integer", "description": format!("Max ranked pages returned (default {RETRIEVE_CONTEXT_DEFAULT_MAX_NODES}, max {RETRIEVE_CONTEXT_MAX_NODES_CEILING}).")},
                    "maxCharsPerPage": {"type": "integer", "description": format!("Per-page body budget in characters (default {READ_PAGES_DEFAULT_CHARS_PER_PAGE}, max {READ_PAGES_CHARS_PER_PAGE_CEILING}).")},
                    "maxTotalChars": {"type": "integer", "description": format!("Total body budget in characters (default {READ_PAGES_DEFAULT_TOTAL_CHARS}, max {READ_PAGES_TOTAL_CHARS_CEILING}).")}
                },
                "required": ["query"]
            }),
            Self::ReadPage => json!({
                "type": "object",
                "properties": {
                    "wiki": wiki,
                    "path": {"type": "string", "description": "Page path, e.g. 'concepts/mcp'."}
                },
                "required": ["wiki", "path"]
            }),
            Self::ReadPages => json!({
                "type": "object",
                "properties": {
                    "wiki": wiki,
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Page paths as returned by search_wiki, e.g. ['concepts/mcp', 'entities/claude']."
                    },
                    "maxPages": {
                        "type": "integer",
                        "description": format!("Max pages to read (default {READ_PAGES_DEFAULT_MAX_PAGES}, max {READ_PAGES_MAX_PAGES_CEILING}); extra paths are reported in `omitted`.")
                    },
                    "maxCharsPerPage": {
                        "type": "integer",
                        "description": format!("Per-page body budget in characters (default {READ_PAGES_DEFAULT_CHARS_PER_PAGE}); longer bodies are truncated and flagged.")
                    },
                    "maxTotalChars": {
                        "type": "integer",
                        "description": format!("Total body budget in characters (default {READ_PAGES_DEFAULT_TOTAL_CHARS}); pages past the budget land in `omitted`.")
                    }
                },
                "required": ["wiki", "paths"]
            }),
            Self::ReadRaw => json!({
                "type": "object",
                "properties": {
                    "wiki": wiki,
                    "path": {"type": "string", "description": "Source path from a compiled page's `sources` field. Accepts canonical 'raw/sources/web/foo.md' or 'sources/web/foo.md' and legacy 'web/foo.md'."}
                },
                "required": ["wiki", "path"]
            }),
            Self::ListWikiTree => json!({
                "type": "object",
                "properties": {"wiki": wiki},
                "required": ["wiki"]
            }),
        }
    }
}

pub const TOOL_NAMES: &[ToolName] = &[
    ToolName::ListWikis,
    ToolName::SearchWiki,
    ToolName::RetrieveContext,
    ToolName::ReadPage,
    ToolName::ReadPages,
    ToolName::ReadRaw,
    ToolName::ListWikiTree,
];

/// tools/list 的完整工具清单。
pub fn tool_list() -> Value {
    let tools = TOOL_NAMES
        .iter()
        .map(|tool| {
            json!({
                "name": tool.as_str(),
                "description": tool.description(),
                "inputSchema": tool.input_schema(),
            })
        })
        .collect::<Vec<_>>();
    json!({ "tools": tools })
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SearchWikiArgs {
    /// 缺省 = 跨全部库检索。
    pub wiki: Option<String>,
    pub query: String,
    pub r#type: Option<String>,
    pub tag: Option<String>,
    pub limit: Option<usize>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct RetrieveContextArgs {
    /// 缺省 = 跨全部库选择种子；后续图遍历仍限定在每个种子所属的库内。
    pub wiki: Option<String>,
    pub query: String,
    pub r#type: Option<String>,
    pub tag: Option<String>,
    pub seed_limit: Option<usize>,
    pub max_depth: Option<usize>,
    pub max_nodes: Option<usize>,
    pub max_chars_per_page: Option<usize>,
    pub max_total_chars: Option<usize>,
}

impl RetrieveContextArgs {
    pub fn budget(&self) -> (usize, usize, usize, usize, usize) {
        let seed_limit = self
            .seed_limit
            .unwrap_or(RETRIEVE_CONTEXT_DEFAULT_SEED_LIMIT)
            .clamp(1, RETRIEVE_CONTEXT_SEED_LIMIT_CEILING);
        let max_depth = self
            .max_depth
            .unwrap_or(RETRIEVE_CONTEXT_DEFAULT_MAX_DEPTH)
            .min(RETRIEVE_CONTEXT_MAX_DEPTH_CEILING);
        let max_nodes = self
            .max_nodes
            .unwrap_or(RETRIEVE_CONTEXT_DEFAULT_MAX_NODES)
            .clamp(1, RETRIEVE_CONTEXT_MAX_NODES_CEILING);
        let per_page = self
            .max_chars_per_page
            .unwrap_or(READ_PAGES_DEFAULT_CHARS_PER_PAGE)
            .clamp(200, READ_PAGES_CHARS_PER_PAGE_CEILING);
        let total = self
            .max_total_chars
            .unwrap_or(READ_PAGES_DEFAULT_TOTAL_CHARS)
            .clamp(per_page.min(1_000), READ_PAGES_TOTAL_CHARS_CEILING);
        (seed_limit, max_depth, max_nodes, per_page, total)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PagePathArgs {
    pub wiki: String,
    pub path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ReadPagesArgs {
    pub wiki: String,
    pub paths: Vec<String>,
    pub max_pages: Option<usize>,
    pub max_chars_per_page: Option<usize>,
    pub max_total_chars: Option<usize>,
}

impl ReadPagesArgs {
    /// clamp 到契约允许的预算区间；缺省取默认值。
    pub fn budget(&self) -> (usize, usize, usize) {
        let max_pages = self
            .max_pages
            .unwrap_or(READ_PAGES_DEFAULT_MAX_PAGES)
            .clamp(1, READ_PAGES_MAX_PAGES_CEILING);
        let per_page = self
            .max_chars_per_page
            .unwrap_or(READ_PAGES_DEFAULT_CHARS_PER_PAGE)
            .clamp(200, READ_PAGES_CHARS_PER_PAGE_CEILING);
        let total = self
            .max_total_chars
            .unwrap_or(READ_PAGES_DEFAULT_TOTAL_CHARS)
            .clamp(per_page.min(1_000), READ_PAGES_TOTAL_CHARS_CEILING);
        (max_pages, per_page, total)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct WikiArgs {
    pub wiki: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tool_names_round_trip() {
        for tool in TOOL_NAMES {
            assert_eq!(ToolName::parse(tool.as_str()), Some(*tool));
        }
        assert_eq!(ToolName::parse("nope"), None);
    }

    #[test]
    fn read_pages_budget_defaults_and_clamps() {
        let args: ReadPagesArgs =
            serde_json::from_value(json!({"wiki": "demo", "paths": ["a", "b"]})).unwrap();
        assert_eq!(
            args.budget(),
            (
                READ_PAGES_DEFAULT_MAX_PAGES,
                READ_PAGES_DEFAULT_CHARS_PER_PAGE,
                READ_PAGES_DEFAULT_TOTAL_CHARS
            )
        );

        let args: ReadPagesArgs = serde_json::from_value(json!({
            "wiki": "demo", "paths": ["a"],
            "maxPages": 999, "maxCharsPerPage": 1, "maxTotalChars": 10_000_000
        }))
        .unwrap();
        assert_eq!(
            args.budget(),
            (
                READ_PAGES_MAX_PAGES_CEILING,
                200,
                READ_PAGES_TOTAL_CHARS_CEILING
            )
        );
    }

    #[test]
    fn retrieve_context_budget_defaults_and_clamps() {
        let args: RetrieveContextArgs =
            serde_json::from_value(json!({"query": "graph retrieval"})).unwrap();
        assert_eq!(
            args.budget(),
            (
                RETRIEVE_CONTEXT_DEFAULT_SEED_LIMIT,
                RETRIEVE_CONTEXT_DEFAULT_MAX_DEPTH,
                RETRIEVE_CONTEXT_DEFAULT_MAX_NODES,
                READ_PAGES_DEFAULT_CHARS_PER_PAGE,
                READ_PAGES_DEFAULT_TOTAL_CHARS,
            )
        );

        let args: RetrieveContextArgs = serde_json::from_value(json!({
            "query": "graph retrieval",
            "seedLimit": 999,
            "maxDepth": 999,
            "maxNodes": 999,
            "maxCharsPerPage": 1,
            "maxTotalChars": 10_000_000
        }))
        .unwrap();
        assert_eq!(
            args.budget(),
            (
                RETRIEVE_CONTEXT_SEED_LIMIT_CEILING,
                RETRIEVE_CONTEXT_MAX_DEPTH_CEILING,
                RETRIEVE_CONTEXT_MAX_NODES_CEILING,
                200,
                READ_PAGES_TOTAL_CHARS_CEILING,
            )
        );
    }

    #[test]
    fn tool_list_exposes_all_tools_with_schemas() {
        let list = tool_list();
        let tools = list["tools"].as_array().unwrap();
        assert_eq!(tools.len(), TOOL_NAMES.len());
        for tool in tools {
            assert!(tool["name"].is_string());
            assert!(!tool["description"].as_str().unwrap().is_empty());
            assert_eq!(tool["inputSchema"]["type"], "object");
        }
    }
}
