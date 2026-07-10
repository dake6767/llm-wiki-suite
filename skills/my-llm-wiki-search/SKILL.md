---
name: my-llm-wiki-search
description: >-
  Read-only lookup and cited Q&A over existing LLM Wikis. Use whenever the user
  asks to look something up in their wiki / knowledge base: “查一下知识库”,
  “查一下我的 wiki”, “wiki 里搜 X”, “知识库里有没有…”, “之前存过…吗”, “我的
  wiki 怎么说 X”, or “what does my wiki say about X” — run bounded top-k
  full-text search, read a few candidate pages under a hard context budget, and
  answer with citations and wikilinks. 只读检索与带引用回答，不做任何写入。Do not
  use to capture/save a NEW source into RAW (that is my-llm-wiki), and do not use
  for wiki maintenance — ingest, review, deep research, dedup, lint, merging,
  saving answers back (that is my-llm-wiki-maintainer).
---

# my-llm-wiki-search — read-only wiki lookup

A thin, read-only public capability: locate the right wiki, retrieve top-k
candidates, read a bounded working set, answer with citations. It never fetches
new sources, never writes RAW, never modifies wiki pages or state files.

The retrieval implementation lives in the shared tool layer (the maintainer's
`wiki_ops.py` and the optional Browser's search/MCP backend — same backend
either way). This skill owns the user-facing query entrance and the canonical
recipe; it deliberately has no scripts of its own.

## Locate the shared tool layer

```bash
SEARCH_SKILL="<this skill directory>"
OPS="$(cd "$SEARCH_SKILL/../my-llm-wiki-maintainer" && pwd)/scripts/wiki_ops.py"
test -f "$OPS"
```

If the check fails, stop and install `my-llm-wiki-maintainer`; do not duplicate
or improvise the tools. Suite installs declare this runtime dependency and
install it automatically. (Loading the maintainer's SKILL.md is NOT needed —
only its script is shared; keep it out of context.)

## Hard rules

- **Read-only.** No writes to `wiki/`, `raw/`, `.llm-wiki/`, or the index. The
  single exception: appending one retrieval-trace line via `wiki_ops.py trace`
  (observability state, not knowledge content).
- **Bounded retrieval, never O(wiki).** Search first, then read at most 3–5
  pages under explicit budgets. Never read the full `wiki/index.md` or a full
  tree listing into context as "search context".
- **The Browser is optional.** Prefer it when present; fall back silently when
  absent. Every step below must succeed without it — absence only makes
  retrieval slightly more expensive, never an error to surface.
- Answer from compiled `wiki/` pages, not raw sources, unless the user
  explicitly asks for raw-source inspection.

## Query recipe (canonical)

1. **Resolve scope.** If the user names a wiki or the conversation has one
   project root in play, use it. Otherwise discover: Browser MCP `list_wikis`,
   or the registry at `~/.my-llm-wiki/wikis.json`, or ask. Cross-wiki search
   (Browser MCP `search_wiki` without a `wiki` key) is the cheapest "which wiki
   was that in?" answer.
2. **Read `purpose.md`** of the chosen wiki (O(1)) if disambiguation or domain
   vocabulary helps the query. Nothing bigger.
3. **Search — three tiers, same backend and budgets, cheapest connected tier
   first, silent fallback:**
   - **Browser MCP** (host has it connected): `search_wiki` with `limit` 8.
   - **Browser HTTP via CLI**:
     `python3 "$OPS" browser-search <root> --q "<keywords>" --top 8`
   - **Local bounded fallback** (no Browser):
     `python3 "$OPS" local-search <root> --q "<keywords>" --top 8`
   Run 1–2 keyword variants if the first query misses; keep each query short
   and specific (entity names beat sentences).
4. **Expand cheaply when needed**: one hop of graph signals —
   `python3 "$OPS" neighbors <root> --page <hit> --max 8` (or a hit page's
   `outgoingLinks`/`backlinks` from MCP `read_page`).
5. **Pack context under a hard budget** — top 3–5 pages only:
   `python3 "$OPS" read-pages <root> --paths ... --max-pages 5
   --max-chars-per-page 6000 --max-total-chars 24000`
   (Browser MCP `read_pages` tool: same budgets, same semantics.)
6. **Answer with citations**: number the packed pages, cite claims as `[1]`,
   reference pages as `[[folder/slug]]` wikilinks. If evidence is insufficient,
   say what is missing — do not pad from general knowledge, and do not silently
   re-derive from raw sources.
7. **Trace the retrieval** (runtime-agnostic metering):
   `python3 "$OPS" trace <root> query.retrieval --backend <mcp|browser|local>
   --candidates <hits> --pages-read <n> --context-chars <chars>`.

## Handoffs (this skill stops at the answer)

- User wants the answer **saved back** into the wiki → `my-llm-wiki-maintainer`
  (Save flow: it writes `wiki/queries/…` and ingests so knowledge compounds).
- User wants to **capture a new source** that came up → `my-llm-wiki`.
- User asks for **maintenance** (review, research, dedup, lint, overview) →
  `my-llm-wiki-maintainer`.

> **Route boundary.** `evals/route/` regression-guards the three-way split:
> read-only lookup → this skill; capture a NEW source → my-llm-wiki;
> write/maintain → my-llm-wiki-maintainer. After editing `description`, re-run
> yao `trigger_eval.py` and promote only if FP/FN deltas stay ≤ 0.
