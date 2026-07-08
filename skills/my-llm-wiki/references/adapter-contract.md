# The adapter contract — bring your own fetch tool

This skill ships **no fetchers**, and the **wiki core depends on none**. The
engine (`scripts/normalize_raw.py`) never calls a scraper — it only consumes a
fixed *shape* on disk. So **any tool** that can produce that shape plugs in:
opencli, agent-reach, yt-dlp, a browser extension's export, a Python scraper,
even hand-written files. This file is the contract that decoupling rests on.

Whatever you fetch with, this is the only file to satisfy. The per-scenario SOPs
in `references/sources.md` give concrete recipes per common tool (opencli /
agent-reach / bare CLIs / the agent's own WebFetch) — all implementations of
this contract.

---

## What an adapter does (and where it stops)

An adapter's whole job is: **fetch a source and lay it down locally in one of the
two shapes below, with media already downloaded as local files.** Then it hands
off to `normalize_raw.py`. That's the line.

The adapter does **not** write into the wiki, invent frontmatter, name files, or
worry about immutability — the core does all of that (see "What the core
guarantees" below). Keep adapters thin: fetch → local files → call the script.

Always work in a **temp dir** (e.g. `/tmp/llmwiki-<x>`), never straight into the
wiki, so a half-finished fetch never lands in RAW.

---

## The two input shapes

`normalize_raw.py` accepts exactly two input forms (see its `--from` / `--md`
handling):

### 1. A folder — `--from <dir>`

A directory containing **one** `*.md` file plus its media. This is what
`opencli weixin download` / `web read` emit, but nothing about it is
opencli-specific.

```
/tmp/llmwiki-x/
  whatever.md          # exactly one *.md (if several, the first by sort wins)
  images/
    a.png
    b.jpg
```

- The `.md` references media with **relative, local** links:
  `![](images/a.png)`. Links are resolved relative to the folder, with a
  basename fallback (`images/a.png` or just `a.png` both resolve).
- Call: `normalize_raw.py --from /tmp/llmwiki-x --source-type web ...`

### 2. A single file — `--md <file> [--assets <dir>]`

A standalone markdown file you assembled, plus an optional media directory.

- `--assets <dir>` is where the file's relative media links resolve; if omitted,
  the file's own parent directory is used.
- Call: `normalize_raw.py --md /tmp/x/post.md --assets /tmp/x/media --source-type x ...`

Use shape 1 when your tool already outputs a folder; shape 2 when you composed a
single document yourself (e.g. one X post stitched from text + a downloaded mp4).

---

## The media responsibility line

**The core does not download remote media.** A link that is still a remote URL
(`![](https://cdn.example.com/a.png)`) is left untouched in the body — it is
*not* fetched. Localizing media is the **adapter's** job: download each image /
video and reference it with a relative local path before calling the script.

What the core *does* do with the local media you provide: copy it into the shared
`raw/assets/` folder, prefix the filename with the capture's `<date>-<slug>--` so
captures never collide, **correct the extension from the file's magic bytes**
(a JPEG served as `.png` is renamed and the link rewritten), and re-point every
link. Video files are turned into Obsidian embeds (`![[…mp4]]`).

So: adapter downloads and links locally → core relocates, de-collides, and
honestly names. Don't try to do the core's half; do faithfully do yours.

---

## Supplying metadata: two routes (use either, or both)

The core needs a little metadata (title, source URL, publish time, author). You
can supply it two ways; **command-line flags always override** anything parsed
from the file.

### Route A — flags (the portable, explicit way)

Pass metadata as flags. Only `--source-type` is required.

| Flag | Required | Meaning |
|------|----------|---------|
| `--source-type` | **yes** | The `raw/sources/<type>/` bucket + frontmatter field. See the bucket list below. |
| `--source-url` | recommended | Canonical original URL. |
| `--original-id` | recommended | The item's stable platform id — this is its **identity** for dedupe/versioning, not the slug. |
| `--title` | optional | Else taken from the file's first `# H1`, else the filename. |
| `--author` | optional | Author / handle / 公众号. |
| `--publish-time` | optional | The source's own publish-time string (verbatim; the core parses a date out of it). |
| `--captured-at` | recommended | UTC ISO-8601 ingest time. Pass `"$(date -u +%Y-%m-%dT%H:%M:%SZ)"`. |
| `--source-file` | doc only | An original file (e.g. the PDF a `doc` was converted from) to archive into `raw/assets/` and record as `source_file:`. |
| `--related` | note only | Comma-separated refs a `note` responds to (a RAW slug, `[[wikilink]]`, URL, or topic). |
| `--tags` | optional | Comma-separated extra tags (always added on top of `inbox`). |
| `--on-exists` | optional | `version` (default) / `skip` / `fail` — what to do if the item already exists. |

### Route B — an in-file header (what opencli happens to emit)

If your tool can't pass flags, the core also parses an optional header at the top
of the markdown:

```markdown
# The Title

> 公众号: Some Author          (also: 作者: / Author:)
> 发布时间: 2026年6月7日         (also: Publish time:)
> 原文链接: https://...          (also: Source: / Url:)

---

…body…
```

It lifts `title` (the first H1), `author`, `publish_time`, `source_url` out of
this preamble and strips it from the body. This is a convenience the default
opencli adapter uses — **a new adapter doesn't need it**; plain markdown + Route A
flags is simpler and just as complete.

---

## What the core guarantees (so adapters stay thin)

Once you hand off, `normalize_raw.py` deterministically does all of this — don't
reimplement any of it in an adapter:

- **Frontmatter** — builds the RAW YAML frontmatter (the output contract is in
  `references/raw-contract.md`).
- **File naming & placement** — `raw/sources/<source_type>/<YYYY-MM-DD>-<slug>.md`,
  date parsed from `publish_time` when possible.
- **Media** — relocate into shared `raw/assets/`, slug-prefix to de-collide,
  magic-byte extension correction, relative-link rewrite, video → Obsidian embed.
- **Readability cleanup** — runs `clean_md.py` on every non-`note` capture to
  repair HTML→Markdown structural damage (exploded links, split headings,
  over-escaping, social-page chrome). Content-preserving. (Skipped for `note`.)
- **Immutability & versioning** — never overwrites; identity is `original_id`.
  Same id again → `-v2.md`; a different source that slugifies the same → an id
  suffix. Slug clashes never silently drop a capture.
- **Wiki routing** — resolves the target wiki (`--wiki`, else ambient walk-up,
  else `$LLM_WIKI_DEFAULT`, else the registry). See `references/routing.md`.
- **Capture-health checks** — sanity-checks the result and flags
  `capture_health: warn` (+ a log line) when a capture looks empty / un-localized
  / chrome-dominated, so a degraded fetch surfaces instead of landing silently.

---

## source_type buckets (canonical)

`source_type` is both the `raw/sources/<type>/` folder and a frontmatter field.
Keep it short and stable. The buckets the skill uses today:

`wechat` · `x` · `xiaohongshu` · `web` · `doc` (local documents) · `video`
(online-video transcripts) · `note` (the wiki owner's own first-party writing).

`web` is the catch-all for any site without a dedicated bucket. Add a new bucket
(`zhihu`, `bloomberg`, …) only when a source recurs enough to deserve its own
shelf. `note` and `doc` get slightly relaxed health checks (a short note isn't a
failed fetch; a sparse doc extraction is fine because its original is archived).

`video` is the online-video bucket (`references/video-capture-sop.md` is its
scenario SOP). Its faithful "original" is the **URL**, not a stored file — the
video is deliberately never downloaded; the body is a **transcript** (a lossy
text extraction, exactly like a `doc`'s markitdown text). So the core suppresses
its `has_video` / "can't download" video machinery for this type: a video source
URL isn't a missing-media gap, it's the whole point. Captions or a local-ASR
pass produce the transcript; the only stored media is the cover thumbnail.

---

## Minimal worked example — no opencli at all

Proof the core is tool-agnostic: build the input by hand and ingest it.

```bash
# 1. An adapter (here: just you + echo) lays down the shape in a temp dir.
mkdir -p /tmp/llmwiki-byo/images
cat > /tmp/llmwiki-byo/post.md <<'EOF'
# Hello from any tool

This file was not produced by opencli. It references one local image:

![a picture](images/a.png)
EOF
# (a real adapter would have downloaded this; we just need a local file)
printf '\x89PNG\r\n\x1a\n' > /tmp/llmwiki-byo/images/a.png

# 2. Hand off to the core with metadata as flags.
python3 <skill>/scripts/normalize_raw.py \
  --from /tmp/llmwiki-byo \
  --source-type web \
  --source-url "https://example.com/hello" \
  --original-id "example-hello-1" \
  --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --wiki <some wiki root>
```

Result: `raw/sources/web/<date>-hello-from-any-tool.md` with proper frontmatter,
the image copied to `raw/assets/<date>-hello-from-any-tool--a.png`, and the link
rewritten — without opencli ever running. Any fetch tool that can produce the
folder above is a valid adapter.
