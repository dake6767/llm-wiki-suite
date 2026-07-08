use std::path::Path;

use rusqlite::{Connection, params, params_from_iter};

use crate::{Result, SearchResult};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SearchDoc {
    pub wiki: String,
    pub path: String,
    pub slug: String,
    pub title: String,
    pub page_type: String,
    pub tags: Vec<String>,
    pub text: String,
}

#[derive(Debug)]
pub struct FullTextSearcher {
    conn: Connection,
}

impl FullTextSearcher {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        if let Some(parent) = path.as_ref().parent() {
            std::fs::create_dir_all(parent)?;
        }
        let conn = Connection::open(path)?;
        let searcher = Self { conn };
        searcher.init()?;
        Ok(searcher)
    }

    pub fn in_memory() -> Result<Self> {
        let searcher = Self {
            conn: Connection::open_in_memory()?,
        };
        searcher.init()?;
        Ok(searcher)
    }

    pub fn reindex_wiki(&self, wiki: &str, docs: &[SearchDoc]) -> Result<()> {
        self.conn
            .execute("DELETE FROM pages WHERE wiki = ?", params![wiki])?;
        self.insert_docs(docs)?;
        Ok(())
    }

    pub fn reindex_all<'a>(
        &self,
        docs_by_wiki: impl IntoIterator<Item = (&'a str, &'a [SearchDoc])>,
    ) -> Result<()> {
        self.conn.execute("DELETE FROM pages", [])?;
        for (_wiki, docs) in docs_by_wiki {
            self.insert_docs(docs)?;
        }
        Ok(())
    }

    fn insert_docs(&self, docs: &[SearchDoc]) -> Result<()> {
        {
            let mut stmt = self.conn.prepare(
                "INSERT INTO pages(title, text, wiki, path, slug, type, tags) VALUES (?,?,?,?,?,?,?)",
            )?;
            for doc in docs {
                stmt.execute(params![
                    doc.title,
                    doc.text,
                    doc.wiki,
                    doc.path,
                    doc.slug,
                    doc.page_type,
                    format!(" {} ", doc.tags.join(" ")),
                ])?;
            }
        }
        Ok(())
    }

    /// `wiki` 为 `None` 时跨全部库检索（所有库共用一张 FTS 表，
    /// 聚合只是少一个 WHERE 条件，成本与单库同量级）。
    pub fn search(
        &self,
        wiki: Option<&str>,
        query: &str,
        page_type: Option<&str>,
        tag: Option<&str>,
        limit: usize,
    ) -> Result<Vec<SearchResult>> {
        let query = query.trim();
        if query.is_empty() {
            return Ok(Vec::new());
        }

        let mut filters = vec!["1 = 1".to_string()];
        let mut filter_values = Vec::new();
        if let Some(wiki) = wiki.filter(|value| !value.is_empty()) {
            filters.push("wiki = ?".to_string());
            filter_values.push(wiki.to_string());
        }
        if let Some(page_type) = page_type.filter(|value| !value.is_empty()) {
            filters.push("type = ?".to_string());
            filter_values.push(page_type.to_string());
        }
        if let Some(tag) = tag.filter(|value| !value.is_empty()) {
            filters.push("instr(tags, ?) > 0".to_string());
            filter_values.push(format!(" {tag} "));
        }
        let where_clause = filters.join(" AND ");

        match build_match_query(query) {
            Some(match_expr) => {
                self.search_match(&match_expr, &where_clause, filter_values, limit)
            }
            None => self.search_like(query, &where_clause, filter_values, limit),
        }
    }

    fn init(&self) -> Result<()> {
        self.conn.execute_batch(
            r#"
            PRAGMA journal_mode=WAL;
            CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
                title, text,
                wiki UNINDEXED, path UNINDEXED, slug UNINDEXED,
                type UNINDEXED, tags UNINDEXED,
                tokenize='trigram'
            );
            "#,
        )?;
        Ok(())
    }

    fn search_match(
        &self,
        match_expr: &str,
        where_clause: &str,
        filter_values: Vec<String>,
        limit: usize,
    ) -> Result<Vec<SearchResult>> {
        let sql = format!(
            r#"
            SELECT wiki, path, slug, title, type,
                   snippet(pages, 1, '<mark>', '</mark>', '…', 12) AS snip,
                   bm25(pages) AS score
            FROM pages
            WHERE pages MATCH ? AND {where_clause}
            ORDER BY score
            LIMIT ?
            "#
        );
        let mut values = vec![match_expr.to_string()];
        values.extend(filter_values);
        values.push(limit.to_string());
        let mut stmt = self.conn.prepare(&sql)?;
        let rows = stmt.query_map(params_from_iter(values), |row| {
            Ok(SearchResult {
                wiki: row.get(0)?,
                path: row.get(1)?,
                slug: row.get(2)?,
                title: row.get(3)?,
                page_type: row.get(4)?,
                snippet: row.get(5)?,
                score: row.get(6)?,
            })
        })?;
        collect_rows(rows)
    }

    fn search_like(
        &self,
        query: &str,
        where_clause: &str,
        filter_values: Vec<String>,
        limit: usize,
    ) -> Result<Vec<SearchResult>> {
        let sql = format!(
            r#"
            SELECT wiki, path, slug, title, type, text, 0.0 AS score
            FROM pages
            WHERE (title LIKE ? OR text LIKE ?) AND {where_clause}
            LIMIT ?
            "#
        );
        let like = format!("%{query}%");
        let mut values = vec![like.clone(), like];
        values.extend(filter_values);
        values.push(limit.to_string());
        let mut stmt = self.conn.prepare(&sql)?;
        let rows = stmt.query_map(params_from_iter(values), |row| {
            let text: String = row.get(5)?;
            Ok(SearchResult {
                wiki: row.get(0)?,
                path: row.get(1)?,
                slug: row.get(2)?,
                title: row.get(3)?,
                page_type: row.get(4)?,
                snippet: make_snippet(&text, query, 60),
                score: row.get(6)?,
            })
        })?;
        collect_rows(rows)
    }
}

fn collect_rows<T>(
    rows: rusqlite::MappedRows<'_, impl FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<T>>,
) -> Result<Vec<T>> {
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

/// 把用户 query 切成若干检索词，逐词加引号后用 ` OR ` 连接，交给 FTS5 trigram MATCH。
///
/// 为什么要拆：`escape_match` 若把整条 query 包成一个短语，trigram 就要求这一整串
/// 连续出现，多关键词查询（“穆斯林 阿拉伯 先知”）几乎永远不命中。拆成逐词 OR 后，
/// 命中词最多的页面由 bm25 自然排到前面，召回大幅提升。
///
/// 切词规则：以空白、标点、以及结构助词「的」为分隔符（trigram 本就是子串匹配，
/// 这些连接词只会拖低召回）。trigram 至少要 3 个字符才能建索引，故短于 3 字符的
/// 词直接丢弃；若没有任何 >=3 字符的词，返回 `None`，交由上层回退到 LIKE。
fn build_match_query(query: &str) -> Option<String> {
    let terms: Vec<String> = query
        .split(is_term_separator)
        .filter(|term| term.chars().count() >= 3)
        .map(escape_match)
        .collect();
    if terms.is_empty() {
        None
    } else {
        Some(terms.join(" OR "))
    }
}

/// 切词分隔符：空白、常见中英文标点、以及结构助词「的」。
/// 刻意保留 `-` `.` `_` `#` `+`，避免把 `gpt-4`、`node.js`、`C++` 这类技术词切碎。
fn is_term_separator(c: char) -> bool {
    c.is_whitespace()
        || matches!(
            c,
            '的' | ',' | ';' | ':' | '|' | '/' | '\\'
                | '，' | '、' | '。' | '；' | '：' | '！' | '？'
                | '（' | '）' | '【' | '】' | '「' | '」' | '『' | '』'
                | '《' | '》' | '·' | '…'
        )
}

fn escape_match(query: &str) -> String {
    format!("\"{}\"", query.replace('"', "\"\""))
}

fn make_snippet(text: &str, query: &str, width: usize) -> String {
    let lower_text = text.to_lowercase();
    let lower_query = query.to_lowercase();
    let Some(idx) = lower_text.find(&lower_query) else {
        return if text.len() > width {
            format!("{}…", &text[..safe_boundary(text, width)])
        } else {
            text.to_string()
        };
    };
    let start = idx.saturating_sub(width / 2);
    let end = (idx + query.len() + width / 2).min(text.len());
    let start = safe_boundary_back(text, start);
    let end = safe_boundary(text, end);
    let mut snippet = String::new();
    if start > 0 {
        snippet.push('…');
    }
    snippet.push_str(&text[start..idx]);
    snippet.push_str("<mark>");
    snippet.push_str(&text[idx..idx + query.len()]);
    snippet.push_str("</mark>");
    snippet.push_str(&text[idx + query.len()..end]);
    if end < text.len() {
        snippet.push('…');
    }
    snippet
}

fn safe_boundary(text: &str, mut idx: usize) -> usize {
    idx = idx.min(text.len());
    while idx > 0 && !text.is_char_boundary(idx) {
        idx -= 1;
    }
    idx
}

fn safe_boundary_back(text: &str, mut idx: usize) -> usize {
    idx = idx.min(text.len());
    while idx < text.len() && !text.is_char_boundary(idx) {
        idx += 1;
    }
    idx
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn searches_with_trigram_and_short_like_fallback() {
        let searcher = FullTextSearcher::in_memory().unwrap();
        searcher
            .reindex_wiki(
                "demo",
                &[
                    SearchDoc {
                        wiki: "demo".to_string(),
                        path: "entities/claude".to_string(),
                        slug: "claude".to_string(),
                        title: "Claude".to_string(),
                        page_type: "entities".to_string(),
                        tags: vec!["ai".to_string()],
                        text: "Claude 是一个 agent".to_string(),
                    },
                    SearchDoc {
                        wiki: "demo".to_string(),
                        path: "concepts/mcp".to_string(),
                        slug: "mcp".to_string(),
                        title: "MCP".to_string(),
                        page_type: "concepts".to_string(),
                        tags: vec!["protocol".to_string()],
                        text: "模型上下文协议".to_string(),
                    },
                ],
            )
            .unwrap();

        let hits = searcher
            .search(Some("demo"), "Claude", None, Some("ai"), 10)
            .unwrap();
        assert_eq!(hits[0].path, "entities/claude");
        assert_eq!(hits[0].wiki, "demo");

        let hits = searcher
            .search(Some("demo"), "模型", Some("concepts"), None, 10)
            .unwrap();
        assert_eq!(hits[0].path, "concepts/mcp");
        assert!(hits[0].snippet.contains("<mark>模型</mark>"));
    }

    #[test]
    fn multi_keyword_query_is_ored_not_treated_as_one_phrase() {
        let searcher = FullTextSearcher::in_memory().unwrap();
        searcher
            .reindex_wiki(
                "demo",
                &[SearchDoc {
                    wiki: "demo".to_string(),
                    path: "concepts/compiler.md".to_string(),
                    slug: "compiler".to_string(),
                    title: "编程语言".to_string(),
                    page_type: "concepts".to_string(),
                    tags: Vec::new(),
                    text: "编译器与解释器的实现原理".to_string(),
                }],
            )
            .unwrap();

        // 多个关键词以空格分隔：只要命中其中一个词就应召回，
        // 而不是要求整条串原样连续出现。
        let hits = searcher
            .search(Some("demo"), "编译器 数据库 操作系统", None, None, 10)
            .unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].path, "concepts/compiler.md");
    }

    #[test]
    fn splits_on_structural_particle_de() {
        let searcher = FullTextSearcher::in_memory().unwrap();
        searcher
            .reindex_wiki(
                "demo",
                &[SearchDoc {
                    wiki: "demo".to_string(),
                    path: "concepts/compiler.md".to_string(),
                    slug: "compiler".to_string(),
                    title: "编程语言".to_string(),
                    page_type: "concepts".to_string(),
                    tags: Vec::new(),
                    text: "编译器在现代工具链中的应用".to_string(),
                }],
            )
            .unwrap();

        // “编译器的历史” 作为整条短语无法命中“编译器在现代…”，
        // 但按「的」切词后 “编译器”(>=3) 应命中；“历史”(<3) 丢弃。
        let hits = searcher
            .search(Some("demo"), "编译器的历史", None, None, 10)
            .unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].path, "concepts/compiler.md");
    }

    #[test]
    fn build_match_query_drops_short_terms_and_falls_back() {
        // 全部词都 <3 字符 → None（上层回退 LIKE）
        assert_eq!(build_match_query("AI 的 应用"), None);
        // 保留 >=3 字符的词，逐词加引号并 OR
        assert_eq!(
            build_match_query("编译器 解释器 语言"),
            Some("\"编译器\" OR \"解释器\"".to_string())
        );
        // 无分隔符的整串保持单短语（不破坏精确短语检索）
        assert_eq!(
            build_match_query("模型上下文协议"),
            Some("\"模型上下文协议\"".to_string())
        );
    }

    #[test]
    fn searches_across_all_wikis_when_wiki_is_none() {
        let searcher = FullTextSearcher::in_memory().unwrap();
        for wiki in ["alpha", "beta"] {
            searcher
                .reindex_wiki(
                    wiki,
                    &[SearchDoc {
                        wiki: wiki.to_string(),
                        path: "concepts/needle.md".to_string(),
                        slug: "needle".to_string(),
                        title: format!("Needle {wiki}"),
                        page_type: "concepts".to_string(),
                        tags: Vec::new(),
                        text: "needle in haystack".to_string(),
                    }],
                )
                .unwrap();
        }

        // 不指定库 → 两个库都命中，且每条带出所属 wiki
        let hits = searcher.search(None, "needle", None, None, 10).unwrap();
        let wikis = hits.iter().map(|hit| hit.wiki.as_str()).collect::<Vec<_>>();
        assert!(wikis.contains(&"alpha") && wikis.contains(&"beta"));

        // 指定库 → 只剩该库
        let hits = searcher
            .search(Some("alpha"), "needle", None, None, 10)
            .unwrap();
        assert!(hits.iter().all(|hit| hit.wiki == "alpha"));
        assert!(!hits.is_empty());
    }
}
