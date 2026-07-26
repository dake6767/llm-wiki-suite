<!-- llm-wiki-schema-version: 2 -->
# Wiki Schema

## Page Types

| Type | Directory | Purpose |
|------|-----------|---------|
| entity | wiki/entities/ | Named things (models, companies, people, datasets) |
| concept | wiki/concepts/ | Ideas, techniques, phenomena |
| source | wiki/sources/ | Papers, articles, talks, blog posts |
| query | wiki/queries/ | Open questions under investigation |
| comparison | wiki/comparisons/ | Side-by-side analysis of related entities |
| synthesis | wiki/synthesis/ | Cross-cutting summaries and conclusions |

## Naming Conventions

Filenames follow the **source's own language** — derive each from the page title, never translate to a fixed language. Keep CJK (中文 / 日本語 / 한국어) characters readable; use kebab-case for Latin-script titles.

- Entities: the thing's own name — `openai.md`, `gpt-4.md`, CJK kept as-is (`深度求索.md`).
- Concepts: a descriptive noun phrase in the source language — `chain-of-thought.md`, `思维链.md`.
- Sources: derived from the raw filename (which carries the source language); the ingest tooling supplies the slug via `source-page`.
- Queries: the question as a slug in the source language — `规模能否提升推理.md`, `does-scale-improve-reasoning.md`.

## Frontmatter

All pages must include YAML frontmatter:

```yaml
---
type: entity | concept | source | query | comparison | synthesis | overview
title: Human-readable title
domain: []                   # controlled facet — see Tag & Domain Policy
                             # (omit until you've defined values; never on entity/concept)
tags: [topical-word, another-topical-word]   # REQUIRED — real subject words, never []
related: []
created: YYYY-MM-DD
updated: YYYY-MM-DD
---
```

Source pages also include:

```yaml
authors: []
year: YYYY
url: ""
venue: ""
```

## Tag & Domain Policy

Two facets, kept separate. `domain` is a small **controlled** vocabulary — the
handful of areas this wiki covers; `tags` is **free** but hygiene-checked. They
never overlap: `tags` carries no domain or type words.

### `domain` — controlled, ≤2 values

A wiki is one repository, intentionally *not* split per topic: the shared
`entity`/`concept` layer is the connective tissue across areas (a single entity
or concept routinely links pages from every area), and a physical split would
sever it. `domain` is the **soft** partition instead — a low-cardinality label
the agent can scope retrieval and browsing by, without fragmenting the graph.

Define this wiki's controlled values in the table below — but **start empty and
let the areas emerge from real content**; a taxonomy guessed on day one, before
there are sources, usually fights the corpus later. While the table is empty,
just omit `domain`. Once a few natural areas are clear (often after a dozen-plus
sources), fill them in and apply going forward.

| domain | covers |
|--------|--------|
|        |        |

Rules:

- **Required** on intent-bearing pages once the table is non-empty: `source`,
  `synthesis`, `query`, `comparison`.
- **Omitted** on `entity` and `concept` pages — these are cross-cutting hubs,
  reached via links from any domain; forcing a single domain on them is wrong.
- Usually exactly one value; allow a second only when a page genuinely straddles
  two. Never more than two.
- Closed vocabulary: to add or rename a domain, edit the table first, then the
  pages — never coin a domain ad hoc in frontmatter.

### `tags` — free, hygiene-checked

Descriptive labels for retrieval and clustering, ≤5 per page, each reusable
across ≥2 pages:

- **Required on every content page — `tags: []` is a defect, not a safe
  default.** The rules below are all *constraints on which* tags to write; none
  of them is a reason to write none. A page that lands with an empty tag set has
  no retrieval facet at all, which is strictly worse than an imperfect tag.
  Every `entity`/`concept`/`source`/`query`/`comparison`/`synthesis` page gets
  real topical words drawn from its own subject matter. The sole exception is
  `wiki/overview.md`, whose frontmatter is exactly `type`/`title`/`created`/
  `updated` with no `tags` key. `apply-blocks` warns at write time and `lint`
  flags it as `missing-tags`, but fill it when generating the page.
- **One language per concept.** Keep an established foreign technical term as-is
  (e.g. `RAG`, `MCP`), but never *also* emit its translated duplicate. Default to
  the wiki's primary language otherwise.
- **Link, don't tag, anything with its own page.** Before writing each tag, check
  whether `wiki/entities/` or `wiki/concepts/` already has a page for it (match on
  meaning, not just exact filename). If a page exists, drop the tag and put the
  page in `related:` / link it `[[...]]` in the body instead. Wikilinks are the
  fine-grained graph; `tags` is only the coarse facet for labels that have *no*
  page of their own.
- **No singletons — read the vocabulary before you write.** A label that would
  appear on just one page belongs in the body or as a link, not in `tags`. This
  rule is only satisfiable if you first look at what the corpus already uses:

  ```bash
  python3 <maintainer-skill>/scripts/wiki_ops.py tags <root> --q "<this page's topic>"
  ```

  Prefer an established tag that fits; prefer a `*`-marked one (used once so far)
  over a fresh near-synonym of it; coin a new word only when nothing listed covers
  the page. Pass `--q` — the unscoped view lists only tags already on ≥2 pages, so
  a word coined by the previous ingest would be invisible and could never reach 2.
  The vocabulary is derived from the pages themselves, so each ingest both reads
  it and extends it — that loop is what makes the facet converge. Skipping the
  read is how one wiki ended up with `近代中国`, `民国政治` and `中国近代史` as
  three separate tags for one subject.
- **No type/domain semantics.** Don't put `concept`/`entity` or domain words in
  `tags` — those live in `type` and `domain`.
- **Never carry RAW capture frontmatter into wiki tags as a substitute for real
  ones.** RAW's `tags: [inbox, ...]` and `source_type` (`video`/`x`/`wechat`/
  `xiaohongshu`/`web`/`note`/…) describe the *capture*, not the *content*. When
  writing a `wiki/sources/` FILE block, generate real topical tags from the
  source's actual subject matter; don't default to echoing the RAW frontmatter
  you just read. Two different failure modes here, of different severity:
  - `inbox` is **never** tolerable, no matter what else is tagged alongside it —
    it means "not yet processed into the wiki", which is false the moment a wiki
    page exists at all.
  - A bare format word like `video`/`bilibili` is fine *as one tag among real
    topical ones* (`[清初, video, bilibili, 康熙朝, 索额图, 太子废立]` is OK — it's
    just a genre marker). It only becomes a defect when it's the page's *entire*
    tag set — i.e. no genuine topical tag was ever generated.

  ```yaml
  tags: [video, inbox]                              # WRONG — no real tags, plus inbox
  tags: [清初, video, 康熙朝, 索额图]                 # OK — video is just one of several real tags
  tags: [清朝, 雍正朝, 官员, 正直讲史]                # RIGHT — real topical words throughout
  ```

  `wiki_ops.py lint` flags both failure modes (`inbox` anywhere, or source_type
  words as the entire tag set) as a warning, but don't rely on lint to catch this
  after the fact — get it right at ingest time.

## Index Format

`wiki/index.md` lists all pages grouped by type. Each entry:

```text
- [[page-slug]] — one-line description
```

## Log Format

`wiki/log.md` records research activity in reverse chronological order:

```text
## YYYY-MM-DD

- Action taken / finding noted
```

## Cross-referencing Rules

- Use `[[page-slug]]` syntax to link between wiki pages
- Every entity and concept should appear in `wiki/index.md`
- Queries link to the sources and concepts they draw on
- Synthesis pages cite all contributing sources via `related:`

## Contradiction Handling

When sources contradict each other:
1. Note the contradiction in the relevant concept or entity page
2. Create or update a query page to track the open question
3. Link both sources from the query page
4. Resolve in a synthesis page once sufficient evidence exists

---

## RAW layer (source-of-truth ingestion)

The page types above are the **wiki layer** — LLM-generated, derived. They are
built from the **RAW layer**: immutable original sources you ingest and never
hand-edit. The two layers and the schema mirror the LLM-WIKI pattern
(<https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f>): you read
from RAW, the LLM writes the wiki.

**Where RAW lives** — `raw/sources/<source_type>/<YYYY-MM-DD>-<slug>.md`, one
self-described markdown file per captured item. `source_type` is the platform
bucket: `wechat`, `x`, `xiaohongshu`, `web`, … plus `note` for the wiki owner's
own first-party writing (see below). Downloaded images live in the shared
`raw/assets/` folder (Obsidian's attachment path), named
`<YYYY-MM-DD>-<slug>--img-NNN.<ext>` so captures don't collide, and referenced
from the markdown with relative links (`../../assets/<file>`).

**RAW frontmatter** (added by the ingest tooling; reading it is optional — the
file is also just plain markdown):

```yaml
---
title: ...
source_type: wechat | x | xiaohongshu | web | ...
source_url: ...
original_id: ...        # platform id — the item's stable identity
author: ...
publish_time: ...       # the source's own string
captured_at: ...        # UTC ISO-8601, when ingested
status: raw
tags: [inbox, ...]      # `inbox` = not yet processed into the wiki
# optional: source_file (archived original) / has_video / video_links / capture_health: warn
---
```

- RAW is **immutable**: re-captures are versioned (`-v2.md`), never overwritten.
  Identity is `original_id`, not the slug. `domain` is a wiki-layer judgment, not
  a RAW field — never add it to a RAW file; the maintainer assigns it on the
  derived wiki pages.
- `capture_health: warn` flags a capture that tripped a sanity check (empty body,
  un-localized images, page-chrome-dominated) — the source's HTML or the ingest
  adapter may have changed. The same events are logged in
  `.llm-wiki/capture-issues.log`. Find flagged items with
  `grep -rl 'capture_health: warn' raw/sources/`.

**First-party notes (`source_type: note`)** — the wiki owner's *own* thoughts,
ideas, and observations, captured into RAW the same way an external source is.
Their frontmatter omits the external-source fields (`source_url`, `original_id`,
`publish_time`) and instead carries `tags: [inbox, note]` and an optional
`related:` list pointing to the source(s) or topic the thought responds to:

```yaml
---
title: ...
source_type: note
captured_at: ...        # when it was written
status: raw
tags: [inbox, note]
related:                # optional — what this note responds to
  - 2026-06-07-some-captured-article    # a RAW slug / [[wikilink]] / URL / topic
---
```

Treat a `note` as the **owner's stance or working hypothesis**, not as external
evidence — when synthesizing, weight and attribute it accordingly ("the owner
believes X" vs "source S reports Y"), and follow `related:` to connect the
owner's view to the material it engages. A note is still an immutable snapshot of
what the owner thought *then*; revising a view means adding a new note, and
letting the evolved understanding live in the wiki layer — not editing the note.

**Ingest** — get an outside source into RAW with the **my-llm-wiki** skill (paste
a link, or sync X bookmarks); it fetches via opencli, localizes media, and writes
a conforming RAW file. Then process `inbox` RAW into wiki pages per the page-type
rules above. Don't hand-write RAW unless necessary.
