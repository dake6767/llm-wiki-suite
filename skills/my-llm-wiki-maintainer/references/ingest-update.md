# Ingest And Update SOP

## Ingest

Goal: compile one raw source into durable wiki pages while preserving traceability.

1. Resolve project root.
2. Read `purpose.md` and `schema.md` (both O(1); naming and classification rules
   live in `schema.md`). Do **NOT** read `wiki/index.md` into context — not full,
   not compact. Awareness of existing pages comes from the bounded retrieval
   working set (step 5) plus the zero-context disk grep sentinel; a full index in
   the prompt costs O(wiki) tokens on **every** subsequent model call in the
   session and is the single largest measured waste in maintainer sessions.
3. Extract source text and compute a hash.
4. Check `.llm-wiki/agent/ingest-cache.json`. If hash and written files are unchanged, skip full ingest.
5. Build the **retrieval working set** — O(top-k), never O(wiki):

   1. From the source's title and a skim of its body, extract the candidate
      entity/concept names this source touches (the same names steps 6–7 will
      decide pages for).
   2. **Per extracted name, one bounded search** — this retrieval discipline is
      the load-bearing rule. If the host actually exposes Browser MCP tools to
      this turn, use `search_wiki`. Otherwise run the single deterministic CLI
      entry point below; it always tries Browser HTTP first and falls back to
      local only when its JSON result says the Browser is unavailable:

      ```bash
      python3 scripts/wiki_ops.py retrieval-search <root> --q "<name>" --top 8
      ```

      Read the returned `backend` (`browser` or `local`) and use that exact
      value in the retrieval trace. Do not call `local-search` directly during
      normal ingest; it is a diagnostic/explicit fallback command. MCP being
      registered in host config is not sufficient evidence that the current
      turn can call it — use MCP only when `search_wiki` is actually present in
      the turn's tools.

      Fall back silently; the Browser is optional and every step here must work
      without it (just slightly more expensive). Do NOT compress this into one
      topic query per source: measured working-set recall drops from ~97% to
      ~28% (quality eval Test C) while saving only ~2KB of context.
   3. Pack the candidate pages under a hard budget — read at most 3–5 pages:

      ```bash
      python3 scripts/wiki_ops.py read-pages <root> --paths-file /tmp/cands.json \
        --max-pages 5 --max-chars-per-page 6000 --max-total-chars 24000
      ```

      (When the Browser MCP tools are exposed to the turn, `search_wiki` + `read_pages` are the
      same two operations as MCP tools — same backend, same budgets.)
   4. **Disk grep sentinel (keep it).** Before creating any new content page,
      check for an existing page with a disk grep — zero context cost:

      ```bash
      grep -F "[[entities/<name>" <root>/wiki/index.md
      ```

      The grep is a disk operation and complements the per-name search; what
      this SOP removed is only "read the index *into context*", never the grep.
   5. **Hard context budget.** Never put the full `wiki/index.md`, a full
      wiki tree listing, or the whole `review.json` into context during ingest.
      A `compact-index --no-desc` load is acceptable only for a small wiki
      (listing under ~10k chars); large wikis get their global pass in review
      batches instead (see `review-research.md` → Link Densification).
   6. **Read the tag vocabulary**, scoped to what this source is about — pass
      the same candidate names from substep 1:

      ```bash
      python3 scripts/wiki_ops.py tags <root> --q "<candidate names / topic>"
      ```

      Part of the working set on purpose, not a step-7 detail: it must land
      **before the size gate below**, or the large-source path (which replaces
      steps 6–7 and rejoins at step 8) would skip tagging guidance entirely.
      Bounded like the rest of this step — ~590 tokens on a 900-page wiki, the
      same on a small one. What you do with it is in step 7.
   7. Record the retrieval trace so before/after comparisons stay possible on
      any host:

      ```bash
      python3 scripts/wiki_ops.py trace <root> ingest.retrieval --backend browser \
        --candidates 8 --pages-read 4 --context-chars 21000
      ```

   **Size gate (large sources).** Before analyzing, run
   `scripts/wiki_ops.py probe-source <root> --raw <source>`. If it returns
   `path: "large"` (body chars ≥ threshold, default 40000), do NOT do single-pass
   synthesis — switch to the two-phase MAP/REDUCE flow in
   `references/large-source-ingest.md`, which chunks the source, extracts
   candidates per chunk, dedupes across chunks, and emits FILE/REVIEW blocks. It
   rejoins THIS SOP at step 8 (apply-blocks), so steps 8–12 below are shared. For
   `path: "small"`, continue with steps 6–12 as written.

6. Run analysis:
   - Key entities and concepts.
   - Main arguments and evidence strength.
   - Connections to existing pages (from the step-5 working set; the conflict
     sentinel's `neighbors` expansion below catches one hop further).
   - Contradictions or tensions with the working-set pages. Cross-cluster
     contradictions that top-k retrieval cannot see are the review queue's job —
     when in doubt file a review item, do not re-read the wiki wholesale.
   - Pages to create/update and review gaps.
7. Generate FILE/REVIEW blocks. Require:
   - Source summary at `wiki/sources/<source-slug>.md`. Give it a **short, readable slug in the source's language** (3–6 words from the title/topic, e.g. `野生小虎-出海seo小游戏案例`) — do NOT dump the whole raw filename. First run `scripts/wiki_ops.py source-page` for dedup (see Source Identity below).
   - Content pages for important entities/concepts/schema-defined types.
   - `wiki/index.md` as delta entries only, not a full rewrite.
   - `wiki/log.md` as an append-only entry.
   - No `wiki/overview.md` block.
   - Links must follow the Link Convention in `project-protocol.md`: body uses `[[folder/slug]]`, `related:` uses bare basename slugs. `apply-blocks` normalizes `related:` anyway, but emit it correctly.
   - **Real `tags` on every page you emit — filling the template's `tags: []` is
     part of generating the block, not a later cleanup.** Draw ≤5 topical words
     from the page's actual subject matter per `schema.md`'s Tag & Domain Policy.
     Two traps, both observed in real ingests: leaving the placeholder empty (the
     page lands with no retrieval facet at all — `related:` full, `tags: []`), and
     echoing the RAW capture frontmatter you just read (`inbox` is never
     acceptable; a format word like `video`/`bilibili` is fine alongside real tags
     but never as the whole set). `apply-blocks` and `lint` both warn on an empty
     set, but the warning arrives after the page is written — get it right here.
   - **Use the tag vocabulary you read in step 5.6** (re-run it there if this is
     the large-source or video path rejoining here):

     ```bash
     python3 scripts/wiki_ops.py tags <root> --q "<candidate names / topic>"
     ```

     Prefer an established tag that fits over coining a new one; coin a new tag
     only when nothing in the list covers the page. This is what makes the
     schema's "reusable across ≥2 pages" rule satisfiable at all — without
     reading the vocabulary you are guessing blind, and every session's guesses
     fragment the facet (`大模型` / `LLM` / `大语言模型` all arrived this way,
     from three separate ingests of one subject). Your new tags become the
     next ingest's vocabulary — that feedback loop is the whole mechanism, so a
     sloppy tag here costs more than this page.

     **Pass `--q`.** Tags marked `*` are used on exactly one page so far, and
     they only appear in the scoped view — the unscoped backbone lists
     established (≥2 page) tags, so without `--q` a tag coined by the previous
     ingest is invisible, never gets reused, and can never reach 2. The scoped
     view is what lets the vocabulary actually grow rather than just recycle
     whatever was already popular.

     Bounded either way (~590 tokens on a 900-page wiki, the same on a 20-page
     one), so this does not violate the step-5 budget. **Never pass `--audit`
     during ingest** — it adds every singleton, duplicate pair and untagged
     page, which is 16KB of cleanup material on a large wiki and useless for
     tagging one page.
   - **REVIEW blocks are part of the deliverable, not an optional extra.** For every
     source, explicitly decide 0–3 `suggestion` items — the research gaps this source
     opens: claims worth verifying, adjacent topics the wiki lacks, tensions with
     existing pages — each with 2–3 ready-to-run `searchQueries`. Zero suggestions is
     a legitimate call for a thin source, but it must be a decision, not a forgotten
     step: review generation silently dropping off is how the deep-research loop
     starves (the queue stops filling, so the user is never offered a direction).

## Source Identity (dedup)

One raw source must map to exactly one `wiki/sources/` page. Two pages for one source (e.g. a clean slug plus an auto-slugged filename) split the graph and double-count.

Before writing the source summary, run:

```text
scripts/wiki_ops.py source-page <project_root> --raw <raw/sources/...> [--url <url>]
```

It scans existing source pages for one already pointing at this raw file (by `sources:` basename) or the same `url:`, and returns JSON:

- `existing` — a wiki-relative path if a wiki page already covers this source. If set, **merge into it** (via `merge-page`); do not create a new page.
- `slug` / `page` — a **fallback** slug derived from the raw filename (date prefix stripped). Prefer your own short readable slug over this; the fallback is only for when you don't supply one.

Dedup does NOT depend on the slug being stable — `existing` is matched by frontmatter `sources`/`url`, so a short hand-written slug re-ingests to the same page just fine. That's why you're free to name the source page concisely (`野生小虎-出海seo小游戏案例`) instead of echoing the raw filename.

## Ingesting listicle / recommendation-heavy sources

When the source is a long listicle that names many specific entities (X handles, books, YouTube channels, podcasts, blogs — anything with a proper name and a 1-line blurb), **do not create entity stubs for every name**. That explodes the graph with shallow pages and drowns the genuinely-canonical entities.

Pattern that works:

- **Always** create 1 source page and the concept page(s) for the article's actual framework or thesis (e.g. "9-category information-source framework", "input > output for AI content creation"). Those are the durable ideas.
- **Sometimes** create entity pages for canonical, distinctive names the wiki would genuinely cross-link to from future sources (e.g. a foundational researcher's homepage, a single canonical aggregator everyone cites). Skip the long tail.
- **Default** to **one synthesis page** that consolidates the list — the full X-handles list, the full books list, the full YouTube list per domain. This is queryable, easy to extend on the next listicle, and keeps the entity graph focused.

Existing-entity check: before creating any entity page, grep `wiki/index.md` for `[[entities/<name>`. If the canonical name is already there, don't duplicate — reference it from the synthesis page instead.

When in doubt, the synthesis-page-consolidation pattern beats N stub entity pages almost every time. The RAW source remains a faithful record of every name the article mentioned; the synthesis page is the curated, queryable index, not the only place those names exist.

## Cross-Linking

Pages must connect across types, not only within a type. When ingesting a source whose entities and concepts come from one another:

- Each **entity** page body links the concepts it exemplifies (and the source): an entity that only links other entities sits at the graph periphery.
- Each **concept** page body links the entity/source it is grounded in, plus adjacent concepts.
- Put the same neighbors in `related:` (bare slugs) so the relation panel mirrors the body links.

## Conflict Sentinel (between generating and applying blocks)

Step 6's contradiction check sees only the compact index; this sentinel catches what
that can't — direct conflicts with the **content** of pages one hop away, which is
where "today's summary contradicts last week's and nobody notices" actually lives.
Run it after generating blocks (step 7), before applying them (step 8), for each
content page the ingest updates or heavily links:

1. **Gather the neighborhood** (deterministic):
   ```bash
   # existing page being updated/merged:
   python3 scripts/wiki_ops.py neighbors <root> --page wiki/concepts/x.md --max 12
   # new page — pass the wikilink targets from its generated block:
   python3 scripts/wiki_ops.py neighbors <root> --slugs "harness,skill是能力商品" --max 12
   ```
2. **Compare** the new block against the returned `neighbors[].file` (ranked
   closest-first; trim the list before trimming per-page content when budget is
   tight). **Direct conflicts only**: the same claim with opposite conclusions, or
   mutually exclusive facts (dates, numbers, "X causes Y" vs "X doesn't"). Different
   emphasis, scope, or style is NOT a conflict. When unsure, it is not a conflict —
   review noise destroys review trust.
3. **Never block the write.** RAW facts win: apply the blocks as planned regardless
   of conflicts. For each real conflict, emit ONE review block (lands in step 10):
   - `type: contradiction`;
   - `affectedPages`: all conflicting pages;
   - description: one line per side quoting the conflicting claim, each tagged with
     its source and `captured_at` — newer is not automatically right, but the
     reviewer needs the timeline to judge.
4. Scope note: the sentinel runs per source as it lands; the cross-source semantic
   lint sweep still owns what per-source checks can't see.

8. Apply blocks using `scripts/wiki_ops.py apply-blocks`:
   ```bash
   # New pages only (no existing pages):
   python3 scripts/wiki_ops.py apply-blocks <root> --blocks-file /tmp/blocks.txt --source <raw/source/path>
   # Existing content pages (backs up old, then overwrites):
   python3 scripts/wiki_ops.py apply-blocks <root> --blocks-file /tmp/blocks.txt --overwrite --source <raw/source/path>
   ```

   **Always pass `--source <raw/source/path>`.** Whichever pass carries the REVIEW
   blocks (step 10) threads this value into each persisted review as `sourcePath`.
   Omit it and the reviews land with `sourcePath: null` — silently, since `apply-blocks`
   still succeeds — and the Browser can no longer trace a suggestion back to its raw source.

   **New vs existing pages — two-pass pattern.** `apply-blocks` without `--overwrite`
   **skips** any content page that already exists (prints `"Skipped existing content page without --overwrite"`).
   When an ingest creates new pages AND updates existing ones, split into two passes:
   1. First pass (no `--overwrite`): writes new pages, index delta, log.
   2. Second pass (`--overwrite`): writes updated/merged pages (backs up old content first).

   Alternatively, use `merge-page` for intelligent merge of a single existing page:
   ```bash
   python3 scripts/wiki_ops.py merge-page <root> <existing-page-path> --incoming-file /tmp/merge-blocks.txt --write
   ```
   Note: the flag is `--incoming-file`, NOT `--blocks-file`. Requires `--write` to persist.
   This unions `sources`, `tags`, `related` and calls the LLM to merge bodies. Prefer
   when the page has substantial existing content you want to preserve alongside new additions.
   Use `--overwrite` (with LLM-generated merged content already in the block) when you've
   written the merged body yourself.

   **Pitfall: `merge-page` may write `---FILE:` wrapper into output.** Observed
   behavior: `merge-page` can leave the `---FILE: wiki/concepts/...md---` wrapper
   line at the top of the actual file. After merge-page, verify the output file
   starts with `---` (YAML frontmatter), not `---FILE:`. If malformed, strip the
   wrapper: `sed -i '' '1{/^---FILE:/d;}' <file>` and any trailing
   `---END FILE---` line.

   **Pitfall: `apply-blocks` is flag-based.** The blocks file is `--blocks-file`
   (or `--items` / `--file`), NOT a positional argument. Forgetting the flag gives
   "unrecognized arguments".

   **Pitfall: FILE block headers must include `.md` suffix.** Every `---FILE:` header
   must end with `.md` (e.g. `---FILE: wiki/entities/openspec.md---`). Omitting the
   suffix (e.g. `---FILE: wiki/entities/openspec---`) causes `apply-blocks` to emit
   a warning per affected block ("FILE path missing .md suffix") even though it
   auto-corrects and writes to the right path. The warnings are noisy and can obscure
   real problems during verification (step 9). Always write the full filename including
   `.md` in the block header.

9. Verify the apply result before moving on: every FILE path you emitted must
   appear in `written[]`, and `warnings` must be empty. A non-empty `warnings`
   (e.g. "not closed with ---END FILE---", "Skipped existing content page") means
   a block was malformed, recovered, or skipped — inspect the affected page for
   stray content or a missing write, fix it, and re-apply. Do not save cache on a
   block that didn't land cleanly. (A common cause: an editor/linter strips the
   trailing `---END FILE---` from the temp blocks file after you write it.)
10. Add review blocks to `.llm-wiki/review.json`. They ride in the step-8 blocks
    file (persisted by `apply-blocks`) or go through `review add-blocks` — either way
    **pass `--source <raw/source/path>`** so each review gets its `sourcePath`, and give
    every REVIEW block a `PAGES:` line so `affectedPages` is populated. A review with
    neither is orphaned: the Browser can't link it to a source or a page.
11. Save cache only after successful, verified writes:
    ```bash
    python3 scripts/wiki_ops.py cache save <root> "<raw/source/path>" \
      --files-file /tmp/written-pages.json
    python3 scripts/wiki_ops.py cache check <root> "<raw/source/path>"
    ```

    **Pitfall: cache save with CJK paths in shell args can hang silently.** Write
    the affected page paths to `written-pages.json` as a JSON array using the
    runtime's normal file-write tool. `--files-file` keeps those dynamic paths
    out of shell code, and the save command prints the persisted
    `filesWritten`. Require that list to match, then require `cache check` to
    report `hit: true`; silent success is worse than a loud failure.
12. Run review sweep after a batch drains.

## Output Language & Filenames

The whole ingest output follows the **source content's own language** — titles, body, filenames, index descriptions, review text. Do not translate to English, or to any fixed language; match what the reader of the source reads. (This is the agent-side equivalent of the app's mandatory output-language directive — there is no hardcoded language anywhere.)

- **Filenames**: derive each page's filename from its title, in that same language. For CJK (中文/日本語/한국어) titles keep the readable characters as-is — never transliterate or translate the slug to English: `wiki/concepts/产品留存作为seo信号.md`, not `wiki/concepts/product-retention-as-seo-signal.md`. Latin-script titles use kebab-case as before.
- **Body wikilinks** then reference those same-language slugs: `[[concepts/产品留存作为seo信号]]`.
- **`related:`** uses the bare basename of that slug: `related: [产品留存作为seo信号]`.
- **Source pages**: take the slug from `source-page`, which derives it from the raw filename — that already carries the source language.

This mirrors the app's ingest rule (filenames from the title in the output language; keep CJK) so app- and skill-generated pages look the same. CJK filenames are fully supported across the toolchain (lint, dedup, wikilink/related resolution).

## Index Delta Rule

The LLM should emit only new or changed index entries:

```text
---FILE: wiki/index.md---
## Concepts
- [[concepts/example|Example]] — one-line description
---END FILE---
```

The script merges by normalized wikilink target. Existing entries not mentioned remain untouched.

**Pitfall: do NOT append "delta" or "append" to index/log FILE paths.** The block header must be exactly `---FILE: wiki/index.md---` and `---FILE: wiki/log.md---`. Using `---FILE: wiki/index.md delta---` or `---FILE: wiki/log.md append---` causes `apply-blocks` to write separate files (`wiki/index.md delta.md`, `wiki/log.md append.md`) instead of merging into the real files. The script prints a warning ("FILE path missing .md suffix") but still succeeds — creating orphan files you must then manually merge with `merge-index` (for index) or `cat >>` (for log), then delete the orphans. Always use the exact filenames, no suffixes or qualifiers.

**Pitfall: `merge-index` reads from a file, not stdin.** When cleaning up orphan delta files, first merge the delta content into the real index:
```bash
python3 scripts/wiki_ops.py merge-index <root> --delta-file "<root>/wiki/index.md delta.md"
rm "<root>/wiki/index.md delta.md"
```
For log entries, simply append:
```bash
cat "<root>/wiki/log.md append.md" >> "<root>/wiki/log.md"
rm "<root>/wiki/log.md append.md"
```

## Page Merge Rule

When a generated content page already exists:

1. Union `sources`, `tags`, and `related`.
2. If body is unchanged, skip LLM merge.
3. If body differs, ask the LLM to merge old and new bodies.
4. Reject LLM merge if:
   - output has no frontmatter;
   - merged body is shorter than 70% of the longer input body.
5. Lock old `type`, `title`, and `created`.
6. Set `updated` to today.
7. Back up old content before fallback writes.

Use this for content pages: `entities`, `concepts`, `queries`, `sources`, `synthesis`, `comparisons`, and custom schema-defined content folders. Do not use it for `index.md`, `log.md`, or `overview.md`.

## Update

Update is ingest with a changed source:

1. Recompute source hash.
2. Find affected pages from cache, frontmatter `sources[]`, and content references.
3. Re-run analysis with attention to deltas and stale claims.
4. Apply FILE blocks through page merge.
5. Update index by delta merge.
6. Append log entry.
7. Sweep review queue conservatively.

## Failure Handling

- On parse anomalies, `apply-blocks` recovers what it can (an unterminated block
  is bounded at the next `---FILE:`/EOF instead of swallowing the following block)
  and reports a warning per affected block. Always surface those warnings and
  fix the cause — recovery is a safety net, not a guarantee the page is clean.
- On hard write failure, do not update ingest cache.
- On uncertain semantic merge, keep existing content via backup and create review item.
- Never delete pages as part of ingest unless the user explicitly requests source deletion cleanup.
