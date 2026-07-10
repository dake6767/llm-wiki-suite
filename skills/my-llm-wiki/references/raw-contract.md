# The RAW contract

Everything in RAW follows one shape so the Wiki layer (and future you) can rely
on it. `scripts/normalize_raw.py` produces exactly this — you rarely hand-write
it — but understanding the contract helps you debug and extend.

This layout matches the open-source [`llm_wiki`](https://github.com/) app and an
Obsidian vault, so a wiki the skill writes into opens cleanly in both.

> This is the **output** contract — what `normalize_raw.py` *writes*. Its mirror
> is the **input** contract, `references/adapter-contract.md`, which specifies
> what any fetch adapter (opencli or otherwise) must *hand to* the script. The
> script is the seam between the two.

## Contents

- [Folder layout](#folder-layout)
- [Frontmatter schema](#frontmatter-schema)
- [Online video](#online-video-source_type-video)
- [First-party notes](#first-party-notes-source_type-note)
- [Naming](#naming)
- [Immutability](#immutability)
- [Media and video](#media--video)
- [Readability cleanup](#readability-cleanup)

## Folder layout

Captures live under `raw/sources/`, one self-described markdown file per item;
images go in the shared `raw/assets/` folder (Obsidian's attachment path):

```
<wiki-root>/
  .llm-wiki/project.json         # identity { id, createdAt }; marks the repo
  schema.md                      # the Schema layer (conventions)
  raw/
    sources/
      wechat/
        2026-05-04-<slug>.md     # the capture, with YAML frontmatter
      x/
        2026-06-01-<slug>.md
      xiaohongshu/ , web/ ...
    assets/                      # shared media folder
      2026-05-04-<slug>--img_001.png
      2026-05-04-<slug>--img_002.jpg
  wiki/                          # LLM-generated layer (not this skill's job)
```

**Why one file + shared assets:** it's exactly what the `llm_wiki` app and
Obsidian expect — the app finds sources under `raw/sources/` (nested `source_type`
folders are supported), and Obsidian's attachment folder is `raw/assets/`. Asset
filenames are prefixed with the capture's `<date>-<slug>` so media from different
sources never collide in the shared folder. The bucket directory is the
`source_type`.

## Frontmatter schema

Each `<date>-<slug>.md` starts with YAML frontmatter, then the faithful body. The
app reads a RAW file as plain markdown, so this frontmatter is metadata for you
and the Wiki layer; it never breaks app compatibility:

```yaml
---
title: <article/post title>
source_type: wechat | x | xiaohongshu | web | ...
source_url: <canonical original URL>
original_id: <platform id — mp /s/ token, tweet id, note id; "" if unknown>
author: <author / 公众号 / handle>
publish_time: <original publish time, verbatim string from the source>
captured_at: <UTC ISO-8601, when we ingested>
status: raw
tags: [inbox, ...]   # inbox = not yet processed into the wiki
# present only when an original file was archived (e.g. a doc's source PDF):
source_file: ../../assets/<date>-<slug>--<original-name>
# present only when video couldn't be localized:
has_video: true
video_links:
  - https://v.qq.com/...
# present only when the capture tripped a sanity check (see below):
capture_health: warn
---
```

**capture_health** is added (value `warn`) only when `normalize_raw.py`'s sanity
checks fire — an almost-empty body, images left un-localized, or a chrome-dominated
capture. Clean captures omit the field. The same events are appended to
`<wiki>/.llm-wiki/capture-issues.log` (timestamp, source_type, url, path, reason)
as a persistent, greppable record of captures worth a second look — and an early
warning that the source's HTML (or the opencli adapter) may have changed and the
skill needs updating. (`.llm-wiki/` is in Obsidian's ignore list, so this log
never clutters the vault.) Find all flagged items later with
`grep -rl 'capture_health: warn' raw/sources/`.

Field intent:
- **source_url / original_id** — let the Wiki layer link back and dedupe. Always
  populate `source_url`; `original_id` is best-effort but is the item's identity.
- **publish_time vs captured_at** — keep them distinct. `publish_time` is the
  source's own (messy, human) string; `captured_at` is our machine timestamp.
- **status: raw** — a marker that this is unprocessed source material. The Wiki
  layer can flip its own view; the RAW file itself stays as-is.
- **tags** — default `[inbox]` so new captures are easy to find before processing.
  `inbox` is a "freshly captured" hint and stays on the RAW file (RAW is
  immutable — it's never flipped to mark the item processed). The authoritative
  "already synthesized into the wiki" ledger is the synthesis skill's own state
  (`my-llm-wiki-maintainer`'s `.llm-wiki/agent/ingest-cache.json`, keyed by source
  path + content hash), not this tag. See my-llm-wiki SKILL.md §7.
- **source_file** — when an original file is archived alongside the text (a `doc`'s
  source PDF/Word/PPT, passed via `--source-file`), it's copied into `raw/assets/`
  with the same `<date>-<slug>--` prefix and `source_file:` points at it. The `.md`
  body is the (lossy) text extraction; `source_file` is the **faithful original** to
  fall back on. Omitted when there's no separate original (web/x/note captures).

## Online video (`source_type: video`)

A video capture distills the video's **content** into RAW without ever storing
the media. Its frontmatter is the ordinary captured-source shape, with the video
as the source:

```yaml
---
title: <video title>
source_type: video
source_url: <canonical video URL — this IS the faithful original>
original_id: <platform video id — YouTube videoId, etc.>
author: <channel / uploader>
publish_time: <original publish date string>
captured_at: <UTC ISO-8601>
status: raw
tags: [inbox]
---
```

The body, produced by the `my-llm-wiki-video` capture pipeline
(`my-llm-wiki-video/references/video-capture-sop.md`)
+ the agent, is:

1. a localized cover image (`![](../../assets/<date>-<slug>--cover.jpg)`),
2. a one-line provenance note — `*时长 15:48 · 转写来源：官方字幕 · 含可跳转时间戳*`
   — recording duration, whether the transcript came from captions or a local
   Whisper pass, and that it carries timestamps (kept in the body, not the
   frontmatter, so the YAML schema stays generic),
3. `## 简介` — the video's own description,
4. `## 文字转写` — the (light-polished) **timestamped** transcript, and
5. `## 中文译文` — **only for a non-Chinese video** — a full Chinese translation
   *below* the original, so RAW keeps both the faithful original and the Chinese.

**Timestamped transcript with jump-back deep links.** Each ~30s chunk of the
transcript is prefixed by a bold, clickable anchor:
`**[23:56](https://www.youtube.com/watch?v=<id>&t=1436s)** …chunk text…`. The
label is the moment (`MM:SS`/`H:MM:SS`); the link opens the video *at* that
second (YouTube/Bilibili honored natively, other hosts get a best-effort `t=`).
This is the contract that lets the wiki answer "where, in which video, was this
said?" — a retrieval hit carries both the passage and a one-click jump to the
source moment. The anchors are plain inline Markdown links, so `clean_md.py`
leaves them intact and the deep-link URLs are *not* treated as un-downloadable
videos (the `video` special-case, below, suppresses that). The capture pipeline
tracks `has_timestamps` / `segment_count`; a translation, when present, carries
the same anchors so the Chinese is equally jumpable.

**No `has_video` / `video_links` / "can't download" callout.** Unlike a WeChat or
X capture whose embedded video couldn't be downloaded (a genuine gap that gets
flagged), a `video` capture deliberately doesn't store the media — the transcript
*is* the capture and `source_url` is the playable original. `normalize_raw.py`
special-cases `source_type == "video"` to suppress that machinery, so a `youtu.be`
link sitting in the description never gets mislabeled as un-downloadable. The cover
thumbnail is the only stored media (localized into `raw/assets/` like any image).

Why this respects RAW immutability/faithfulness: the transcript is a lossy text
extraction whose faithful original is the **URL** — structurally identical to a
`doc` (lossy markitdown text + an archived original file), except the original is
a link rather than a stored file. The light polish is content-preserving (same
spirit as `clean_md.py`); the translation is additive, never replacing the
original. See the `my-llm-wiki-video` skill and its
`references/video-capture-sop.md`.

## First-party notes (`source_type: note`)

Not everything in RAW is captured from outside. The wiki owner's **own** thoughts
and ideas belong in RAW too — as `source_type: note` — so they can be synthesized
alongside captured material (your view *and* the article it responds to, in one
place). This is the same RAW machinery, with a note-shaped frontmatter:

```yaml
---
title: <a title — the note's first H1, or one the agent drafts>
source_type: note
captured_at: <UTC ISO-8601, when written>
status: raw
tags: [inbox, note]
related:                # optional — what this note engages
  - 2026-06-07-some-article   # a RAW slug, an [[wikilink]], a URL, or a topic
---
```

How it differs from a captured source:
- **No `source_url` / `original_id` / `publish_time`** — there's no external
  origin; the note *is* the origin. Identity is just its date + slug, so two notes
  that slugify the same on one day land as `-2`, `-3`, … (no `original_id` key).
- **No readability cleanup** — `normalize_raw.py` skips `clean_md.py` for notes
  (there's no HTML→Markdown damage to repair), so intentional formatting and
  escapes (`\*`, custom indentation) are preserved verbatim.
- **Relaxed sanity check** — a two-line thought is valid, not a failed fetch, so
  the "almost no text" tripwire doesn't fire for notes. (The un-localized-image
  check still does — a note that references an image which didn't copy is worth a
  glance.)
- **`related`** — the synthesis hook: it links the note to the source(s) or topic
  it responds to, which is what lets the Wiki layer place "the owner thinks X"
  next to "source Y says Z".

Immutability still holds: a note is a snapshot of what you thought *then*. To
revise a view, add a new note; the evolving, current understanding lives in the
**wiki layer**, not in edits to the note. Local images (screenshots, photos) are
localized into `raw/assets/` exactly like captured media. Ingest is via the
my-llm-wiki skill's note path (SKILL.md) — typically you dictate the thought and
the agent files it.

## Naming

- File: `raw/sources/<source_type>/<YYYY-MM-DD>-<slug>.md`.
  - Date = parsed from `publish_time` when possible (handles `2026年5月4日`,
    `2026-05-04`, `2026/5/4`), else the capture date.
  - Slug = title with whitespace/punctuation collapsed to dashes; CJK kept
    as-is; length-bounded.
- Assets: `raw/assets/<YYYY-MM-DD>-<slug>--<original-name>`, referenced from the
  markdown with a relative link (`../../assets/<file>`) that renders in the app,
  Obsidian, and any plain-markdown viewer.
- This makes RAW naturally chronological and human-scannable per source.

## Immutability

RAW files are **never edited in place**. If you capture the same URL again, the
script writes `<date>-<slug>-v2.md` rather than overwriting (`--on-exists version`,
the default). Use `--on-exists skip` in batch sync to cheaply ignore already-
captured items, or `--on-exists fail` if you want a hard stop. Identity is the
`original_id`: two *different* sources that slugify the same get an `-<id8>`
filename suffix and both land — a slug clash never silently drops a capture.

If a user genuinely wants to *replace* a capture (e.g. a bad first fetch), delete
the old file (and its `raw/assets/<date>-<slug>--*` images) explicitly and
re-ingest — that's a deliberate act, not a default.

## Media & video

- **Images** are localized into `raw/assets/` and links rewritten to a relative
  `../../assets/<prefixed-name>`. opencli already downloads images for
  `weixin download` / `web read`; the script relocates, prefixes, and re-points
  them.
- **Extensions are corrected by content, not by name.** CDNs (X media in
  particular) often serve a JPEG under a `?format=png` filename. The script
  sniffs magic bytes when moving each file and fixes the suffix (e.g.
  `img.png` → `img.jpg`), rewriting the link to match, so RAW never lies about
  what's actually on disk.
- **Video** usually can't be downloaded (WeChat = 腾讯视频 iframe; most web video
  is DRM/streamed). When it can't, the script keeps the original link, sets
  `has_video: true` + `video_links`, and adds a callout in the body so the gap is
  explicit rather than silent. (This is about an *embedded* video in a web/wechat/x
  capture. A dedicated `source_type: video` capture is different — there the video
  is deliberately not downloaded and the transcript is the content, so this
  machinery is suppressed; see "Online video" above.) The one exception is X, where `opencli twitter
  download` (or the `my-llm-wiki-x` fallback in
  `references/x-fallback-capture.md`) can fetch
  video — place a local `![video](images/<file>.mp4)` link in the markdown and the
  script localizes it into `raw/assets/`.
- **Local video uses an Obsidian embed.** When the script localizes a video it
  rewrites the link to a wikilink embed — `![[<date>-<slug>--<file>.mp4]]` — not a
  standard `![](…mp4)`, because Obsidian only plays video through `![[…]]` embeds
  (resolved by basename across the vault). Images stay as portable
  `![](../../assets/…)` markdown.
- **Localized vs un-localized is never contradictory.** If a capture contains a
  local video asset, the script treats the video as localized: it sets
  `has_video: true` but omits `video_links` and the "can't download" callout (the
  `![[…mp4]]` embed already says it's there). It only emits `video_links` + the
  callout for videos that are *not* localized. So you can safely leave a remote
  `.mp4` URL lying in the assembled body — a localized video won't get
  mislabelled as un-downloadable.

## Readability cleanup

HTML→Markdown converters (opencli's `web read`, and turndown-style converters in
general) capture the right *content* but mangle the *markup*: inline links explode
into multi-line `[ \n\n text \n\n ](url)` blocks, headings get split from their
text (`##` on its own line, the title on the next), ASCII punctuation is
over-escaped (`1\.`, `\[\[x\]\]`), and social pages leak navigation chrome
(avatars, @handles, like/repost/view counts, "Upgrade to Premium" footers).

`scripts/clean_md.py` repairs this and runs automatically inside
`normalize_raw.py` on every capture. The repairs are **content-preserving**:
collapse exploded link blocks, merge split headings, un-escape spurious
backslashes, and collapse runaway blank lines. For X captures it additionally
trims page chrome using a precise signal: the avatar, display-name, and @handle
each link to the author's **bare profile URL** (`x.com/<handle>`), while the cover
image (题图) and inline article images link to `.../media|photo|article`. So it
drops the profile-linked byline + engagement counts + the footer block, and
**keeps the cover image and every inline image exactly where they sit in the
body** — image positions are never disturbed. Trimming is bounded to a window at
the very top and the footer at the very end, so prose is never cut.

It's also a standalone tool for repairing a RAW file captured before the cleanup
existed (or from another importer): `python3 scripts/clean_md.py <file.md>` reads
the frontmatter for `source_type`/`title`/`author`, cleans the body, and rewrites
the file in place — lossless, so this is a fix, not a content edit.
