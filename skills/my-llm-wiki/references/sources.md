# Source capture SOPs — probe first, then the best available recipe

This skill owns the **RAW contract**, not the fetchers. Every scenario below is
one job: lay the source down in a temp dir in the `adapter-contract.md` shape
(markdown + local media), then hand off to `normalize_raw.py`. *Which tool does
the fetching is a property of the machine, not of this skill* — so each scenario
starts from a capability probe and lists recipes per tool, best first. Any tool
that produces the shape is a valid adapter; when tools disagree with this file,
trust their `--help` and note the drift.

**Probe before fetching** (once per session; especially on a fresh machine):

```bash
python3 <skill>/scripts/preflight.py        # per-source-type capability map
agent-reach doctor --json 2>/dev/null       # per-platform availability, if installed
```

Common fetch stacks, in rough order of capture cleanliness:

| Stack | Install / home | What it gives you | Watch out |
|------|------|--------------------|-----------|
| **opencli** | `npm i -g @jackwener/opencli` · [npm](https://www.npmjs.com/package/@jackwener/opencli) | real logged-in browser: JS pages, auth-gated content, auto image download | an **npm** CLI — never pip-install it; needs its sibling `node` on PATH (put its bin dir on PATH, not just the binary) |
| **agent-reach** | [github.com/Panniantong/agent-reach](https://github.com/Panniantong/agent-reach) (one-line agent install in its README) | maintained per-platform access (X, Reddit, YouTube, 小红书, …) with `doctor` self-check | output is text/markdown — images usually stay remote URLs; localize them yourself |
| **bare CLIs** | [yt-dlp](https://github.com/yt-dlp/yt-dlp) · [ffmpeg](https://ffmpeg.org) · [markitdown](https://github.com/microsoft/markitdown) (`brew` / `pipx`) | video/audio/subtitles, docs | compose the shape yourself |
| **agent built-ins** (WebFetch / tavily-extract) | none needed | zero-install text extraction for any URL | text-mostly; images stay remote; JS/auth pages weaker |

**When a scenario's best tool is missing, don't silently degrade** — tell the
user what the recommended tool is, what it would improve for *this* capture, and
give the install command **with the project's home URL** (above) so they can vet
it. Then proceed with the best available path (or wait, their call).

General rules, every scenario:

- **Temp dir first** (`/tmp/llmwiki-<x>`), then `normalize_raw.py` commits. A
  failed fetch never touches RAW.
- **Media must end up local.** The core does not download remote media — a
  capture whose images are still `https://` links is degraded (the core flags it
  via `capture_health`, but localizing is the fetch step's job).
- **Don't fight auth.** Login walls → surface the tool's login step
  (`opencli twitter login`, browser login for cookie-based tools). Never scrape
  around auth.
- **PATH gotcha:** tools are frequently installed but missing from a sandboxed
  agent's PATH (nvm/Homebrew/pipx bins). Resolve the absolute path in the *same*
  shell block as the call; install only if genuinely absent — and in the right
  ecosystem (opencli = npm; markitdown = pip/pipx).

---

## WeChat 公众号 — `mp.weixin.qq.com`

source_type `wechat` · `original_id` = the `/s/<token>` URL segment.

- **opencli** (best — localizes images):
  ```bash
  opencli weixin download --url "<url>" --output /tmp/llmwiki-wx --download-images true -f yaml
  ```
  Output folder is exactly the `--from` shape, with a `>`-header (`公众号:` /
  `发布时间:` / `原文链接:`) the core parses. **Verify `images/`:** newer 图文
  (image-text) posts keep images in a JS gallery `weixin download` doesn't see —
  full text, empty images. Then redo with the generic reader
  (`opencli web read --url … --download-images true --wait 5`) and normalize
  from that folder instead, carrying `--author` / `--publish-time` from the
  first pass (web read's header lacks them).
- **No browser tool:** WebFetch / tavily-extract → save markdown to
  `/tmp/llmwiki-wx/page.md`, download the visible images into the dir, rewrite
  to relative links, pass metadata via flags. mp.weixin pages are largely
  static, so text comes through well.

A pure-text article legitimately has no images; when in doubt a web-read retry
is harmless. web read pulls a little chrome (avatar, 打赏 UI) — trim the obvious
boilerplate if easy; RAW favors faithful over tidy.

---

## Any web page (incl. 小红书, blogs, news) — the universal fallback

source_type: `xiaohongshu` for xiaohongshu.com, else `web`.

- **opencli**:
  ```bash
  opencli web read --url "<url>" --output /tmp/llmwiki-web --download-images true -f yaml
  ```
  Slow/JS pages: `--wait-until networkidle`, `--wait <s>`,
  `--wait-for "<css>"`, `--frames all-same-origin` for iframe articles.
- **agent-reach / WebFetch / tavily-extract**: get clean markdown → write to
  `/tmp/llmwiki-web/page.md` → download must-keep images into the dir and
  rewrite to relative links (or accept text-only; the core will flag it) →
  `normalize_raw.py --md … --source-type web` with metadata flags.

小红书 notes are image-centric and login-gated — a fresh session hits an
"IP at risk" login wall before content renders, so the generic readers above
usually fail. The working recipe is the dedicated adapter,
`opencli xiaohongshu` (this is also what agent-reach's xiaohongshu backend
delegates to): check `whoami`, then `note '<full signed URL with xsec_token>'`
for text/metadata (resolve the `xhslink.com` short link with
`curl -w '%{url_effective}'` to get one — a bare note id is rejected) and
`download '<xhslink or signed URL>' --output <tmpdir>` for images/video.
**Video notes** are a video capture — follow
`references/video-capture-sop.md` §8. If none of that is available, you may
only get text + cover; say so rather than pretending the capture is complete.

---

## X / Twitter — single post

source_type `x` · `original_id` = the numeric `/status/<id>`. An X post needs
**composing**, not one command — the full rules (don't trust bookmark-listing
text, video assembly, long-form `untitled` fix) are the scenario SOP regardless
of tool:

1. **Full text + images**: a per-tweet fetch of the rendered page —
   `opencli web read --url "<tweet-url>" --download-images true` (best), or the
   **fxtwitter API** (no browser needed): `references/x-fallback-capture.md`.
   Never build the capture from a bookmarks-listing `text` field — it's lossy
   (long-form posts show as a bare `t.co` link).
2. **Video**: rendered-page fetchers leave X video as a `blob:` placeholder.
   Download the mp4 separately (`opencli twitter download --tweet-url …`, or
   fxtwitter's best mp4 variant), move it into the folder's `images/`, add a
   `![video](images/<file>.mp4)` line to the md.
3. **Normalize** with `--from <folder>` (or `--md tweet.md --assets <dir>` when
   you composed the file yourself).

Long-form/article posts can come back `title: untitled` — fix before
normalizing: `references/x-article-pitfalls.md`.

---

## Online video (YouTube, Bilibili, …) — transcript, never the file

source_type `video`. The whole scenario — acceptance shape (timestamped
`**[MM:SS](…&t=NNNs)**` anchors), captions-first decision order, language-routed
local ASR (zh → SenseVoice, else faster-whisper), the anchor assembly recipe,
background+poll discipline, and the Bilibili pitfall list — lives in
**`references/video-capture-sop.md`**. Read it before any video capture.

The short version: probe → captions if they exist (free, seconds) → else
audio-only download + local ASR → assemble the anchored transcript → verify
duration/char-count → polish (SKILL.md §8) → normalize.

---

## Local documents (PDF, docx, pptx, xlsx, epub, …) — `markitdown`

source_type `doc`. markitdown (pip/pipx, **not** npm) converts office/document
files to structure-preserving Markdown:

```bash
markitdown "/path/to/file.pdf" -o /tmp/llmwiki-doc/doc.md
# stable id from content so re-ingesting the same file dedups:
id=$(shasum -a 256 "/path/to/file.pdf" | cut -c1-16)
python3 <skill>/scripts/normalize_raw.py \
  --md /tmp/llmwiki-doc/doc.md \
  --source-type doc \
  --source-file "/path/to/file.pdf" \
  --source-url "file:///path/to/file.pdf" \
  --original-id "$id" \
  --title "<document title or filename>" \
  --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

- **Always pass `--source-file`** — markitdown's text is a lossy extraction;
  the flag archives the real original into `raw/assets/` next to its searchable
  text, and stops the core from false-flagging a sparse extraction.
- A scanned/image-only PDF yields little text — that's expected; the original
  is archived.
- `source_url` is a `file://` URI unless the doc has a real online origin.

---

## Choosing source_type

`source_type` = the `raw/sources/<source_type>/` bucket + frontmatter field.
Canonical buckets: `wechat` · `x` · `xiaohongshu` · `web` · `doc` · `video` ·
`note`. Add a new bucket (`zhihu`, `bloomberg`, …) only when a source recurs
enough to deserve its own shelf — and prefer a dedicated adapter/channel over
the generic web path when the installed tools have one (`opencli list`,
`agent-reach doctor`), since it yields a cleaner capture.
