# Project Protocol

## Root Detection

Resolve one project root for every operation.

- If given a path under `raw/sources/`, the project root is the directory above `raw/`.
- If given a path under `wiki/`, the project root is the directory above `wiki/`.
- If given a directory, accept it only when it contains `purpose.md`, `schema.md`, and `wiki/`.
- If multiple roots are possible, stop and ask for the intended root.

All writes must stay inside the resolved project root.

## Standard Tree

```text
project/
├── purpose.md
├── schema.md
├── raw/
│   ├── sources/
│   └── assets/
├── wiki/
│   ├── index.md
│   ├── log.md
│   ├── overview.md
│   ├── entities/
│   ├── concepts/
│   ├── sources/
│   ├── queries/
│   ├── synthesis/
│   └── comparisons/
├── .llm-wiki/
│   ├── review.json
│   ├── lint.json
│   ├── dedup-not-duplicates.json
│   └── agent/
│       ├── ingest-cache.json
│       ├── token-trace.jsonl
│       ├── research/
│       │   ├── queue.json
│       │   └── runs/
│       ├── ingest-staging/
│       │   └── <source-slug>/
│       │       ├── manifest.json
│       │       ├── chunks/
│       │       └── map/
│       ├── page-history/
│       └── runs/
└── .obsidian/
    ├── app.json
    └── appearance.json
```

## Page Contract

Content pages must include YAML frontmatter:

```yaml
---
type: entity | concept | source | query | comparison | synthesis | overview
title: Human-readable title
tags: [topical-word, another-topical-word]
related: []
created: YYYY-MM-DD
updated: YYYY-MM-DD
sources: []
---
```

`tags` must carry real topical words drawn from the page's subject matter (≤5,
each reusable across ≥2 pages) — see `schema.md`'s Tag & Domain Policy. An
empty `tags: []` on a content page is a defect that `apply-blocks` and `lint`
both warn about; `wiki/overview.md` is the sole exception, carrying exactly
`type`/`title`/`created`/`updated` and no `tags` key.

## Link Convention

Two consumers resolve links differently. Get both right or links render broken in the app.

- **Body wikilinks** — use full-path `[[folder/slug]]`, e.g. `[[concepts/typo-seo]]`, `[[entities/野生小虎]]`. The app indexes pages by both full relative path and basename, so these resolve; the path also reads clearly. Aliases are fine: `[[concepts/typo-seo|错别字流量]]`.
- **`related:` frontmatter** — use **bare basename slugs only**: `related: [野生小虎, typo-seo]`. **Never** put `[[...]]`, a folder prefix, or `.md` here.

Why `related:` differs: the app's `resolveRelatedSlug` treats any value containing `/` as a project-relative *path* and looks it up verbatim. A path-style wikilink `[[entities/野生小虎]]` unwraps to `entities/野生小虎`, which the app resolves as `<project>/entities/野生小虎` — missing the `wiki/` prefix and `.md`, so it never matches and the Related chip shows unresolved. Only bare slugs (`野生小虎`), `野生小虎.md`, or a full `wiki/entities/野生小虎.md` path resolve. Bare slug is the simplest correct shape.

Do not hand-normalize: `wiki_ops.py apply-blocks` and `merge-page` rewrite `related:` to bare slugs automatically. The rule above is so generated blocks are correct *before* the script runs and so humans editing pages stay consistent.

**Slug language**: a page's filename/slug follows the source's own language, never a fixed one — derive it from the title, keeping CJK characters readable (`产品留存作为seo信号`), Latin titles kebab-case. The wikilink targets above then use that same-language slug. See `references/ingest-update.md` → Output Language & Filenames.

## Operational State

- `.llm-wiki/review.json`: App-compatible per-project review items only.
- `.llm-wiki/lint.json`: App-compatible latest lint findings.
- `.llm-wiki/dedup-not-duplicates.json`: App-compatible "not a duplicate" whitelist (array-of-arrays, each group lowercased + sorted). Shared two-way with the app's dedup tool.
- `.llm-wiki/agent/ingest-cache.json`: map source identity to hash, written files, timestamp.
- `.llm-wiki/agent/lint-state.json`: semantic-lint baseline — `lastLintAt` + per-page content hashes, written by `lint-scope --mark`. Skill-only because App-compatible `lint.json` is an array of issues and cannot carry this bookkeeping.
- `.llm-wiki/agent/token-trace.jsonl`: one JSON object per LLM call or script-estimated expensive step.
- `.llm-wiki/agent/research/queue.json`: queued/running research tasks if a task is interrupted.
- `.llm-wiki/agent/research/runs/*.json`: `producer` (the retrieval skill/MCP that gathered evidence), `domain`, search queries, results, synthesis path, errors. `producer`/`domain` make a run reproducible — see `references/review-research.md` → Evidence Contract.
- `.llm-wiki/agent/ingest-staging/<source-slug>/`: per-source map-reduce scratch for large-source ingest — `manifest.json` (chunk ledger + raw hash + MAP status), `chunks/` (split text), `map/` (per-chunk extracted candidates). Regenerable from the raw source; safe to delete after a successful ingest. See `references/large-source-ingest.md`.
- `.llm-wiki/agent/page-history/`: backups before risky page merge fallback; each
  tag rewrite gets an isolated `tag-rewrite-<run-id>/` containing the backed-up
  page tree plus its apply/rollback `manifest.json` journal.

Do not store project state outside the project root except for temporary scratch files.

## Token Policy

Track expensive calls by scope. `.llm-wiki/agent/token-trace.jsonl` is written by
the skill in the project root, so it is the **only runtime-agnostic meter** — a
host's own accounting (e.g. Hermes' state.db) sees only that host, while the
trace makes before/after and cross-host comparison possible.

Analysis/synthesis steps record chars:

```json
{"scope":"ingest.analysis","projectRoot":"/abs/project","source":"raw/sources/a.md","inputChars":12000,"outputChars":3000,"createdAt":"2026-06-11T00:00:00Z"}
```

Retrieval steps record the backend and what was packed (all flags on
`wiki_ops.py trace`):

```json
{"scope":"query.retrieval","projectRoot":"/abs/project","backend":"browser","candidates":8,"pagesRead":4,"contextChars":21000,"createdAt":"2026-07-10T00:00:00Z"}
```

- `backend`: `mcp` | `browser` | `local` — which retrieval tier served the step.
- `candidates`: search hits considered; `pagesRead`: pages actually packed.
- `contextChars`: chars that entered the prompt context for this step.
- `promptTokens` / `cacheReadTokens`: exact counts when the host exposes them.

Use character counts when exact token usage is unavailable. The purpose is observability, not billing accuracy.

## Overview Policy

`wiki/overview.md` is a SHORT digest — a map above `index.md`, not an ingest aggregate. Do not regenerate it during ingest or update; **manual refresh only**, from `purpose.md`, `schema.md`, and a description-free `wiki/index.md` listing (`compact-index --no-desc`). Frontmatter is exactly 4 fields — `type`, `title`, `created`, `updated` (no tags/related/sources). Body: one-sentence intro + `## 主要板块` + `## 怎么用`, 5–10 lines, no page enumeration. Full SOP in `references/lint-query-save.md`.
