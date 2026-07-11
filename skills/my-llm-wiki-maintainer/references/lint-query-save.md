# Lint, Query, Save, Overview SOP

## Lint

Structural lint should be script-first:

- Missing or invalid frontmatter.
- `type` mismatches folder/schema.
- Missing `sources[]` on content pages.
- Broken `[[wikilinks]]`.
- Orphan pages with no inbound links.
- Pages with no outbound wikilinks.
- `index.md` missing an existing page.
- `index.md` points to a missing page.
- Duplicate basenames/slugs.
- Review starvation (info): 5+ ingests since the last review item with zero review
  output — the deep-research queue stopped filling (usually a workflow doc or model
  dropping REVIEW generation). Fix the ingest habit, not the symptom.
- Missing raw (info): a `sources:` entry names a raw `.md` that doesn't exist
  (compared by basename) — the page's claims can't be re-checked against the
  original. Typical causes: research evidence never persisted, a raw rename, or a
  model writing a wiki-page-style name instead of the actual raw filename.

Semantic lint is LLM-assisted:

- Contradictions across pages.
- Stale claims superseded by newer sources.
- Duplicate concepts under different names.
- Important concepts mentioned often but lacking pages.
- Source summaries too thin to support later query.

Auto-fix formatting and index omissions. Put judgment-heavy issues into review items.

### Incremental semantic lint (default)

Full-wiki semantic lint costs LLM tokens proportional to wiki size, so it silently
stops being run — which is exactly how "today's page contradicts last week's and
nobody notices" happens. Scope it instead:

1. `wiki_ops.py lint-scope <root>` — returns pages changed since the last marked
   pass plus their one-hop neighbors (`scope[]`), and separately `deleted[]`
   (check the index and inbound links of deleted pages by hand).
2. Run the semantic checks above on `scope[]` only, comparing changed pages
   against their listed neighbors. `firstRun: true` legitimately means the whole
   wiki — that's the baseline pass.
3. After the pass completes (issues filed to review items), record the baseline:
   `wiki_ops.py lint-scope <root> --mark`. Never `--mark` without having run the pass.

Go full-wiki only when the user explicitly asks for a complete audit.

**When to run** (pick what the environment supports; any is better than none):

- **After a flush**: a batch ingest draining is the contradiction-prone moment —
  run a scoped semantic lint as the batch's last step (see SKILL.md → Flush pending).
- **Scheduled**: a Claude Code `/loop`, system cron, or the desktop App's scheduled
  task calling "lint this wiki" periodically; structural lint's `--exit-code` gate
  already supports CI-style wiring.

## Query

Answer from the compiled wiki, not raw sources, unless the user explicitly asks for raw-source inspection.

> **Trigger surface note.** The user-facing read-only query entrance (“查一下知识库”,
> “wiki 里有没有…”) belongs to the sibling **my-llm-wiki-search** skill, which embeds
> this same recipe (same `wiki_ops.py` backend, same budgets). This SOP stays here
> because maintainer operations (save, research, dedup) query internally — and so
> the maintainer remains fully usable where the search skill isn't installed.

1. Resolve project root.
2. Read `purpose.md` (O(1)). Do **not** read `wiki/index.md` — full or compact —
   into context; candidates come from bounded retrieval below.
3. Retrieve candidate pages — cheapest connected tier first, with a
   deterministic silent fallback:
   - **Browser MCP** (when the host has it connected): `search_wiki`
     (default top 8) — use it only when `search_wiki` is actually exposed to
     the current turn; registration in host config alone is not sufficient.
   - **Runtime-neutral CLI**:
     `wiki_ops.py retrieval-search <root> --q "<keywords>" --top 8` — one
     command that tries the Browser HTTP index first and runs the bounded local
     fallback only when Browser is unavailable. Its returned `backend` is
     authoritative for tracing. Do not select `local-search` directly during
     normal query work. Run 1–2 keyword variants if the first query misses.
4. Expand through graph signals where cheap:
   - direct wikilinks;
   - shared `sources[]`;
   - common neighbors (`wiki_ops.py neighbors <root> --page <hit> --max 8`);
   - type affinity.
5. Pack context under a hard budget — read at most 3–5 pages:
   `wiki_ops.py read-pages <root> --paths ... --max-pages 5 --max-chars-per-page 6000 --max-total-chars 24000`
   (or the Browser MCP `read_pages` tool — same budgets). Never cat whole files
   or paste the index as "context".
6. Number pages in the prompt.
7. Answer with page citations like `[1]` and wiki links like `[[concept-slug]]`.
8. If evidence is insufficient, say what is missing and optionally create a review suggestion.
9. Record the retrieval trace:
   `wiki_ops.py trace <root> query.retrieval --backend <mcp|browser|local> --candidates <hits> --pages-read <n> --context-chars <chars>`.

## Save

Save high-value answers so exploration compounds.

1. Clean hidden citation comments and thinking blocks.
2. Create `wiki/queries/<date-time-slug>.md`.
3. Add frontmatter with `type: query`.
4. Merge a query entry into `wiki/index.md`.
5. Append `wiki/log.md`.
6. Ingest the saved query page so it can update concepts/entities/synthesis and create reviews.

## Refresh Overview (digest)

`wiki/overview.md` is a SHORT digest — a map that sits ABOVE `index.md`, not an aggregate of it. MANUAL only: never regenerate it during ingest/update.

1. Read `purpose.md`, `schema.md`, and a **description-free** index listing:
   `scripts/wiki_ops.py compact-index <root> --no-desc` (keeps `## Section` headings + page links, drops the ` — description` tail).
2. Do NOT read the old `overview.md` — avoid copying bloat forward.
3. Ask the LLM for a concise digest **in the wiki's own language** (whatever the sources/index are written in — never force a fixed language). All headings and prose below are localized to that language; the Chinese forms are just examples:
   - One sentence: what this wiki is about.
   - A "main sections" heading (e.g. `## 主要板块`): group the sections (实体/概念/来源/查询 …) into a few themes, ONE short line each — summarize at the theme level.
   - A "how to use" heading (e.g. `## 怎么用`): read `index.md` first to find pages, then drill in; history is in `log.md`.
   - Hard limits: 5–10 body lines, under ~300 words. Do NOT list individual pages or paste wikilinks — `index.md` already does that.
4. Frontmatter is EXACTLY these 4 fields, nothing else: `type: overview`, `title` (in the wiki's language, e.g. `项目总览` / `Overview`), `created`, `updated` (both today). **No tags/related/sources.**
5. Write `wiki/overview.md` only on non-empty output; leave the old file untouched on LLM error.

This matches the app's `buildOverviewDigestPrompt` (`src/lib/overview-refresh.ts`) so a skill-generated and an app-generated overview look the same.
