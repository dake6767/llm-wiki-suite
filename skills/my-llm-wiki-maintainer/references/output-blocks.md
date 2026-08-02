# Output Blocks — the FILE/REVIEW contract

This is the **output contract** every generating operation shares (ingest, update,
save, large-source MAP/REDUCE, and review). When you ask the LLM to generate wiki
files, require these exact shapes so `apply-blocks` can parse and safely apply them.

## FILE block

```text
---FILE: wiki/concepts/example.md---
---
type: concept
title: Example
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [topical-word, another-topical-word]
related: [related-slug, another-slug]
sources: ["raw/sources/example.md"]
---

# Example

Body with full-path [[entities/some-entity]] and [[concepts/other]] wikilinks.
---END FILE---
```

The `---FILE: <path>---` line names the target path; everything between it and
`---END FILE---` is the file's full content (its own `---`-delimited frontmatter
plus the Markdown body).

> **Keep the frontmatter's opening `---`.** The header line ends in `---` and the
> frontmatter opens with `---` on the very next line — two `---` lines in a row.
> Do **not** collapse them: the page content must still begin with its own `---`
> fence, then the YAML keys, then a closing `---`. Emitting `---FILE: …---`
> straight into `type: …` produces a *headless* frontmatter the app and Obsidian
> can't parse. (`apply-blocks` now self-heals this and warns, but emit it
> correctly.)

> **`tags` is a field to fill, never a placeholder to keep.** Emitting `tags: []`
> is a defect, not a neutral default — it means the page went into the wiki with
> no retrieval facet at all. Generate real topical words from the source's actual
> subject matter (`[清朝, 雍正朝, 官员, 正直讲史]`), ≤5 per page, per `schema.md`'s
> Tag & Domain Policy. Do not echo the RAW capture frontmatter you just read:
> `inbox` is never acceptable, and a bare format word like `video`/`image`/`bilibili` is
> fine *alongside* real tags but never as the entire set. `apply-blocks` and
> `lint` both warn on an empty set, but emit it correctly rather than relying on
> the warning. (The one exception is `wiki/overview.md`, whose frontmatter is
> exactly four fields with no `tags` key — and ingest never writes it anyway.)

> **Quote frontmatter values safely.** A title/scalar that contains `"`, `:`,
> `#`, or leading/trailing spaces must be YAML-safe. The common trap is a title
> with interior double quotes wrapped in double quotes —
> `title: "从"康熙弃子"到"雍正副皇""` — which YAML reads as ending at the first
> interior `"` and then fails to parse the *whole* frontmatter. Single-quote such
> values instead (`title: '从"康熙弃子"到"雍正副皇"'`); use `''` to escape a
> literal apostrophe. (`apply-blocks` re-quotes broken scalars and warns, but
> emit them correctly.)

## Link convention

(See also `references/project-protocol.md`.)

- **Body** wikilinks use the full path `[[folder/slug]]`.
- **`related:`** uses bare basename slugs only (`[野生小虎, typo-seo]`) — never
  `[[...]]`, never a folder prefix, never a `.md` suffix, or the app's relation
  panel can't resolve them. `apply-blocks` normalizes `related:` anyway, but emit
  it correctly.

## Output language & filenames

Every page — title, body, **and filename** — follows the source's own language,
never a fixed one. Derive the filename from the title in that language, keeping
CJK characters as-is (`wiki/concepts/产品留存作为seo信号.md`, not an English
transliteration). See `references/ingest-update.md` → Output Language & Filenames.

## REVIEW block (optional)

```text
---REVIEW: suggestion | Precise title---
Why this needs attention.
OPTIONS: Deep Research | Create Page | Skip
PAGES: wiki/concepts/example.md | wiki/entities/other.md
SEARCH: query one | query two | query three
---END REVIEW---
```
