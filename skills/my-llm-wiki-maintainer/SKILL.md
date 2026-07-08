---
name: my-llm-wiki-maintainer
description: "Operate and maintain an existing agent-native LLM Wiki — a Karpathy-style Markdown knowledge base — compiling already-captured raw sources into interlinked wiki pages and keeping them healthy. Use to initialize a wiki, ingest/update raw sources into pages, run the review queue, deep research, dedup, lint, answer with cited queries and save them back, merge pages and the index, or refresh the overview, all per one explicit project root and without the LLM Wiki desktop app. NOT for fetching or capturing a new URL, X/Twitter post, 公众号 article, 小红书 note, video, or personal idea into RAW — that upstream capture is the my-llm-wiki skill, and this skill consumes raw sources that already exist. 用于维护与运营 LLM Wiki 知识库——把已沉淀的 raw 源编译成相互链接的 wiki 页面，跑 review、deep research、lint、去重、带引用的查询与回存。ALSO the query entrance: whenever the user wants to READ or ASK their wiki — \"查一下知识库\", \"查一下我的 wiki\", \"wiki 里搜 X\", \"知识库里有没有…\", \"之前存过…吗\", \"what does my wiki say about X\" — use this skill's Query flow (browser-search first, cited answer), even if the user names my-llm-wiki or just says 查/搜 with a wiki context."
---

# LLM Wiki Maintainer

Use this skill to act as the maintainer of an LLM Wiki: read raw sources, compile them into interlinked Markdown pages, surface review items, run deep research, answer with citations, and save valuable answers back into the wiki.

**Upstream capture.** Raw sources are usually produced by a capture skill — typically **my-llm-wiki** — which writes `raw/sources/<source_type>/<YYYY-MM-DD>-<slug>.md` with capture frontmatter (`source_url`, `captured_at`, `status: raw`, `tags: [inbox]`, …) and localizes media under `raw/assets/`. Extra frontmatter fields are harmless — ingest reads the body plus what it needs and ignores the rest. A capture skill may hand over one just-written source path to ingest now, or ask to flush the whole inbox (see **Flush pending**). It owns wiki selection (which repo); this skill always operates on one explicit project root it is given.

## Hard Rules

- Resolve one project root before doing anything. A project root contains `purpose.md`, `schema.md`, `wiki/`, and usually `raw/sources/`.
- Keep all state per project under that root. Never write a global review/cache/research file for multiple wikis.
- Treat `raw/sources/` as immutable source material. Write knowledge products under `wiki/`. Keep App-compatible review/lint state under `.llm-wiki/` and Skill-only state under `.llm-wiki/agent/`.
- Use deterministic scripts for path checks, caches, index merging, review queue edits, and lint. Use the LLM for analysis, synthesis, semantic merge, and conservative semantic review.
- Prefer conservative failure. If unsure, create or keep a review item instead of overwriting, deleting, or marking resolved.
- Do not make `wiki/overview.md` part of every ingest. Refresh it only on explicit overview-refresh requests.

## Project Scope

For a raw source path like `/repo/my-wiki/raw/sources/a/b.pdf`, operate only on `/repo/my-wiki`.

Use `scripts/wiki_ops.py resolve-root <path>` before write operations unless the user explicitly supplied a verified root. The same rule applies to review, lint, query, save, and deep research.

Per-project state lives under `.llm-wiki/` (App-compatible: `review.json`, `lint.json`) and `.llm-wiki/agent/` (Skill-only: `ingest-cache.json`, `token-trace.jsonl`, `research/`, `page-history/`, `runs/`). Full layout and the file/state/token policy: `references/project-protocol.md`.

## Workflow

1. Identify the requested operation: initialize, ingest, flush pending (process the inbox), update, review, deep research, dedup, lint, query, save, or refresh overview.
2. Resolve and validate the project root.
3. Load only the reference needed for the operation:
   - File/REVIEW output contract (block shapes, wikilink + filename rules): `references/output-blocks.md`
   - Ingest/update/page merge/index merge: `references/ingest-update.md`
     (concrete end-to-end example: `references/ingest-worked-example.md`)
   - Large-source (map-reduce) ingest for long reports / big PDFs: `references/large-source-ingest.md`
   - Video ingest (YouTube/Bilibili transcript batch workflow): `references/video-ingest-workflow.md`
   - Review/deep research/sweep/dedup: `references/review-research.md`
   - Lint/query/save/overview refresh: `references/lint-query-save.md`
   - File schema/state/token policy: `references/project-protocol.md`
   - Deterministic helper commands: `references/scripts.md`
4. Run deterministic helpers from `scripts/wiki_ops.py` where applicable.
5. Record meaningful state changes in `wiki/log.md` and/or `.llm-wiki/agent/token-trace.jsonl`.
6. When ingest/flush/update added review items, end the final response with a short "可深挖方向" section: each new `suggestion` as one line (title + a hook clause), then one line on how to continue ("想展开哪个，直接说：research <标题>"). A new `contradiction` gets one alert line. Surface titles and hooks only — never raw JSON or item IDs. This is what turns the review queue into offered research directions instead of a silently growing file.
7. After a successful write-backed operation, probe the optional My LLM Wiki Browser share status with `scripts/wiki_ops.py browser-share <root> --page <primary-written-page>`. Prefer a newly written/updated `wiki/sources/...md` source summary as the primary page for ingest/flush/update; otherwise use the saved query page, overview page, or canonical dedup page when there is one. If the command returns `markdownLink`, include that exact Markdown link in the final user response (default label: `点击查看总结`). If it only returns `pageUrl`/`onlineUrl`, format it as `[点击查看总结](url)`; never expose a long bare URL. If the browser is not installed/running, auth fails, or relay is not connected, silently omit the link unless the user explicitly asked for browser/relay diagnostics. Do not store tokenized share URLs in `wiki/`, `wiki/log.md`, review state, or trace files.

## Core Operations

- **Initialize**: create the standard folder layout and template files from `assets/templates/`.
- **Ingest**: analyze a source, emit FILE/REVIEW blocks, run the conflict sentinel (`wiki_ops.py neighbors` + direct-conflict check → `contradiction` review; see `references/ingest-update.md` → Conflict Sentinel), safely apply FILE blocks, merge index deltas, persist review items, and update ingest cache. Gate large sources first with `probe-source`: a long report / big PDF flattened into one un-chunked `.md` takes the two-phase **MAP/REDUCE** path (chunk → per-chunk extract → cross-chunk dedupe → emit blocks) — see `references/large-source-ingest.md`.
- **Flush pending (process the inbox)**: ingest every raw source not yet synthesized for a wiki. List them with `scripts/wiki_ops.py cache pending <root>` — it returns the sources with no cache entry (`new`) or a changed hash (`changed`) — then ingest each per the Ingest flow. `ingest-cache.json` is the authoritative "already synthesized" ledger; a RAW file's `tags: [inbox]` is only an upstream "freshly captured" hint, never the dedupe key. Operates on the one explicit `<root>` you are given — wiki selection is the caller's job. As the batch's last step, run a scoped semantic lint (`wiki_ops.py lint-scope <root>` → checks on `scope[]` → `--mark`; see `references/lint-query-save.md`) — a batch draining is the contradiction-prone moment.
- **Update**: re-ingest only changed sources; merge existing content pages instead of overwriting.
- **Review**: list, create, resolve, or sweep stale review items. Treat Deep Research as a review action. Resolving **marks** (`resolved:true` + `resolvedAction`) via `review resolve` — never hand-delete entries from `review.json`. See `references/review-research.md` → Resolving Items.
- **Deep Research**: **delegation-first** — this skill defines the evidence contract and owns filing/synthesis/ingest-back, but does NOT bundle a search engine or domain tool registry. Infer the domain from `purpose.md`, route retrieval to whatever domain-appropriate skill/MCP exists in the environment (e.g. a finance retrieval skill, a general deep-research skill, a web/Tavily MCP), normalize the returned cited evidence into RAW, synthesize the wiki page yourself, then ingest. See `references/review-research.md` → Deep Research.
- **Lint**: run structural checks in code; use LLM semantic checks only for contradictions, stale claims, duplicates, and missing concepts.
- **Query**: answer from retrieved wiki pages with citations and wikilinks; do not rederive from raw sources unless asked.
- **Save**: save useful answers to `wiki/queries/`, then ingest the saved page so knowledge compounds.
- **Dedup**: detect duplicate entities/concepts saved under different names; after explicit user confirmation, merge a group into one canonical page and rewrite references wiki-wide. Independent maintenance action, never part of ingest.
- **Refresh Overview**: regenerate `wiki/overview.md` as a short digest (one-sentence intro + `## 主要板块` + `## 怎么用`, 5–10 lines, 4-field frontmatter) from `purpose.md`, `schema.md`, and a description-free index listing. Manual only — never during ingest.

## Optional Browser Share Link

The desktop browser is an optional companion app. Maintainer work must not require
it, start it, or fail because it is absent.

At the end of successful ingest/update/flush/review/deep-research/dedup/save/overview
operations, run the probe with the best page to open:

```bash
python3 scripts/wiki_ops.py browser-share <root> --page wiki/sources/example.md
```

Pick the page from the operation's written files:

- Ingest / update / flush: prefer the new or updated `wiki/sources/...md` page for the raw source just synthesized.
- Save: use the `wiki/queries/...md` page.
- Refresh overview: use `wiki/overview.md`.
- Dedup: use the confirmed canonical page.

Read the JSON result:

- `markdownLink` is the preferred final-response value. Add one concise line such as `线上 WIKI: [点击查看总结](...)`.
- `pageUrl` means My LLM Wiki Browser is reachable, its relay is connected, and the helper resolved a concrete browser route for the page. Use it only to build a Markdown link if `markdownLink` is absent.
- `available: true` without `pageUrl` still means the online WIKI root is available. Use `onlineUrl` as a fallback.
- `available: false` means no online link should be shown. Common reasons are `browser-unavailable`, `relay-not-connected`, or `unauthorized`; keep these out of the normal success response unless the user asked you to debug browser access.

Returned URLs already include the browser token because they are meant for the user
to open directly. Treat them as convenience share links: show one as a Markdown
link in the response, but never write it into wiki content, logs, review items, or
agent trace files. Do not paste long bare URLs into chat/IM; they are easy to
break when titles contain spaces or punctuation.

## Output Blocks

Ingest, save, large-source, and review all emit wiki files through two fenced block types: a `---FILE: <path>---` … `---END FILE---` block (wrapping full frontmatter + body) and an optional `---REVIEW: …---` block. The exact block shapes, the `related:`-vs-body wikilink convention, and the source-language filename rule are the **output contract** in `references/output-blocks.md` — load it whenever you generate wiki files.

> **Route boundary.** `evals/route/` regression-guards the trigger split vs the upstream **my-llm-wiki** capture skill — after editing `description`, re-run yao `trigger_eval.py` and promote only if FP/FN deltas stay ≤ 0.
