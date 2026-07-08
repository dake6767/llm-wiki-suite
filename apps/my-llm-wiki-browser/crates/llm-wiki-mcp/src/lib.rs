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
    ReadPage,
    ReadRaw,
    ListWikiTree,
}

impl ToolName {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::ListWikis => "list_wikis",
            Self::SearchWiki => "search_wiki",
            Self::ReadPage => "read_page",
            Self::ReadRaw => "read_raw",
            Self::ListWikiTree => "list_wiki_tree",
        }
    }

    pub fn from_str(name: &str) -> Option<Self> {
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
            Self::ReadPage => {
                "Read one compiled wiki page as markdown: frontmatter metadata, body with \
                 [[wikilinks]], plus resolved outgoing links and backlinks. `path` is a \
                 page path as returned by search_wiki or list_wiki_tree, e.g. 'concepts/mcp'."
            }
            Self::ReadRaw => {
                "Read an original captured source under the wiki's raw/ layer (immutable \
                 originals the pages cite in their `sources` field), e.g. \
                 'sources/web/some-article.md'. Use to re-check a page's claims."
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
            Self::ReadPage => json!({
                "type": "object",
                "properties": {
                    "wiki": wiki,
                    "path": {"type": "string", "description": "Page path, e.g. 'concepts/mcp'."}
                },
                "required": ["wiki", "path"]
            }),
            Self::ReadRaw => json!({
                "type": "object",
                "properties": {
                    "wiki": wiki,
                    "path": {"type": "string", "description": "Raw source path under raw/, e.g. 'sources/web/foo.md'."}
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
    ToolName::ReadPage,
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
pub struct PagePathArgs {
    pub wiki: String,
    pub path: String,
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
            assert_eq!(ToolName::from_str(tool.as_str()), Some(*tool));
        }
        assert_eq!(ToolName::from_str("nope"), None);
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
