//! MCP streamable HTTP 端点（POST /mcp）。
//!
//! 无会话、无 SSE 的最小实现：每个 JSON-RPC 请求独立处理，响应一律
//! `application/json`——MCP 规范允许服务端不提供流式通道，此时 GET 返回
//! 405 即可（也是这里的行为）。工具契约（名称/描述/schema）在 llm-wiki-mcp
//! crate，本模块只做协议分发与工具执行，执行逻辑全部复用 /api/v1 同一套
//! AppState（索引 + 全文搜索），保证 MCP 与 Web API 读到的东西一致。

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use axum::{
    Json,
    extract::State,
    http::{StatusCode, header},
    response::{IntoResponse, Response},
};
use llm_wiki_mcp::{
    PagePathArgs, ReadPagesArgs, RetrieveContextArgs, SearchWikiArgs, ToolName, WikiArgs, tool_list,
};
use serde_json::{Value, json};

use super::{AppState, read_manager, safe_join};
use llm_wiki_core::parser;

/// 本实现支持的协议版本；客户端要的版本在列表内就照办，否则回落最新版。
const SUPPORTED_VERSIONS: &[&str] = &["2025-06-18", "2025-03-26", "2024-11-05"];

pub(crate) async fn mcp_post(
    State(state): State<AppState>,
    Json(message): Json<Value>,
) -> Response {
    let Some(obj) = message.as_object() else {
        // 批量请求（JSON 数组）已在 2025-06-18 版移除，这里直接拒绝。
        return rpc_error(
            Value::Null,
            -32600,
            "expected a single JSON-RPC object".into(),
        )
        .into_response();
    };
    let method = obj
        .get("method")
        .and_then(|value| value.as_str())
        .unwrap_or_default()
        .to_string();
    let params = obj.get("params").cloned().unwrap_or(Value::Null);
    // 通知（无 id）不需要响应体：按 streamable HTTP 规范回 202。
    let Some(id) = obj.get("id").filter(|id| !id.is_null()).cloned() else {
        return StatusCode::ACCEPTED.into_response();
    };

    let result = match method.as_str() {
        "initialize" => Ok(initialize_result(&params)),
        "ping" => Ok(json!({})),
        "tools/list" => Ok(tool_list()),
        "tools/call" => tools_call(&state, &params),
        other => Err((-32601, format!("method not found: {other}"))),
    };
    match result {
        Ok(result) => Json(json!({"jsonrpc": "2.0", "id": id, "result": result})).into_response(),
        Err((code, message)) => rpc_error(id, code, message).into_response(),
    }
}

/// GET/DELETE /mcp：不提供 SSE 流与会话，按规范回 405 并标注可用方法。
pub(crate) async fn mcp_method_not_allowed() -> Response {
    (
        StatusCode::METHOD_NOT_ALLOWED,
        [(header::ALLOW, "POST")],
        Json(json!({"detail": "MCP endpoint accepts POST JSON-RPC only"})),
    )
        .into_response()
}

fn rpc_error(id: Value, code: i64, message: String) -> Json<Value> {
    Json(json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}))
}

fn initialize_result(params: &Value) -> Value {
    let requested = params
        .get("protocolVersion")
        .and_then(|value| value.as_str())
        .unwrap_or_default();
    let version = if SUPPORTED_VERSIONS.contains(&requested) {
        requested
    } else {
        SUPPORTED_VERSIONS[0]
    };
    json!({
        "protocolVersion": version,
        "capabilities": {"tools": {}},
        "serverInfo": {
            "name": "my-llm-wiki-browser",
            "version": env!("CARGO_PKG_VERSION"),
        },
    })
}

/// tools/call 分发。协议层问题（未知工具、参数不合法）返回 JSON-RPC error；
/// 工具执行层问题（wiki/页面不存在等）按规范包成 isError 的工具结果，
/// 让 agent 能读到原因并自行调整参数重试。
fn tools_call(state: &AppState, params: &Value) -> Result<Value, (i64, String)> {
    let name = params
        .get("name")
        .and_then(|value| value.as_str())
        .ok_or((-32602, "missing tool name".to_string()))?;
    let tool = ToolName::parse(name).ok_or((-32602, format!("unknown tool: {name}")))?;
    let arguments = params.get("arguments").cloned().unwrap_or(json!({}));

    let outcome = match tool {
        ToolName::ListWikis => run_list_wikis(state),
        ToolName::SearchWiki => {
            let args: SearchWikiArgs = parse_args(arguments)?;
            run_search_wiki(state, args)
        }
        ToolName::RetrieveContext => {
            let args: RetrieveContextArgs = parse_args(arguments)?;
            run_retrieve_context(state, args)
        }
        ToolName::ReadPage => {
            let args: PagePathArgs = parse_args(arguments)?;
            run_read_page(state, args)
        }
        ToolName::ReadPages => {
            let args: ReadPagesArgs = parse_args(arguments)?;
            run_read_pages(state, args)
        }
        ToolName::ReadRaw => {
            let args: PagePathArgs = parse_args(arguments)?;
            run_read_raw(state, args)
        }
        ToolName::ListWikiTree => {
            let args: WikiArgs = parse_args(arguments)?;
            run_list_wiki_tree(state, args)
        }
    };

    Ok(match outcome {
        Ok(payload) => tool_result(payload, false),
        Err(message) => tool_result(json!({"error": message}), true),
    })
}

fn parse_args<T: serde::de::DeserializeOwned>(arguments: Value) -> Result<T, (i64, String)> {
    serde_json::from_value(arguments).map_err(|err| (-32602, format!("invalid arguments: {err}")))
}

fn tool_result(payload: Value, is_error: bool) -> Value {
    let text = serde_json::to_string_pretty(&payload).unwrap_or_else(|_| payload.to_string());
    json!({
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    })
}

fn run_list_wikis(state: &AppState) -> Result<Value, String> {
    let manager = read_manager(state).map_err(|_| "index unavailable".to_string())?;
    let mut wikis = manager
        .wikis
        .values()
        .map(|idx| {
            json!({
                "key": idx.entry.key,
                "name": idx.entry.name,
                "description": idx.entry.description.clone().unwrap_or_default(),
                "default": idx.entry.default,
                "pageCount": idx.pages.len(),
            })
        })
        .collect::<Vec<_>>();
    wikis.sort_by_key(|wiki| {
        (
            !wiki["default"].as_bool().unwrap_or(false),
            wiki["name"].as_str().unwrap_or_default().to_string(),
        )
    });
    Ok(json!({"wikis": wikis}))
}

fn run_search_wiki(state: &AppState, args: SearchWikiArgs) -> Result<Value, String> {
    let query = args.query.trim();
    if query.is_empty() {
        return Err("query must not be empty".into());
    }
    // wiki 缺省 = 跨全部库；给了就先验证存在，让打错 key 得到明确报错而非空结果。
    if let Some(wiki) = args.wiki.as_deref() {
        let manager = read_manager(state).map_err(|_| "index unavailable".to_string())?;
        manager
            .get(wiki)
            .ok_or_else(|| format!("wiki not found: {wiki}"))?;
    }
    let limit = args.limit.unwrap_or(8).clamp(1, 50);
    let hits = state
        .searcher
        .lock()
        .map_err(|_| "search index unavailable".to_string())?
        .search(
            args.wiki.as_deref(),
            query,
            args.r#type.as_deref(),
            args.tag.as_deref(),
            limit,
        )
        .map_err(|_| "search failed".to_string())?
        .into_iter()
        .map(|result| {
            json!({
                "wiki": result.wiki,
                "path": result.path,
                "title": result.title,
                "type": result.page_type,
                "snippet": result.snippet,
                "score": result.score,
            })
        })
        .collect::<Vec<_>>();
    Ok(json!({
        "scope": args.wiki.unwrap_or_else(|| "all".to_string()),
        "query": query,
        "total": hits.len(),
        "hits": hits,
    }))
}

#[derive(Debug)]
struct ContextCandidate {
    wiki: String,
    path: String,
    depth: usize,
    retrieval_score: f64,
    search_score: Option<f64>,
    search_rank: Option<usize>,
    search_snippet: Option<String>,
    discovered_via: Vec<Value>,
}

fn context_candidate_score(candidate: &ContextCandidate) -> f64 {
    // 多条独立路径是轻量的相关性佐证，但不能压过种子/边类型本身。
    let corroboration = candidate.discovered_via.len().saturating_sub(1).min(4) as f64 * 0.025;
    candidate.retrieval_score + corroboration
}

/// 全文命中作为种子，再在每个种子所属 Wiki 内沿显式 wikilink 做有界扩展。
/// 搜索排名被归一化为 0..1 的种子分；出链和反链分别施加不同权重，下一跳继续
/// 衰减。最终正文复用 read_pages 的字符预算语义，避免一次图遍历重造整库上下文。
fn run_retrieve_context(state: &AppState, args: RetrieveContextArgs) -> Result<Value, String> {
    let query = args.query.trim();
    if query.is_empty() {
        return Err("query must not be empty".into());
    }
    if let Some(wiki) = args.wiki.as_deref() {
        let manager = read_manager(state).map_err(|_| "index unavailable".to_string())?;
        manager
            .get(wiki)
            .ok_or_else(|| format!("wiki not found: {wiki}"))?;
    }

    let (seed_limit, max_depth, max_nodes, per_page, total_budget) = args.budget();
    let seeds = state
        .searcher
        .lock()
        .map_err(|_| "search index unavailable".to_string())?
        .search(
            args.wiki.as_deref(),
            query,
            args.r#type.as_deref(),
            args.tag.as_deref(),
            seed_limit,
        )
        .map_err(|_| "search failed".to_string())?;

    let manager = read_manager(state).map_err(|_| "index unavailable".to_string())?;
    let mut candidates: BTreeMap<(String, String), ContextCandidate> = BTreeMap::new();
    let mut queue: VecDeque<(String, String, usize, f64)> = VecDeque::new();
    let mut queued_depth: BTreeMap<(String, String), usize> = BTreeMap::new();

    for (rank, hit) in seeds.iter().enumerate() {
        let path = parser::normalize_page_path(&hit.path);
        if manager
            .get(&hit.wiki)
            .and_then(|idx| idx.pages.get(&path))
            .is_none()
        {
            continue;
        }
        // 相邻搜索名次保持接近，让强关系邻居可以超过尾部弱种子，但不会超过首个种子。
        let retrieval_score = 1.0 / (1.0 + rank as f64 * 0.15);
        let key = (hit.wiki.clone(), path.clone());
        candidates.insert(
            key.clone(),
            ContextCandidate {
                wiki: hit.wiki.clone(),
                path: path.clone(),
                depth: 0,
                retrieval_score,
                search_score: Some(hit.score),
                search_rank: Some(rank + 1),
                search_snippet: Some(hit.snippet.clone()),
                discovered_via: vec![json!({
                    "relation": "search",
                    "query": query,
                    "rank": rank + 1,
                })],
            },
        );
        queued_depth.insert(key, 0);
        queue.push_back((hit.wiki.clone(), path, 0, retrieval_score));
    }

    // 多收集少量候选后再排序，避免目录顺序决定最终结果；仍设置硬上限防止高连接图膨胀。
    let discovery_ceiling = max_nodes
        .saturating_mul(seed_limit)
        .saturating_mul(max_depth + 1)
        .max(seed_limit)
        .min(200);
    while let Some((wiki, path, depth, branch_score)) = queue.pop_front() {
        if depth >= max_depth {
            continue;
        }
        let Some(idx) = manager.get(&wiki) else {
            continue;
        };
        let Some(record) = idx.pages.get(&path) else {
            continue;
        };
        // 同一节点可能先后被多个种子发现；扩展时使用已经汇总出的最佳路径分。
        let branch_score = candidates
            .get(&(wiki.clone(), path.clone()))
            .map(|candidate| candidate.retrieval_score.max(branch_score))
            .unwrap_or(branch_score);

        let mut neighbors: Vec<(String, &'static str, f64)> = Vec::new();
        let outgoing = record
            .targets
            .iter()
            .filter_map(|target| idx.resolve(target))
            .filter(|target| *target != path)
            .map(str::to_string)
            .collect::<BTreeSet<_>>();
        neighbors.extend(
            outgoing
                .into_iter()
                .map(|target| (target, "outgoingLink", 0.72)),
        );
        neighbors.extend(
            idx.backlinks
                .get(&path)
                .into_iter()
                .flatten()
                .filter(|target| target.as_str() != path)
                .cloned()
                .map(|target| (target, "backlink", 0.58)),
        );
        // 每个节点最多贡献一组最终可返回规模的邻居，避免单个 hub 垄断全局候选预算。
        neighbors.truncate(max_nodes.min(20));

        for (neighbor, relation, relation_weight) in neighbors {
            let next_depth = depth + 1;
            let score = branch_score * relation_weight;
            let key = (wiki.clone(), neighbor.clone());
            let provenance = json!({
                "relation": relation,
                "page": path,
                "depth": next_depth,
            });
            let mut accepted = false;
            if let Some(candidate) = candidates.get_mut(&key) {
                candidate.depth = candidate.depth.min(next_depth);
                candidate.retrieval_score = candidate.retrieval_score.max(score);
                if !candidate.discovered_via.contains(&provenance) {
                    candidate.discovered_via.push(provenance);
                }
                accepted = true;
            } else if candidates.len() < discovery_ceiling {
                candidates.insert(
                    key.clone(),
                    ContextCandidate {
                        wiki: wiki.clone(),
                        path: neighbor.clone(),
                        depth: next_depth,
                        retrieval_score: score,
                        search_score: None,
                        search_rank: None,
                        search_snippet: None,
                        discovered_via: vec![provenance],
                    },
                );
                accepted = true;
            }

            if accepted
                && next_depth < max_depth
                && queued_depth
                    .get(&key)
                    .is_none_or(|known_depth| next_depth < *known_depth)
            {
                queued_depth.insert(key, next_depth);
                queue.push_back((wiki.clone(), neighbor, next_depth, score));
            }
        }
    }

    let total_discovered = candidates.len();
    let seed_count = candidates.values().filter(|item| item.depth == 0).count();
    let mut ranked = candidates.into_values().collect::<Vec<_>>();
    ranked.sort_by(|a, b| {
        context_candidate_score(b)
            .total_cmp(&context_candidate_score(a))
            .then_with(|| a.depth.cmp(&b.depth))
            .then_with(|| (&a.wiki, &a.path).cmp(&(&b.wiki, &b.path)))
    });

    let mut nodes = Vec::new();
    let mut omitted = Vec::new();
    let mut total_chars = 0usize;
    for (rank, candidate) in ranked.into_iter().enumerate() {
        if rank >= max_nodes {
            omitted.push(json!({
                "wiki": candidate.wiki,
                "path": candidate.path,
                "reason": "maxNodes",
            }));
            continue;
        }
        if total_budget.saturating_sub(total_chars) < 200 {
            omitted.push(json!({
                "wiki": candidate.wiki,
                "path": candidate.path,
                "reason": "maxTotalChars",
            }));
            continue;
        }
        let Some(idx) = manager.get(&candidate.wiki) else {
            continue;
        };
        let Some(record) = idx.pages.get(&candidate.path) else {
            continue;
        };
        let body_budget = per_page.min(total_budget - total_chars);
        let original_chars = record.body.chars().count();
        let (body, truncated) = if original_chars > body_budget {
            let cut = record
                .body
                .char_indices()
                .nth(body_budget)
                .map(|(index, _)| index)
                .unwrap_or(record.body.len());
            (format!("{}…", &record.body[..cut]), true)
        } else {
            (record.body.clone(), false)
        };
        let chars = original_chars.min(body_budget);
        total_chars += chars;

        let outgoing_links = record
            .targets
            .iter()
            .filter_map(|target| idx.resolve(target))
            .collect::<BTreeSet<_>>();
        let backlinks = idx
            .backlinks
            .get(&candidate.path)
            .into_iter()
            .flatten()
            .collect::<Vec<_>>();
        nodes.push(json!({
            "rank": rank + 1,
            "wiki": candidate.wiki,
            "path": record.path,
            "title": record.title,
            "type": record.page_type,
            "tags": record.tags,
            "sources": record.sources,
            "depth": candidate.depth,
            "retrievalScore": context_candidate_score(&candidate),
            "searchScore": candidate.search_score,
            "searchRank": candidate.search_rank,
            "searchSnippet": candidate.search_snippet,
            "discoveredVia": candidate.discovered_via,
            "outgoingLinks": outgoing_links,
            "backlinks": backlinks,
            "chars": chars,
            "originalChars": original_chars,
            "truncated": truncated,
            "body": body,
        }));
    }

    Ok(json!({
        "scope": args.wiki.unwrap_or_else(|| "all".to_string()),
        "query": query,
        "strategy": "fullText+wikilinkGraph",
        "budget": {
            "seedLimit": seed_limit,
            "maxDepth": max_depth,
            "maxNodes": max_nodes,
            "maxCharsPerPage": per_page,
            "maxTotalChars": total_budget,
        },
        "seedCount": seed_count,
        "discoveredCount": total_discovered,
        "returnedCount": nodes.len(),
        "totalChars": total_chars,
        "nodes": nodes,
        "omitted": omitted,
    }))
}

fn run_read_page(state: &AppState, args: PagePathArgs) -> Result<Value, String> {
    let manager = read_manager(state).map_err(|_| "index unavailable".to_string())?;
    let idx = manager
        .get(&args.wiki)
        .ok_or_else(|| format!("wiki not found: {}", args.wiki))?;
    let path = parser::normalize_page_path(&args.path);
    let rec = idx
        .pages
        .get(&path)
        .ok_or_else(|| format!("page not found: {path}"))?;
    // 正文保留原始 [[wikilinks]]（不像 Web API 那样改写成站内 URL），
    // agent 拿到的链接目标可直接回喂 read_page。
    let outgoing = rec
        .targets
        .iter()
        .filter_map(|target| idx.resolve(target))
        .collect::<std::collections::BTreeSet<_>>();
    let backlinks = idx
        .backlinks
        .get(&path)
        .into_iter()
        .flatten()
        .collect::<Vec<_>>();
    Ok(json!({
        "wiki": args.wiki,
        "path": rec.path,
        "title": rec.title,
        "type": rec.page_type,
        "tags": rec.tags,
        "sources": rec.sources,
        "frontmatter": rec.frontmatter,
        "outgoingLinks": outgoing,
        "backlinks": backlinks,
        "body": rec.body,
    }))
}

/// 有界批量读取：这是「top-k 检索 → 读候选页」链条的打包步骤。
/// 单页缺失不整体报错（记入 missing），预算越界按「先截断、后省略」处理，
/// 让 agent 一次调用就拿到可控大小的上下文，而不是循环 read_page 重造大上下文。
fn run_read_pages(state: &AppState, args: ReadPagesArgs) -> Result<Value, String> {
    let manager = read_manager(state).map_err(|_| "index unavailable".to_string())?;
    let idx = manager
        .get(&args.wiki)
        .ok_or_else(|| format!("wiki not found: {}", args.wiki))?;
    if args.paths.is_empty() {
        return Err("paths must not be empty".into());
    }
    let (max_pages, per_page, total_budget) = args.budget();

    // 归一化 + 去重（保序）：search_wiki 的多次查询命中同一页很常见。
    let mut seen = std::collections::BTreeSet::new();
    let normalized: Vec<String> = args
        .paths
        .iter()
        .map(|path| parser::normalize_page_path(path))
        .filter(|path| seen.insert(path.clone()))
        .collect();

    let mut pages = Vec::new();
    let mut missing = Vec::new();
    let mut omitted = Vec::new();
    let mut total_chars = 0usize;
    for path in &normalized {
        let Some(rec) = idx.pages.get(path) else {
            missing.push(path.clone());
            continue;
        };
        if pages.len() >= max_pages || total_budget.saturating_sub(total_chars) < 200 {
            // 页数满或剩余预算不足以承载有意义的内容 → 省略，让 agent 知道还有谁没读。
            omitted.push(path.clone());
            continue;
        }
        let budget = per_page.min(total_budget - total_chars);
        let body_chars = rec.body.chars().count();
        let (body, truncated) = if body_chars > budget {
            let cut = rec
                .body
                .char_indices()
                .nth(budget)
                .map(|(i, _)| i)
                .unwrap_or(rec.body.len());
            (format!("{}…", &rec.body[..cut]), true)
        } else {
            (rec.body.clone(), false)
        };
        total_chars += body_chars.min(budget);
        pages.push(json!({
            "path": rec.path,
            "title": rec.title,
            "type": rec.page_type,
            "sources": rec.sources,
            "chars": body_chars.min(budget),
            "truncated": truncated,
            "body": body,
        }));
    }
    Ok(json!({
        "wiki": args.wiki,
        "budget": {
            "maxPages": max_pages,
            "maxCharsPerPage": per_page,
            "maxTotalChars": total_budget,
        },
        "totalChars": total_chars,
        "pages": pages,
        "missing": missing,
        "omitted": omitted,
    }))
}

fn run_read_raw(state: &AppState, args: PagePathArgs) -> Result<Value, String> {
    let manager = read_manager(state).map_err(|_| "index unavailable".to_string())?;
    let idx = manager
        .get(&args.wiki)
        .ok_or_else(|| format!("wiki not found: {}", args.wiki))?;
    let raw_root = &idx.entry.raw_dir;
    let rel = args.path.strip_prefix("raw/").unwrap_or(&args.path);
    let (target, resolved_rel) =
        resolve_raw_source(raw_root, rel)?.ok_or_else(|| format!("raw source not found: {rel}"))?;
    let parsed =
        parser::parse_file(&target).map_err(|_| "failed to read raw source".to_string())?;
    Ok(json!({
        "wiki": args.wiki,
        "path": format!("raw/{resolved_rel}"),
        "frontmatter": parsed.frontmatter,
        "body": parsed.body,
    }))
}

/// Resolve both canonical raw-relative paths (`sources/x/foo.md`) and legacy
/// source references (`x/foo.md`) emitted by older wiki pages. Keep all joins
/// behind `safe_join`, so the compatibility fallback cannot escape raw/.
fn resolve_raw_source(
    raw_root: &std::path::Path,
    rel: &str,
) -> Result<Option<(std::path::PathBuf, String)>, String> {
    if let Some(resolved) = resolve_raw_candidate(raw_root, rel)? {
        return Ok(Some(resolved));
    }
    if !rel.starts_with("sources/") {
        return resolve_raw_candidate(raw_root, &format!("sources/{rel}"));
    }
    Ok(None)
}

fn resolve_raw_candidate(
    raw_root: &std::path::Path,
    rel: &str,
) -> Result<Option<(std::path::PathBuf, String)>, String> {
    let target = safe_join(raw_root, rel).ok_or_else(|| "invalid path".to_string())?;
    if target.is_file() {
        return Ok(Some((target, rel.to_string())));
    }
    if !rel.ends_with(".md") {
        let with_extension = format!("{rel}.md");
        let target =
            safe_join(raw_root, &with_extension).ok_or_else(|| "invalid path".to_string())?;
        if target.is_file() {
            return Ok(Some((target, with_extension)));
        }
    }
    Ok(None)
}

fn run_list_wiki_tree(state: &AppState, args: WikiArgs) -> Result<Value, String> {
    let manager = read_manager(state).map_err(|_| "index unavailable".to_string())?;
    let idx = manager
        .get(&args.wiki)
        .ok_or_else(|| format!("wiki not found: {}", args.wiki))?;
    let mut buckets: std::collections::BTreeMap<String, Vec<Value>> =
        std::collections::BTreeMap::new();
    for rec in idx.pages.values() {
        let top = rec
            .path
            .split_once('/')
            .map(|(head, _)| head)
            .unwrap_or("_root")
            .to_string();
        buckets
            .entry(top)
            .or_default()
            .push(json!({"path": rec.path, "title": rec.title}));
    }
    let types = buckets
        .into_iter()
        .map(|(page_type, mut pages)| {
            pages.sort_by_key(|page| page["title"].as_str().unwrap_or_default().to_string());
            json!({"type": page_type, "count": pages.len(), "pages": pages})
        })
        .collect::<Vec<_>>();
    Ok(json!({"wiki": args.wiki, "types": types}))
}
