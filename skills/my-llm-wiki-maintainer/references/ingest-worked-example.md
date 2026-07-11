# Ingest Worked Example — End-to-End

A concrete session transcript showing the full ingest flow for a video source
into an existing wiki with content pages. Use as a checklist when running
ingest manually.

## Context

Source: YouTube video (Chinese, ~28 min, caption transcript)
Wiki: `~/wikis/<your-wiki>` (illustrative domain: 明朝 history)
Existing wiki: has `purpose.md`, `schema.md`, `wiki/index.md` with 10 entities,
13 concepts, 2 source pages, and a `wiki/entities/朱元璋.md` entity page.

## Step 1 — Resolve project root

Always verify unless the caller gave an explicit, verified root:
```bash
python3 scripts/wiki_ops.py resolve-root <path>
```

## Step 2 — Read project context (O(1) files only)

Read `purpose.md` and `schema.md` from the wiki root — what the wiki is about
and what page types exist. Do **not** read `wiki/index.md` into context:
"what's already there" comes from bounded retrieval in Step 5 (per-name
`retrieval-search` + `read-pages`) plus the zero-cost disk grep
sentinel (`grep -F "[[entities/<name>" wiki/index.md`). On a grown wiki a full
index in the prompt is re-billed on every subsequent call of the session.

## Step 3 — Probe source size

```bash
python3 scripts/wiki_ops.py probe-source <root> --raw <raw/sources/path.md>
```

- `path: "small"` → continue with single-pass ingest (steps 4–9)
- `path: "large"` → switch to MAP/REDUCE flow (`references/large-source-ingest.md`)

## Step 4 — Source-page dedup

```bash
python3 scripts/wiki_ops.py source-page <root> --raw <raw/sources/path.md> --url "<source_url>"
```

- `existing: null` → create a new source page
- `existing: "wiki/sources/..."` → merge into that page (use `merge-page`)

## Step 5 — Retrieve the working set, then analyze

Read the full raw source text and extract the candidate entity/concept names it
touches. Then, **per extracted name, one bounded search** (never a single topic
query for the whole source — that collapses working-set recall from ~97% to ~28%):

```bash
python3 scripts/wiki_ops.py retrieval-search <root> --q "朱元璋" --top 8
```

Pack the top 3–5 candidate pages with `read-pages` (defaults: 5 pages / 6000
chars per page / 24000 total), then identify:
- **Key entities and concepts** — what's already in the wiki vs what's new
- **Main arguments and evidence**
- **Connections to existing pages** — for cross-links
- **Gaps** — what needs review

## Step 6 — Generate FILE/REVIEW blocks

Write all blocks to a single temp file (e.g. `/tmp/blocks.txt`). Include:

1. **Source summary page** at `wiki/sources/<short-slug>.md`
   - Use a readable slug from the title (3–6 words), NOT the full raw filename
   - `sources: ["raw/sources/<type>/<filename>.md"]`
2. **Content pages** for new entities/concepts
3. **Index delta** at `wiki/index.md` — only new entries
4. **Log entry** at `wiki/log.md` — append-only
5. **Optional REVIEW blocks** for gaps

### Example blocks file structure

```
---FILE: wiki/sources/短标题.md---
---
type: source
title: 完整标题
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [tag1, tag2]
related: [existing-slug, new-concept-slug]
sources: ["raw/sources/video/YYYY-MM-DD-slug.md"]
url: "https://..."
authors: ["作者"]
year: YYYY
---

# 完整标题

摘要内容...

## 核心论点
...

## 主要内容
1. [[concepts/概念A]]：...
2. [[concepts/概念B]]：...
---END FILE---

---FILE: wiki/concepts/概念A.md---
---
type: concept
title: 概念A
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [...]
related: [related-slug]
sources: ["raw/sources/video/YYYY-MM-DD-slug.md"]
---

# 概念A

内容... with [[entities/实体名]] and [[concepts/其他概念]] wikilinks.
---END FILE---

---FILE: wiki/index.md---
## Concepts
- [[concepts/概念A|概念A]] — 一句话描述
---END FILE---

---FILE: wiki/log.md---
## YYYY-MM-DD

- Ingested source title
- Created N concept pages, N entity pages
---END FILE---
```

## Step 7 — Apply blocks (two-pass when updating existing pages)

**New pages only** (first pass):
```bash
python3 scripts/wiki_ops.py apply-blocks <root> --blocks-file /tmp/blocks.txt
```

**Updated/merged pages** (second pass, if updating existing pages):
```bash
python3 scripts/wiki_ops.py apply-blocks <root> --blocks-file /tmp/update-blocks.txt --overwrite
```

Alternatively for intelligent merge of a single existing page:
```bash
python3 scripts/wiki_ops.py merge-page <root> <existing-page-path> \
  --incoming-file /tmp/merge-blocks.txt --write
```

## Step 8 — Verify

Check that:
- All FILE paths appear in `written[]`
- `warnings` is empty (or only expected ones like "backed up before overwrite")
- If `merge-page` was used, verify output starts with `---` (frontmatter), not `---FILE:`

## Step 9 — Save ingest cache

Write `/tmp/pages.json` as a JSON array with the normal file-write tool, then:

```bash
python3 "$MAINTAINER/scripts/wiki_ops.py" cache save "$ROOT" "$RAW" \
  --files-file /tmp/pages.json
python3 "$MAINTAINER/scripts/wiki_ops.py" cache check "$ROOT" "$RAW"
```

Require the save output's `filesWritten` to match the JSON array and the check
output's `hit` to be true. This keeps long CJK paths in a data file without an
inline subprocess wrapper.

## Pitfalls recap

| Pitfall | Fix |
|---------|-----|
| Index delta file named `wiki/index.md delta` | Use exactly `wiki/index.md` — apply-blocks merges by wikilink target |
| `apply-blocks` positional arg | Flag is `--blocks-file`, not positional |
| `merge-page` flag | `--incoming-file`, not `--blocks-file` |
| `merge-page` writes `---FILE:` wrapper | Strip with `sed -i '' '1{/^---FILE:/d;}' <file>` |
| CJK paths in `cache save` | Use `--files-file`, then `cache check` |
| `related:` uses `[[wikilink]]` | Use bare basename slugs: `related: [slug-a, slug-b]` |
