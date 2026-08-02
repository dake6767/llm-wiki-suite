# Script Reference

All commands use:

```bash
python3 /path/to/my-llm-wiki-maintainer/scripts/wiki_ops.py <command>
```

## Root

```bash
wiki_ops.py resolve-root /path/to/wiki/raw/sources/file.md
```

New Wiki creation is intentionally not a maintainer operation. Load
`my-llm-wiki` and use its canonical `scripts/init_wiki.py`; that path places
additional repositories in the initialized collection and registers the
non-empty topic description in `wikis.json`.

## Index

```bash
wiki_ops.py compact-index /path/to/wiki
wiki_ops.py compact-index /path/to/wiki --no-desc
wiki_ops.py merge-index /path/to/wiki --delta-file /tmp/index-delta.md --write
```

`merge-index` expects delta entries grouped by `## Section`; it merges by normalized wikilink target and preserves existing entries. `compact-index --no-desc` drops the ` — description` tail (sections + page links only) — the input shape for the overview digest.

## Source Page (dedup)

```bash
wiki_ops.py source-page /path/to/wiki --raw raw/sources/x/article.md --url https://x.com/h/status/123
```

Resolves the canonical source page for a raw source so one source never gets two pages. Returns JSON: `existing` (wiki-relative path if a page already covers this raw file or URL — merge into it), `slug`, and `page` (where to write). The slug is deterministic (URL handle+id, or raw filename stem), so re-ingest always targets the same page. Run this before emitting the `wiki/sources/<slug>.md` FILE block.

## Large-Source Ingest (map-reduce)

```bash
wiki_ops.py probe-source /path/to/wiki --raw raw/sources/big-report.md
wiki_ops.py split-source /path/to/wiki --raw raw/sources/big-report.md --url https://… --write
wiki_ops.py stage-status /path/to/wiki --source raw/sources/big-report.md
wiki_ops.py stage-status /path/to/wiki --source raw/sources/big-report.md --mark-done 7
```

For very large sources (a long report / big PDF the capture skill flattened into
one un-chunked `.md`). All stdlib-only; the chunker is a pure function (unit tests
in `scripts/test_chunker.py`). Full SOP in `references/large-source-ingest.md`.

- `probe-source` — size gate. Strips frontmatter, counts BODY chars, estimates
  CJK-aware tokens. Returns `{bodyChars, cjkRatio, estTokens, path: "small"|"large",
  threshold, …}`. Decision is on `bodyChars` vs `--threshold` (default 40000);
  `estTokens` is observability only. `small` → single-pass ingest; `large` → map-reduce.
- `split-source` — chunks the body along H1–H3 (oversized headingless blocks fall
  back to fence-safe char windows; fenced code is never split) into
  `.llm-wiki/agent/ingest-staging/<source-slug>/{manifest.json, chunks/, map/}`.
  Idempotent + resumable via the raw-file hash: same hash → `reused` (keeps MAP
  progress); changed hash → re-split, all `pending`; `--force` wipes and re-splits.
  Without `--write` it is a dry run (prints the manifest). `<source-slug>` =
  `compute_source_slug`, the SAME slug the source page uses.
- `stage-status` — report mode prints `{chunkCount, done, pending, stale,
  pendingIndices, staleIndices, ready}`; it drives resumption and gates REDUCE
  (`ready` only when nothing pending/stale). `--mark-done N` flips chunk N's
  `mapStatus` after confirming `map/chunk-NNN.json` exists and parses (refuses
  otherwise — conservative failure); a done chunk whose map file vanished or whose
  text changed reports as `stale`.

## Apply Blocks

```bash
wiki_ops.py apply-blocks /path/to/wiki --blocks-file /tmp/llm-output.txt --source raw/sources/file.md
```

This applies FILE blocks and persists REVIEW blocks. It skips `wiki/overview.md` during ingest, and **normalizes every content page's `related:` field to bare basename slugs** (so the app's relation panel resolves them) before writing. Existing content pages are skipped unless `--overwrite` is passed. Use `merge-page` or an LLM semantic merge before overwriting existing content pages.

## Page Merge

```bash
wiki_ops.py merge-page /path/to/wiki wiki/concepts/foo.md --incoming-file /tmp/incoming.md
wiki_ops.py merge-page /path/to/wiki wiki/concepts/foo.md --incoming-file /tmp/incoming.md --write
```

This deterministic fallback unions `sources`, `tags`, and `related` (normalizing `related` to bare slugs), locks `type`, `title`, and `created`, and sets `updated` to today. For significant body differences, ask the LLM for a semantic merge first, then use this as a fallback/safety layer.

## Review

```bash
wiki_ops.py review list /path/to/wiki --status open
wiki_ops.py review get /path/to/wiki review-20260611-0001
wiki_ops.py review find /path/to/wiki --type contradiction --status open --limit 5
wiki_ops.py review find /path/to/wiki --q "检索 质量"
wiki_ops.py review add-blocks /path/to/wiki --blocks-file /tmp/output.txt --source raw/sources/file.md
wiki_ops.py review resolve /path/to/wiki review-20260611-0001 --action researched
wiki_ops.py sweep-reviews /path/to/wiki
```

`get`/`find` are the context-budget entry points: they print one item / a
filtered subset as full JSON so handling a single to-do never requires reading
the whole `review.json` into context. `find --q` requires every space-separated
keyword to match title/description/pages/queries; default `--status open`,
`--limit 10`.

`sweep-reviews` implements only deterministic stale-review rules. Use the LLM semantic sweep from `references/review-research.md` for ambiguous remaining items.

## Cache

```bash
wiki_ops.py cache check /path/to/wiki /path/to/wiki/raw/sources/file.md
wiki_ops.py cache save /path/to/wiki /path/to/wiki/raw/sources/file.md wiki/sources/file.md wiki/concepts/foo.md
wiki_ops.py cache save /path/to/wiki /path/to/wiki/raw/sources/file.md --files-file /tmp/pages.json
wiki_ops.py cache pending /path/to/wiki   # list raw sources not yet ingested (new) or changed since (changed)
```

Prefer `--files-file` for generated or CJK-heavy page lists. It accepts a JSON
array or one relative path per line and keeps data out of inline code.

`cache pending` is the batch-discovery entry point for "process the inbox": it scans `raw/sources/**/*.md` and returns those with no cache entry or a changed hash. Read-only; takes one explicit wiki root (no multi-wiki discovery — the caller picks the root).

## Dedup

```bash
wiki_ops.py dedup-summaries /path/to/wiki
wiki_ops.py dedup-merge /path/to/wiki --canonical 野生小虎 --slugs 野生小虎,wild-tiger --body-file /tmp/merged.md
wiki_ops.py dedup-not-duplicate /path/to/wiki --slugs foo,bar
```

`dedup-summaries` emits `{summaries, notDuplicates}` for the LLM detector (scans `wiki/entities` + `wiki/concepts`). `dedup-merge` applies one user-confirmed merge deterministically: frontmatter union, `related` → bare slugs, `[[wikilink]]` + `related:` cross-reference rewrites, `index.md` line removal, backup to `.llm-wiki/agent/page-history/dedup-<stamp>/`, delete merged-away pages. `dedup-not-duplicate` appends a group to the shared whitelist `.llm-wiki/dedup-not-duplicates.json` (App-compatible). Full SOP + LLM prompts in `references/review-research.md`.

## Lint And Trace

```bash
wiki_ops.py lint /path/to/wiki
wiki_ops.py lint /path/to/wiki --exit-code                 # cron/CI gate: non-zero on warnings+
wiki_ops.py lint /path/to/wiki --exit-code --fail-on error # only hard errors trip the gate
wiki_ops.py trace /path/to/wiki ingest.analysis --source raw/sources/file.md --input-chars 12000 --output-chars 3000
wiki_ops.py trace /path/to/wiki query.retrieval --backend browser --candidates 8 --pages-read 4 --context-chars 21000
```

`lint` writes `.llm-wiki/lint.json` and always prints `{count, failing, fail_on, issues}`.
By default it exits 0 (report-only). `--exit-code` turns it into a **cron/CI gate** —
it exits 1 when any issue at or above `--fail-on` (severity ranking `info < warning <
error`, default `warning`) exists, so broken links / missing frontmatter fail a scheduled
health check while advisory `info` items (orphans, no-outlinks) don't. Tag hygiene
adds `too-many-tags` and same-page duplicate/format-variant warnings plus an advisory
`tag-shadows-page` finding when a tag lexically matches an existing concept/entity
slug or title. `trace` appends
JSONL to `.llm-wiki/agent/token-trace.jsonl`; the retrieval flags (`--backend
mcp|browser|local`, `--candidates`, `--pages-read`, `--context-chars`,
`--prompt-tokens`, `--cache-read-tokens`) are the runtime-agnostic counters used
for cross-host before/after comparison (see `project-protocol.md` → Token Policy).

## Tag Vocabulary (read before tagging)

```bash
wiki_ops.py tags <root> --q "检索增强 向量数据库 嵌入"   # INGEST VIEW — always scope it
wiki_ops.py tags <root> --paths-file cands.json   # ...or scope to exact pages (most precise)
wiki_ops.py tags <root>                           # unscoped backbone + bounded singleton tail
wiki_ops.py tags <root> --limit 0                 # all established tags
wiki_ops.py tags <root> --audit                   # cleanup view: + singletons, dups, untagged
wiki_ops.py tags <root> --json --verbose          # machine-readable, plus tag → pages
wiki_ops.py tags-rewrite plan <root> --mapping mapping.json --out plan.json
wiki_ops.py tags-rewrite apply <root> --plan plan.json
wiki_ops.py tags-rewrite rollback <root> --manifest <run>/manifest.json
```

`--q` takes topic words / entity names; punctuation (ASCII or CJK) is treated as
a separator and single characters are dropped, so `检索增强，向量库` and `检索增强 向量库`
behave identically and a query of pure punctuation is rejected rather than
matching everything. Hyphens and dots stay inside tokens — `agent-skills` and
`claude-code` are real tags. `--paths` / `--paths-file` skip term matching
entirely and are the better call when the caller already has a working set.

Derived from the pages on every call — there is no stored vocabulary file to
maintain or to drift out of sync. A hand-kept registry was considered and
rejected: every other derived artifact here (`index.md`, `ingest-cache.json`,
`lint.json`) is regenerable from the pages, and a tag registry would be the
first to hold truth the pages don't, which is exactly what turns it into a
maintenance burden. Canonical choice remains a rule rather than stored truth:
meaning and the Wiki's primary language first, recognized product/proper-name
spelling next, and corpus frequency only as a tie-breaker for ordinary terms.

**Ingest reads the scoped view before generating tags** (`ingest-update.md`
step 5.6, placed before the size gate so the large-source and video paths get it
too): prefer an established tag, then a `*`-marked one, and coin a new word only
when nothing fits. That read-then-extend loop is what makes schema.md's
"reusable across ≥2 pages" rule satisfiable at all.

**Scope it, or the loop is open.** A tag coined by one ingest sits at count 1.
The unscoped view leads with established (≥2 page) tags, so that new word is
invisible to the next ingest, never reused, and can never reach 2 — the facet
would only ever recycle what was already popular. The scoped view lists
one-page tags too, marked `*`. Unscoped calls keep a bounded 25-tag tail as a
fallback, but `--q` / `--paths-file` is the intended ingest path.

**Keep the two views apart.** Both scoped and unscoped are bounded, so cost is
flat in corpus size (~590 tokens on a 900-page wiki, ~440 on a 20-page one) —
that matters because ingest pays it on every source, and the SOP's first
retrieval rule is O(top-k), never O(wiki). `--audit` is the corpus-wide cleanup
view; text respects `--limit`, while `--audit --json` / `--limit 0` exposes the
full singleton, candidate and untagged sets for disk-first batch tooling. That
material is useless mid-ingest and belongs to `health` or an explicit
tag-consolidation pass. `--audit`/`--json` are also the only modes that
pay the O(tags²) duplicate scan. `nearDuplicates` remains a compatibility list;
`candidateGroups` is the governance view. It separates case/space/hyphen/
underscore-only `formatVariants`, CJK character-overlap `semanticReview`, and
mostly-parent/child `containment`. Every group is a hint to judge, never an
automatic merge — the test is lexical, so it finds `检索`/`检索增强` but not
semantic pairs like `大模型`/`LLM`.

`tags-rewrite` is the deterministic write boundary for confirmed tag-to-tag
mappings. `plan` validates exact page counts/paths, rejects Link-don't-tag targets,
and records before/after tags plus hashes without modifying the Wiki. `apply`
requires that exact reviewed plan, locks the project, backs up and journals every
page under `.llm-wiki/agent/page-history/tag-rewrite-<run-id>/`, atomically replaces
each file, then reruns lint/audit. `rollback` restores only when current after-hashes
and backup before-hashes still match. Full classification and confirmation SOP:
`references/tag-governance.md`.

## Wiki Health (project-level drift)

```bash
wiki_ops.py health /path/to/wiki
wiki_ops.py health /path/to/wiki --json
```

`lint` asks "is this page well-formed"; `health` asks "is this wiki still
configured for the corpus it grew into" — schema version gap, empty domain
table, `purpose.md` still holding init placeholders, `overview.md` never
refreshed, tag sprawl. Setup nudges stay quiet under `HEALTH_SOURCE_THRESHOLD`
(12) sources: a young wiki should *not* have a taxonomy yet. Everything it
reports is advisory.

## Schema Upgrade (reach already-initialized wikis)

```bash
wiki_ops.py schema-upgrade /path/to/wiki           # report the version gap
wiki_ops.py schema-upgrade /path/to/wiki --diff    # ...and show the unified diff
wiki_ops.py schema-upgrade /path/to/wiki --apply   # take it (backs up first)
```

`init_wiki.py` copies the schema template into a wiki once and never overwrites
it, so a wiki's conventions freeze on its creation day and later template fixes
never arrive. The `<!-- llm-wiki-schema-version: N -->` marker makes that gap
visible; a marker-less schema reads as v1. `--apply` backs the old file up to
`.llm-wiki/agent/page-history/` and carries the hand-filled `| domain | covers |`
table across, **verifying the carry-over and aborting if it can't be preserved**
rather than reporting success over a lost taxonomy.

## Lint Scope (incremental semantic lint)

```bash
wiki_ops.py lint-scope /path/to/wiki           # report changed + neighbors since last --mark
wiki_ops.py lint-scope /path/to/wiki --mark    # record baseline AFTER the semantic pass ran
```

`lint-scope` hashes every content page and diffs against the Skill-only baseline
in `.llm-wiki/agent/lint-state.json`. Output: `changed[]`, `deleted[]`,
`neighbors[]` (one hop out from each changed page — same graph relations as
`neighbors`), and `scope[]` (changed ∪ neighbors) — the input set for the LLM
semantic lint pass. `firstRun: true` (no baseline yet) puts the whole wiki in
scope. `--mark` snapshots current hashes; run it only after the semantic pass
actually completed. Structural lint (`lint`) is script-cheap and stays full-repo.

## Neighbors (conflict-sentinel input)

```bash
wiki_ops.py neighbors /path/to/wiki --page wiki/concepts/x.md --max 12
wiki_ops.py neighbors /path/to/wiki --slugs "harness,skill是能力商品"   # new page's link targets
```

`neighbors` walks the wiki graph one hop out from a target page: outbound body
wikilinks, inbound wikilinks, `related:` entries (both directions), and pages
sharing a `sources[]` entry (compared by basename). `--page` takes an existing
page; `--slugs` resolves the wikilink targets of a page that hasn't been written
yet (both can combine). Output is JSON with `neighbors[].file` (project-relative)
and `via` (the relation kinds), ranked closest-first — more distinct relations =
tighter neighbor — and capped by `--max` (default 12) so the conflict-sentinel
prompt stays small. Used by the ingest Conflict Sentinel
(`ingest-update.md`); index/log/overview are excluded from the graph.

## Optional Browser Share Link

```bash
wiki_ops.py browser-share
wiki_ops.py browser-share /path/to/wiki --page wiki/sources/example.md
wiki_ops.py browser-share /path/to/wiki --page wiki/sources/example.md --base-url http://127.0.0.1:8800 --token <token>
wiki_ops.py browser-share /path/to/wiki --page wiki/sources/example.md --label 点击查看总结
```

`browser-share` probes the optional My LLM Wiki Browser local API for its current
relay share URL. It reads the persisted browser port and auth token from
`~/.my-llm-wiki/connector/` (or `LLM_WIKI_BROWSER_URL` / `LLM_WIKI_WEB_URL` /
`LLM_WIKI_WEB_TOKEN`) and calls `/api/v1/config/share`. When given a project root
and `--page`, it also calls `/api/v1/config/wikis` to resolve the browser wiki key
and returns `pageUrl`, a deep link like `/w/<wiki>/page/sources/<slug>`. It also
returns `markdownLink` using the label from `--label` (default: `点击查看总结`).

It always exits successfully and prints JSON. Prefer `markdownLink` in the final
user response; fall back to formatting `pageUrl`/`onlineUrl` as
`[点击查看总结](url)` only when `markdownLink` is absent. If `available: false`
(`browser-unavailable`, `relay-not-connected`, `unauthorized`, etc.), omit the
link during normal maintainer output.

## Optional Browser Full-Text Search (Query first tier)

```bash
wiki_ops.py browser-search /path/to/wiki --q "检索词" --top 8
wiki_ops.py browser-search /path/to/wiki --q "检索词" --type concept --tag seo
wiki_ops.py browser-search --wiki <browser-wiki-key> --q "检索词"   # skip root resolution
```

`browser-search` is the Browser-only diagnostic primitive underneath
`retrieval-search`: it queries the browser's full-text index
(`/api/v1/wikis/<key>/search`) instead of hand-scanning `wiki/`, so candidate
retrieval costs stay flat as the wiki grows. Connection and auth resolution are
identical to `browser-share`. The project root (or `--wiki`) picks which
registered wiki to search.

It always exits 0 and prints JSON. On success: `available: true` and `hits[]`,
each hit carrying `file` (project-relative, e.g. `wiki/concepts/x.md` — read it
directly), `title`, `type`, `snippet` (with `<mark>` highlights), `score`. On
`available: false` (`browser-unavailable`, `wiki-key-unresolved`,
`unauthorized`, …) **fall back silently** to `local-search` below — the browser
is optional and its absence is a normal state, not an error to surface.

## Deterministic Retrieval Search (normal CLI entry point)

```bash
wiki_ops.py retrieval-search /path/to/wiki --q "检索词" --top 8
```

Use this command for normal ingest/query CLI retrieval. It calls
`browser-search` first and, only when the Browser reports `available: false`,
runs the bounded `local-search` fallback. Its JSON always includes `backend` as
`browser` or `local`; copy that value into the retrieval trace. Keeping the
choice inside the deterministic helper prevents agents from skipping the
Browser after seeing two apparently equivalent example commands.

Call `browser-search` or `local-search` directly only for diagnostics or when a
workflow explicitly needs one backend. A connected MCP registration does not
prove the current agent/subagent turn has MCP tools; prefer MCP only when
`search_wiki` is actually exposed in that turn.

## Local Bounded Search (retrieval fallback tier)

```bash
wiki_ops.py local-search /path/to/wiki --q "检索词" --top 8
wiki_ops.py local-search /path/to/wiki --q "检索词" --max-file-chars 8000
```

`local-search` is the **last tier** of the retrieval chain (Browser MCP →
`browser-search` → this). Pure stdlib, no Browser required — it exists so every
SOP stays fully usable without the optional Browser, just slightly more
expensive. Bounded by design: scans at most `--max-file-chars` per page for
scoring, returns top-k snippet-sized hits (`backend: "local"`, same `hits[]`
shape as `browser-search`), and never returns whole files. Title hits weigh
5x body occurrences; an exact/prefix whole-query title match pins to the top.
`index.md`/`log.md`/`overview.md` are excluded.

## Read Pages (budgeted context packing)

```bash
wiki_ops.py read-pages /path/to/wiki --paths "wiki/concepts/a.md,wiki/entities/b.md"
wiki_ops.py read-pages /path/to/wiki --paths-file /tmp/cands.json \
  --max-pages 5 --max-chars-per-page 6000 --max-total-chars 24000
```

`read-pages` packs search candidates into a bounded context — the local mirror
of the Browser MCP `read_pages` tool (same budgets, same semantics). At most
`--max-pages` pages (clamped to 20); each body truncated to
`--max-chars-per-page` (`truncated: true` when clipped); the whole result capped
at `--max-total-chars`. Output: `{budget, totalChars, pages[], missing[],
omitted[]}` — `missing` are paths with no file, `omitted` are paths dropped by
the page/total budget (so the caller knows what was left unread). Prefer
`--paths-file` (JSON array or one path per line) for CJK-heavy lists. Use this
instead of `cat` for reading wiki pages during ingest/query.
