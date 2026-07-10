# Large-Source Ingest (MAP / REDUCE)

A very large raw source — a long report, a book-length article, a big PDF that the
capture skill (my-llm-wiki) ran through `markitdown` into one un-chunked `.md` —
cannot be ingested in a single pass. One `Read` either overflows context or
dilutes the middle ("lost in the middle"), so entities and concepts buried deep
in the document get dropped, and a recurring entity gets a separate page per
section. This SOP replaces the single-pass synthesis (Ingest steps 6–7 in
`ingest-update.md`) with a two-phase **MAP → REDUCE** flow for large sources only.
Small sources keep the single-pass path unchanged.

The split is deterministic-script vs LLM, exactly as the Hard Rules require: the
**gate** and **chunking** are scripted (`wiki_ops.py`), the **extraction** and
**synthesis** are LLM. The REDUCE output re-enters the normal pipeline at the
FILE/REVIEW-block boundary, so `source-page` / `apply-blocks` / `merge-index` /
`merge-page` / `cache save` / lint / sweep are all reused without change.

## 1. When this applies — the size gate

After resolving the root and reading `purpose.md` / `schema.md` / `wiki/index.md`
(Ingest steps 1–2), run the gate before extracting:

```bash
scripts/wiki_ops.py probe-source <root> --raw raw/sources/<...>.md
```

It strips frontmatter, counts BODY chars, estimates tokens (CJK-aware), and
returns `path: "small" | "large"`. Decision is on `bodyChars` vs `--threshold`
(default 40000); `estTokens` is observability only.

- `path == small` → return to `ingest-update.md` and run steps 6–12 as usual.
- `path == large` → follow this SOP (phases 2–5 below), then rejoin
  `ingest-update.md` at **step 8 (apply-blocks)**; steps 8–12 (verify, reviews,
  cache, sweep) are shared, not duplicated here.

## 2. Phase overview

```
probe-source ──large──▶ split-source ──▶ MAP each chunk ──▶ REDUCE ──▶ apply-blocks
   (gate)              (deterministic)     (LLM, per chunk)   (LLM)     (existing pipeline)
                                                │                │
                                          map/*.json       dedup + FILE/REVIEW blocks
```

## 3. Split (deterministic)

```bash
scripts/wiki_ops.py split-source <root> --raw raw/sources/<...>.md \
    [--url <url>] [--chunk-chars 12000] [--overlap 600] --write
```

Chunks the frontmatter-stripped body along H1–H3 boundaries into pieces under the
char budget (oversized headingless blocks fall back to fence-safe char windows),
and writes staging under the SAME stable slug the source page will use:

```
.llm-wiki/agent/ingest-staging/<source-slug>/
├── manifest.json            # rawHash, chunkChars, overlap, chunkCount, chunks[]
├── chunks/chunk-NNN.md      # the chunk text you MAP
└── map/chunk-NNN.json       # you write the MAP result here
```

Each `manifest.chunks[]` entry carries: `index`, `file`, `mapFile`, `breadcrumb`
(its `H1 > H2 > H3` path), `headingLevel`, `part` (window index, or null),
`chars`, `charStart`/`charEnd`, `textHash`, `mapStatus` (`pending`/`done`),
`mapHash`. Without `--write` it is a dry run (prints the manifest only).

The **breadcrumb** tells the MAP step where each chunk sits in the document — use
it so an extracted claim is anchored to its section, and so the REDUCE summary can
reconstruct the document's structure.

## 4. MAP phase (LLM, per chunk → structured candidates)

MAP runs **index-free** — do not load the index (full or compact) into the MAP
prompts; on a grown wiki that re-bills an O(wiki) block once per chunk. Extract
candidates from the chunk text alone and leave `linksToExisting` empty unless
you already know the target; REDUCE re-checks existing pages with bounded
retrieval anyway.

For each chunk where `stage-status` reports it pending (phase 6), read
`chunks/chunk-NNN.md` and **extract candidates only — do NOT write wiki pages
yet**. Write the result as JSON to `map/chunk-NNN.json` (the staging contract):

```json
{
  "chunkIndex": 7,
  "breadcrumb": "第3章 市场结构 > 3.2 竞争格局",
  "entities": [
    {"name": "野生小虎", "type": "entity", "salience": "high",
     "facts": ["fact grounded in this chunk", "..."],
     "evidence": "short quote or paraphrase",
     "linksToExisting": ["entities/某公司"], "aliases": ["小虎"]}
  ],
  "concepts": [
    {"name": "产品留存作为seo信号", "type": "concept", "salience": "med",
     "claim": "the concept as argued here", "evidence": "...",
     "linksToExisting": ["concepts/typo-seo"]}
  ],
  "keyClaims": [{"claim": "...", "evidence": "...", "strength": "strong|weak"}],
  "connections": [{"from": "野生小虎", "to": "concepts/typo-seo", "why": "..."}],
  "contradictions": [{"with": "concepts/x", "tension": "..."}]
}
```

MAP rules:

- **Extract, don't synthesize.** Names + facts + evidence + links, scoped to THIS
  chunk. No prose pages, no index edits.
- `salience`: `high` = clearly page-worthy; `med` = notable, may merge into another
  page; `low` = mention only (becomes a review item later, never silently dropped).
- `linksToExisting` references existing pages by `folder/slug` when already
  known (e.g. from an earlier bounded search). If unsure, leave it out —
  REDUCE re-checks with per-name retrieval.
- `name` follows the **source's own language** (keep CJK as-is) — these become
  page titles/slugs downstream, which must match the source language (see
  `ingest-update.md` → Output Language & Filenames).
- The map file MUST be valid JSON. After writing it, mark the chunk done:

```bash
scripts/wiki_ops.py stage-status <root> --source raw/sources/<...>.md --mark-done 7
```

`--mark-done` refuses unless the map file exists and parses (conservative
failure), records the chunk-text hash, and flips `mapStatus`.

Chunks are independent — MAP them in any order, and resume freely (phase 6). To
process several at once, spawn parallel sub-agents, **one chunk per agent**; each
writes its own `map/chunk-NNN.json`, so there is no shared-state contention (unlike
ingesting whole separate sources in parallel, which is unsafe — see SKILL.md).

## 5. REDUCE phase (LLM merge → FILE/REVIEW blocks)

Only start when `stage-status` reports `ready: true` (`pending == 0 && stale == 0`).

1. Read ALL `map/chunk-NNN.json`. They are far smaller than the prose, so the full
   candidate set fits in one context even for a 100-page report. (If it genuinely
   does not, reduce hierarchically: merge per top-level H1 group first, then merge
   the group summaries.)
2. **Dedup across chunks.** Collapse an entity/concept that recurs across chunks
   (e.g. `野生小虎` in chunks 2, 9, 17) into ONE candidate: union `facts`,
   `evidence`, `linksToExisting`, `aliases`; keep the highest `salience`. This is
   the cross-section dedup a single pass cannot do.
3. **Check candidates against the existing wiki with bounded retrieval** — the
   deduped candidate names are exactly the "extracted names" of the ingest
   retrieval discipline (`ingest-update.md` step 5): per name, one
   `browser-search`/`local-search --top 8` plus the disk grep sentinel
   (`grep -F "[[entities/<name>" wiki/index.md`), then `read-pages` the top
   3–5 pages that overlap. Never load the whole index to "see what exists".
4. Run source identity first (dedup one source → one page):

```bash
scripts/wiki_ops.py source-page <root> --raw raw/sources/<...>.md [--url <url>]
```

   If it returns `existing`, merge into that page (`merge-page`) instead of
   creating a new one.
5. Emit FILE/REVIEW blocks using the EXISTING output contract (see
   `output-blocks.md` and `ingest-update.md`):
   - `wiki/sources/<slug>.md` — a **structured multi-section** summary (template
     below), NOT a one-paragraph blurb.
   - One content page per deduped `high`/`med`-salience entity/concept.
   - `wiki/index.md` — delta entries only.
   - `wiki/log.md` — one append-only line noting two-phase ingest + chunk count.
   - REVIEW blocks for: contradictions, `low`-salience-but-notable candidates, and
     any candidate NOT promoted to a page — conservative failure, nothing dropped.
6. Apply and verify exactly as `ingest-update.md` steps 8–12: `apply-blocks`
   (every FILE in `written[]`, `warnings` empty), index delta merge, then
   `cache save` listing every written page. Run the coverage gate (phase 7) and
   `lint` before saving cache.

### Structured source-page template (large sources)

Body is richer than a small source's; frontmatter and link conventions are
unchanged (`type: source`, body `[[folder/slug]]`, `related:` bare slugs, filename
from the `source-page` slug). Section headings follow the source's language:

```
# <报告标题>

> 概要：one-paragraph executive summary of the whole document.

## 核心要点
- 要点，带 [[entities/…]] / [[concepts/…]] 链接

## 章节结构
- 第1章 …：one line on what it covers
- 第2章 …：…

## 关键实体与概念
- [[entities/野生小虎]] — 在报告中的角色
- [[concepts/产品留存作为seo信号]] — …

## 证据与论点
- 论点 → 证据强度

## 待跟进 / 矛盾
- 与 [[concepts/x]] 的张力，或开放问题（与所提 REVIEW 块呼应）
```

## 6. Resumability

An interrupted large ingest resumes without redoing work:

```bash
scripts/wiki_ops.py split-source <root> --raw <...> --write   # same hash → "reused", keeps MAP progress
scripts/wiki_ops.py stage-status <root> --source <...>        # lists pendingIndices / staleIndices
# → MAP only the pending/stale chunks, --mark-done each
# → REDUCE when ready:true
```

- `split-source` compares the raw file hash to `manifest.rawHash`: same hash reuses
  staging (and `mapStatus`); a changed hash (source re-captured) re-splits and
  resets all chunks to pending; `--force` wipes staging and starts clean.
- A chunk marked done is `stale` if its map file went missing or its chunk text
  changed — `stage-status` surfaces it and REDUCE stays blocked until it is re-MAPped.

Staging is disposable: it lives under `.llm-wiki/agent/`, is regenerable from the
raw source, and is safe to delete after a successful ingest.

## 7. Quality gates (before saving cache)

1. **apply-blocks verification** (existing): every emitted FILE appears in
   `written[]` and `warnings` is empty, or fix and re-apply before caching.
2. **Coverage gate** (the "nothing in the middle is lost" guarantee): build the
   candidate-name set from every `map/chunk-NNN.json`. For each candidate, confirm
   it resolves to EITHER a written page (check `build_page_index` via a quick
   `lint`/index scan) OR an open review item. Any candidate with neither → raise a
   REVIEW block before finishing.
3. **lint** (existing): `scripts/wiki_ops.py lint <root>` for broken links / orphans.
4. **MAP completeness**: never start REDUCE while any chunk is pending/stale
   (`stage-status` → `ready` must be true) — guards against a thin summary built
   from a half-mapped document.

## 8. Worked example (abridged)

```bash
R=/repo/my-wiki ; S=raw/sources/note/2026-06-14-行业长报告.md
scripts/wiki_ops.py probe-source $R --raw $S
# → {"bodyChars":210000,"path":"large",...}
scripts/wiki_ops.py split-source $R --raw $S --url https://x/report --write
# → {"status":"written","chunkCount":18,"sourceSlug":"行业长报告"}
# MAP each of 18 chunks (index-free) → map/chunk-NNN.json ; --mark-done NNN each
scripts/wiki_ops.py stage-status $R --source $S           # → "ready": true
# REDUCE: read all map/*.json, dedup (e.g. 野生小虎 in chunks 2/9/17 → one page)
scripts/wiki_ops.py browser-search $R --q "野生小虎" --top 8   # per deduped name (fallback: local-search)
scripts/wiki_ops.py source-page $R --raw $S --url https://x/report
scripts/wiki_ops.py apply-blocks $R --blocks-file /tmp/blocks.txt --source $S
scripts/wiki_ops.py lint $R
scripts/wiki_ops.py cache save $R $R/$S --files-file /tmp/written-pages.json
```

Expected: the recurring entity has exactly ONE page (cross-chunk dedup), the
contradiction produced a REVIEW item, every map candidate maps to a page or a
review (coverage gate), and the source page is multi-section.
