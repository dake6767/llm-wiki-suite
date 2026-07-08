# Default adapter: opencli (web/social) + markitdown (docs)

This is the skill's **bundled default** fetch/convert adapter — one
implementation of the tool-agnostic `references/adapter-contract.md`. The wiki
core (`scripts/normalize_raw.py`) does **not** depend on opencli; it only
consumes the contract's on-disk shape, so this whole file can be replaced by any
tool that produces that shape. Use it when opencli is what you have (the common
case); swap it out when adapting the skill to a different scraper.

How to capture each kind of source with opencli/markitdown, and how their output
maps onto the RAW contract. The universal fallback is `web read` — it handles any
site, so an unknown host is never a blocker.

General rules:
- `opencli` is an **npm** CLI (`@jackwener/opencli`), not a Python package. If a
  shell can't find it (agents that don't load the user's nvm/profile), resolve the
  absolute path — see the **Preflight** section just below. Never `pip`/`pipx
  install opencli`.
- Always capture into a **temp** dir, then run `normalize_raw.py` to commit.
- Use `-f yaml` so you can read the `saved:` path and `title:` programmatically.
- opencli adapters drive a real browser. On a login/auth wall, surface the
  adapter's `login` subcommand to the user — don't scrape around it.
- Discover flags with `opencli <adapter> <cmd> --help`. Adapters evolve; trust
  `--help` over this file if they disagree, and note the drift to the user.

---

## Preflight — make the tools callable (don't reinstall blindly)

Both tools are frequently *installed but not on a sandboxed agent's `PATH`*
(agents often don't load the user's nvm/profile). Resolve the path first; install
only if genuinely absent. This is the classic time-sink — do it once, here.

### opencli (the web/social adapter)

`opencli` is an **npm** global CLI (`@jackwener/opencli`) — **not** a Python
package. Never `pip`/`pipx install opencli`; that fails fatally (wrong ecosystem).
opencli is a Node script, so it needs its sibling `node` too, and both live in the
same bin dir — **put that bin dir on `PATH`** (resolving only opencli's absolute
path isn't enough — it'll fail with `env: node: No such file or directory`):

```bash
OPENCLI="$(command -v opencli || true)"
if [ -n "$OPENCLI" ]; then BIN="$(dirname "$OPENCLI")"; else
  for d in "$HOME"/.nvm/versions/node/*/bin /opt/homebrew/bin /usr/local/bin "$HOME"/.local/bin; do
    [ -x "$d/opencli" ] && BIN="$d" && break
  done
fi
[ -n "$BIN" ] && export PATH="$BIN:$PATH"
command -v opencli >/dev/null && opencli --version || echo "opencli NOT found"
```

A `PATH` export does **not** persist across separate shell invocations, so run
this resolution in the **same** command block as your opencli calls (prepend it).
Only if opencli is genuinely absent, install with **npm** (needs Node.js):
`npm install -g @jackwener/opencli` — or tell the user. Do not fall back to pip.

### markitdown (the local-document converter)

`markitdown` (Microsoft's doc→Markdown converter) is a **pip/pipx** CLI — same
trap, opposite ecosystem. Resolve it first; install once only if truly absent:

```bash
MD="$(command -v markitdown || true)"
if [ -z "$MD" ]; then
  for d in "$HOME"/.pyenv/shims "$HOME"/.local/bin "$HOME"/Library/Python/*/bin \
           /opt/homebrew/bin /usr/local/bin "$HOME"/.local/pipx/venvs/markitdown/bin \
           "$HOME"/.asdf/shims; do
    [ -x "$d/markitdown" ] && export PATH="$d:$PATH" && MD="$d/markitdown" && break
  done
fi
command -v markitdown >/dev/null && markitdown --version || echo "markitdown NOT found"
```

Run it in the **same** block as your `markitdown` call. If genuinely absent,
install **once** with `pipx install markitdown` (or `pip install 'markitdown[all]'`)
or tell the user — don't auto-install repeatedly, and don't `npm`-flail.

---

## WeChat 公众号 — `mp.weixin.qq.com`

```bash
opencli weixin download --url "<url>" --output /tmp/llmwiki-wx --download-images true -f yaml
```

Output: `<tmp>/<title>/<title>.md` + `<title>/images/`. The md has a `>`-style
header (`公众号:` / `发布时间:` / `原文链接:`) and relative image links — exactly
what `normalize_raw.py --from <that folder>` expects. source_type = `wechat`.
`original_id` = the `/s/<token>` segment of the URL.

For a traditional article this localizes images cleanly. Video is a 腾讯视频
iframe → not downloadable; the script keeps the link and flags `has_video`.

**Verify images, and fall back to `web read` for the newer 图文 (image-text)
posts.** WeChat now also publishes 小红书-style image-text posts whose images live
in a JS-rendered gallery that `weixin download` does NOT extract — it returns the
full text but an empty/missing `images/` folder. So after `weixin download`,
check the `images/` folder: if it's empty even though the post clearly carries
images (an image-text post, an infographic, etc.), redo the capture with the
generic reader, which renders the gallery:

```bash
opencli web read --url "<url>" --download-images true --wait 5 --output /tmp/llmwiki-wx2 -f yaml
```

Then normalize from the web-read folder instead (still `--source-type wechat`).
web read's page header lacks the `公众号:` / `发布时间:` lines, so author and
publish_time get lost on the fallback — but the first `weixin download` pass
*did* capture them. Carry them over explicitly:

```bash
python3 <skill>/scripts/normalize_raw.py --from /tmp/llmwiki-wx2/<folder> \
  --source-type wechat --source-url "<url>" --original-id "<token>" \
  --author "<from weixin download>" --publish-time "<from weixin download>" \
  --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

web read also pulls in a little extra chrome (author avatar, 打赏/赞赏 UI text) —
trim the obvious boilerplate if easy, but RAW favors faithful over tidy. A
pure-text article legitimately has no images; a web-read retry on it is harmless
(same text, still no images), so when in doubt, retry.

---

## Any web page (incl. 小红书, blogs, news, docs) — fallback

```bash
opencli web read --url "<url>" --output /tmp/llmwiki-web --download-images true -f yaml
```

Same output shape as weixin (folder + md + images/, relative links), so
normalize the same way. Useful flags when a page renders slowly or via JS:
- `--wait-until networkidle` and/or `--wait <seconds>` for lazy content
- `--wait-for "<css selector>"` to block until the main content exists
- `--frames all-same-origin` if the article lives in an iframe

source_type: use `xiaohongshu` for `xiaohongshu.com`, else `web`. 小红书 notes
are image-centric — confirm the images came through; if the note is gated, the
user may need `opencli xiaohongshu login` first.

---

## X / Twitter — single post — `x.com` / `twitter.com`

**Treat an X post like the generic web path for content.** Do NOT build the
capture from `twitter bookmarks` fields — that listing's `text` is lossy (often
just a `t.co` link, truncated, or empty, even for long-form posts) and its
`has_media` can be wrong. The reliable content source is a per-tweet fetch:

1. **Full text + images** — the primary capture:
   ```bash
   opencli web read --url "<tweet-url>" --download-images true --output /tmp/llmwiki-x -f yaml
   ```
   This renders the whole post (long-form tweets included) and localizes its
   images into `images/` — the exact folder shape `normalize_raw.py --from`
   wants. Verified: a long-form tweet that the bookmarks listing reduced to a
   bare `t.co` link comes back here with its full body + all images.

2. **Video (web read can't get it)** — X video is a `blob:` source that
   `web read` leaves as a placeholder. If the post has video:
   ```bash
   opencli twitter download --tweet-url "<url>" --output /tmp/llmwiki-xv
   ```
   then move the downloaded `.mp4` into the web-read folder's `images/` and add a
   line `![video](images/<file>.mp4)` to that folder's `.md`, so normalization
   carries the file into the shared `raw/assets/` and links it locally.

3. **Normalize** from the web-read folder:
   ```bash
   python3 <skill>/scripts/normalize_raw.py \
     --from /tmp/llmwiki-x/<web-read folder> \
     --source-type x --source-url "<tweet-url>" --original-id "<status id>" \
     --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   ```

`original_id` = the numeric `/status/<id>`. On an auth wall: `opencli twitter login`.

Note: web read also captures a little page chrome (author avatar, engagement
counts). RAW favors faithful over tidy, so that's an acceptable trade for never
losing the body text.

**Fallback when opencli is unavailable or fails:** use `references/x-fallback-capture.md`.
It documents how to assemble a long-form/article tweet from the fxtwitter status
API, including downloading article images and the best mp4 variant before running
`normalize_raw.py --md ... --assets ...`. This is a fallback recipe, not a
replacement for the preferred opencli path.

`original_id` = the numeric status id from the URL (`/status/<id>`).
If any X command hits auth, have the user run `opencli twitter login` once.

---

## Online video (YouTube, …) — `scripts/fetch_video.py`

Goal: distill a video's **content** into RAW and keep the link — **never** store
the video file (the user collects many videos and has no disk for them). The
faithful "original" is the URL; the body is a **transcript** (a lossy text
extraction, like a `doc`'s markitdown text). source_type = `video`.

The bundled `scripts/fetch_video.py` is the adapter; it does only the
deterministic fetch and lays down the `--from` folder shape. The agent then
light-polishes and (for a foreign video) translates the transcript before
normalizing — see SKILL.md §8. Two transcript paths, cheapest first, **zero API
cost**:

1. **Captions** — free, no download. **YouTube** uses `opencli youtube transcript`
   and **Bilibili** uses `opencli bilibili subtitle` (including B 站's AI-generated
   `ai-zh` track) — both drive the logged-in browser, so they sidestep yt-dlp's
   "Sign in to confirm you're not a bot" wall; other hosts try yt-dlp's subtitle
   tracks. (Metadata likewise: `opencli youtube video` / `opencli bilibili video`,
   else `yt-dlp --dump-single-json`.)
2. **Audio + local ASR** — the no-caption fallback. Download audio-only with
   yt-dlp, transcribe with a **local** ASR backend (Whisper / faster-whisper),
   then **delete the audio**.

**The transcript is timestamped.** Every path above carries per-segment start
times — opencli/Bilibili cue timestamps, yt-dlp VTT cues, or Whisper emitting
**SRT** instead of plain text — and the script keeps them. The body renders each
~30s chunk with a clickable `**[MM:SS](…&t=NNNs)**` deep link to that exact
moment (`--segment-seconds` tunes the spacing; default 30). This is what makes
the wiki answer *"I remember a point was made somewhere in some video — where?"*:
a search hit carries both the passage and a one-click jump back to the source
moment. The summary reports `has_timestamps` / `segment_count`.

**opencli is optional here.** `fetch_video.py` uses opencli only for the *cleaner*
caption + metadata path on YouTube/Bilibili; if opencli isn't installed it
**degrades automatically** to yt-dlp for metadata, yt-dlp subtitles for captions,
and the audio + local-ASR fallback otherwise. So the script needs only
`yt-dlp` + `ffmpeg` + an ASR backend to work end-to-end without opencli (the audio
path on YouTube reads browser cookies via `--browser` to get past the bot wall).

```bash
python3 <skill>/scripts/fetch_video.py --url "<video url>" --output /tmp/llmwiki-vid
```

Options: `--whisper-model medium` (default; `base`/`small` are faster + rougher,
`medium`/`large-v3` slower + better, `turbo` = large-v3-turbo is fast *and*
accurate — a long video on CPU can take many minutes), `--asr auto|whisper|
faster-whisper` (ASR backend, default auto), `--browser chrome` (which browser's
cookies yt-dlp reads for the audio fallback), `--lang en` (force a caption / ASR
language; omit to auto-select the original track), `--prompt "..."` (override the
term hint), `--status-file <path>` (write the final summary there atomically),
`--keep-audio` (debug).

> **Long-running fallback → run it in the background and poll.** Captions return
> in seconds, but the no-caption Whisper pass takes **minutes to tens of minutes**
> on CPU and **exceeds short single-command timeouts** (e.g. hermes kills any one
> terminal command at 300 s). Don't call it as a blocking foreground command for a
> video that might lack captions — launch it detached with `--status-file` and a
> `run.log`, then poll the status file with short commands until it appears. The
> exact recipe is in SKILL.md §8 step 1. Installing **faster-whisper**
> (`pip install whisper-ctranslate2`, auto-picked) and/or `--whisper-model turbo`
> shortens the pass; background+poll is what makes even a slow pass safe.

**Transcription quality (the no-caption Whisper path).** Whisper mis-decodes
code-switching and domain terms on small models — a mixed Chinese/English tech
talk turns "token" into "偷肯", "GPT" into "吉皮提". Two levers, both applied
automatically:
- **Model size is the biggest lever.** `base` is rough; `medium`/`large-v3`/`turbo`
  fix most code-switching errors. The default is `medium`; bump to `large-v3` or
  `turbo` for term-dense tech/finance content. The **ASR backend is pluggable**
  (`--asr`): install faster-whisper (`pip install whisper-ctranslate2`) to afford
  `large-v3` at a fraction of the time — it's auto-picked when present. See
  `references/adapters-without-opencli.md` for adding other backends (e.g.
  SenseVoice for stronger Chinese ASR).
- **Term priming from the video's own metadata.** `fetch_video.py` auto-builds a
  Whisper `--initial_prompt` from the video's **title + keywords + description**
  (YouTube `keywords`, Bilibili `tag`, yt-dlp `tags`) and carries it across every
  window (`--carry_initial_prompt`), so the decoder spells the video's own jargon
  right. Pass `--prompt "token, API, RAG, ..."` to hand-tune the term list when a
  specific word keeps coming out wrong.
- **The agent's §8 polish is the backstop.** Whatever the ASR still gets wrong, the
  light-polish step fixes in context — a reader instantly knows "你这偷肯保真吗" is
  "你这 token 保真吗". Captions (YouTube/Bilibili) skip all of this — they're clean.

It prints a YAML summary — read these and pass them to `normalize_raw.py` as flags:

| summary field | use |
|------|------|
| `source_url` / `original_id` | `--source-url` / `--original-id` (originalId = the videoId) |
| `author` / `publish_time` | `--author` / `--publish-time` |
| `transcript_source` | `captions` or `<backend>(<model>)` (e.g. `whisper(medium)`, `faster-whisper(large-v3)`) — report which to the user |
| `has_timestamps` / `segment_count` | `true` is the norm — the body carries `**[MM:SS](…&t=…)**` jump-back anchors; preserve them when polishing |
| `needs_translation` | `true` → append a `## 中文译文` to `transcript.md` before normalizing (carry the same anchors into it) |
| `warnings` | surface to the user (e.g. cover failed, whisper warning) |
| `status: error` | do **not** ingest — show the message (auth/login/cookie issue) |

Then normalize the folder (the cover image is carried into `raw/assets/`):

```bash
python3 <skill>/scripts/normalize_raw.py --from /tmp/llmwiki-vid \
  --source-type video --source-url "<url>" --original-id "<videoId>" \
  --author "<channel>" --publish-time "<publishDate>" \
  --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

`normalize_raw.py` knows `video` is a deliberately-not-downloaded source, so it
does **not** stamp `has_video` / `video_links` / the "can't download" callout the
way it does for an undownloadable WeChat/X video — here the transcript *is* the
capture.

### Preflight — yt-dlp + whisper (the fallback path)

`fetch_video.py` resolves tool paths itself (it searches the nvm + Homebrew bins),
so you normally don't prep anything. The caption path needs only `opencli`
(already the skill's default — see the opencli Preflight above). The **audio
fallback** additionally needs `yt-dlp`, `ffmpeg`, and the `whisper` CLI; if a
capture errors with one missing, install the absent one **once** (don't reinstall
blindly):

```bash
brew install yt-dlp ffmpeg          # audio download + decode
brew install openai-whisper         # local, free transcription (CPU)
# optional, recommended for long/term-dense videos — same models, much faster,
# auto-picked by --asr auto when present:
pip install whisper-ctranslate2     # faster-whisper backend
```

The audio fallback reads cookies from your browser (`--browser`, default chrome)
to get past YouTube's bot wall — so be logged into YouTube in that browser. If a
video genuinely has no captions *and* the audio download hits auth, the script
reports `status: error`; relay it rather than landing a stub.

### Audio-fallback gotchas (the no-caption path)

- **`audio download incomplete …` (a preview/试看 clip behind a cookie wall)** →
  retry the **same command with ONLY `--browser <your-browser>` added**. Do **not**
  also switch ASR backends: the download was the problem, not the transcriber.
  **Keep `--asr auto`** so Chinese still routes to SenseVoice — forcing
  `--asr whisper` here is a common mistake that makes a zh video slow and worse for
  no reason.
- **`ModuleNotFoundError: No module named 'torch'` / "SenseVoice isn't working"** →
  SenseVoice's `torch`/`funasr` live in a **dedicated venv**
  (`~/.local/share/llm-wiki/asr-venv`), NOT in the system Python. The script finds
  that interpreter automatically (`_funasr_python`), so you almost never need to
  touch it. **Never debug this by running `python3 -c "import torch"`** — the system
  `python3` has no torch and will falsely report it missing, sending you down a
  rabbit hole. To check ASR deps authoritatively, run
  `python3 <skill>/scripts/preflight.py` (its `sensevoice:` line shows the exact
  interpreter and confirms `funasr+torch ✓`), or use that venv's python directly.
- **It looks like a broken backend but isn't** → remember transcription must run
  **backgrounded** (SKILL.md §8 step 1). A foreground SenseVoice/Whisper call gets
  killed by the command timeout, which can masquerade as a dead backend.

## Local documents (PDF, docx, pptx, xlsx, epub, …) — `markitdown`

opencli is for the web; it doesn't convert office/document files. For a local
document the user wants to archive into RAW, use Microsoft's **markitdown** —
it turns PDF/Word/PowerPoint/Excel/EPUB/HTML/CSV and more into Markdown with a
much better structure-preserving converter than a generic HTML scrape.

First make `markitdown` callable — run the **markitdown Preflight** above (the
Preflight section near the top of this file; it's a pip/pipx CLI, often installed
but missing from a sandboxed agent's PATH; resolve it, install once only if truly
absent). Then, in the same shell block:

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

Notes:
- **Always pass `--source-file`** — markitdown's Markdown is a *lossy text
  extraction* (it drops embedded figures, layout, tables-as-images). `--source-file`
  archives the **real original** into `raw/assets/` and records `source_file:` in
  the frontmatter, so RAW keeps the faithful source next to its searchable text.
- markitdown extracts **text structure**, not embedded images — a text-heavy
  document (report, paper, slides' text) comes through well; a scanned/image-only
  PDF yields little text. Because the original is archived via `--source-file`,
  `normalize_raw.py` does **not** false-flag a sparse extraction as a failed
  capture — the original is right there to fall back on.
- `source_url` uses a `file://` URI (or pass the document's real origin URL if it
  came from somewhere online). `original_id` = a content hash so the same file
  isn't captured twice (and its archived original isn't re-copied).
- Readability cleanup also runs here, so the extracted Markdown is tidy.

## Choosing source_type

`source_type` becomes the `raw/sources/<source_type>/` bucket and the frontmatter field.
Keep it short and stable: `wechat`, `x`, `xiaohongshu`, `web`, `doc` (local
documents via markitdown), `video` (online-video transcripts via
`fetch_video.py`). Add new buckets
(e.g. `zhihu`, `bloomberg`) when a source recurs enough to deserve its own shelf —
opencli has dedicated adapters for many (`zhihu download`, `bloomberg news`,
`36kr article`, …); check `opencli list` and prefer a dedicated adapter over
`web read` when one exists, since it yields a cleaner capture.
