# Capturing without opencli — a no-browser adapter profile

opencli is the skill's **default** fetch adapter, not a requirement. The wiki
core (`scripts/normalize_raw.py`) only consumes the on-disk shape in
`references/adapter-contract.md`; it never calls a scraper. So a user who hasn't
installed opencli — or who runs a different stack (e.g. the yt-dlp + whisper +
markitdown tooling that ships with projects like *chubbyskills*) — can still use
every part of this skill. This file is a concrete adapter profile that uses **no
opencli and no headless browser** — only tools the agent already has plus a few
common CLIs.

> Read this when `opencli` isn't installed, the user prefers other tools, or you
> are packaging this skill for someone who won't have opencli. For the opencli
> happy path, see `references/sources.md` instead. Both are just implementations
> of the same `adapter-contract.md`.
>
> To see which paths are live on *this* machine, run
> `python3 <skill>/scripts/preflight.py` — it maps each source type to its best
> available adapter and flags only what's genuinely missing.

## The principle

An adapter's only job: **lay the source down in a temp dir as `markdown + local
media`, then call `normalize_raw.py`.** Anything that produces that shape works.
The two things opencli gives you that a plain HTTP fetch doesn't are (a) a real
browser for JS-rendered/auth-gated pages and (b) automatic image download. Where
you lose those, localize media yourself (download referenced images into the temp
dir) or accept a text-only capture — `normalize_raw.py` will flag an empty/
un-localized capture via `capture_health`, so a degraded fetch never lands
silently.

## Per-source recipes (no opencli)

| Source | No-opencli path | Notes |
|------|------|------|
| **online video** (YouTube, Bilibili, …) | `scripts/fetch_video.py` already degrades automatically | With no opencli it uses **yt-dlp** for metadata + subtitles and the **local ASR** fallback (audio-only → Whisper/faster-whisper → delete audio). You lose the cleaner opencli caption path, nothing else. See "Online video" in `sources.md` and §Step-2 below. |
| **local document** (PDF/docx/pptx/…) | `markitdown` | Already opencli-free. Recipe in `sources.md` → "Local documents". Pass `--source-file` to archive the original. |
| **X / Twitter post** | the fxtwitter fallback | Already documented in `references/x-fallback-capture.md` — assembles the post (text + images + best mp4) from the public fxtwitter API, no browser. |
| **web page / article / news / blog** | agent fetch → md → `--md` | Use the agent's own **`tavily-extract`** skill (or built-in **WebFetch**) to get clean Markdown, write it to `/tmp/llmwiki-x/page.md`, then `normalize_raw.py --md … --source-type web`. Media: these return text, so inline images stay as remote URLs — download the ones you want into the temp dir and rewrite to local links for a faithful capture, or accept text-only. |
| **WeChat 公众号** | same as web (tavily-extract / WebFetch) | mp.weixin pages are largely static, so a text extractor captures the body well. Carry `author` / `publish_time` via flags. Images live in a JS gallery → download the visible ones manually if you need them. |
| **小红书 note** | tavily-extract / WebFetch | Image-centric and often gated; without a logged-in browser you may only get text + the cover. Note the gap to the user rather than pretending the capture is complete. |
| **first-party note** | none needed | `source_type: note` never used a fetch tool — see SKILL.md §6. |

### Minimal web example (no opencli)

```bash
# 1. Agent extracts clean markdown (tavily-extract skill or WebFetch), saved to:
#    /tmp/llmwiki-web/page.md   (download any must-keep images into the same dir
#    and reference them with relative links for a faithful capture)
# 2. Hand off to the core:
python3 <skill>/scripts/normalize_raw.py \
  --md /tmp/llmwiki-web/page.md --assets /tmp/llmwiki-web \
  --source-type web --source-url "<url>" --original-id "<stable id>" \
  --title "<title>" --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

This is exactly the contract's "no opencli at all" worked example
(`adapter-contract.md`), wired to tools the agent already has.

## Pluggable transcription (the video ASR backend)

`fetch_video.py` does not hard-wire one transcriber. Its no-caption fallback
resolves an **ASR backend** (`--asr auto|whisper|faster-whisper|sensevoice`,
default `auto`), and `auto` **routes by language** (guessed from the title, or an
explicit `--lang`): Chinese → SenseVoice when available, everything else → Whisper.

- **SenseVoice** — FunASR's `SenseVoiceSmall`, wrapped by the bundled
  `scripts/asr_sensevoice.py`. ~15x faster than Whisper and far better on
  **Chinese** proper nouns; weaker than Whisper on English, so `auto` only picks it
  for Chinese. It's a heavy optional dep (funasr + torch + a ~900MB model), so the
  cleanest install is a dedicated venv at the **auto-discovered path** — no env var
  needed:
  ```bash
  python3 -m venv ~/.local/share/llm-wiki/asr-venv
  ~/.local/share/llm-wiki/asr-venv/bin/pip install funasr torch torchaudio
  ```
  `_funasr_python()` probes, in order: `LLM_WIKI_ASR_PYTHON` (explicit override) →
  the interpreter running `fetch_video.py` → `python3` → that conventional venv. So
  `pip install funasr` into your default python also works; the env var is only for
  a funasr env in some other location. `LLM_WIKI_ASR_DEVICE=cpu|mps` (default cpu).
- **faster-whisper** — the `whisper-ctranslate2` CLI (`pip install
  whisper-ctranslate2`). Same Whisper models, ~3x faster, so a `large-v3` pass
  is affordable on CPU. The preferred non-Chinese backend. This is what
  chubbyskills-style setups already have.
- **openai-whisper** — the stock `whisper` CLI (`brew install openai-whisper`).
  The last-resort fallback when nothing else is present.

Whisper backends take the auto term-priming `--initial_prompt` (built from the
video's own title + keywords, see `build_whisper_prompt`); SenseVoice accepts and
ignores it (the model takes no decoder prompt).

**The backend contract** (whichever tool): take the audio file and write a
**timestamped `<outdir>/<stem>.srt`** — `audio_transcribe` always invokes
`--output_format srt` and parses cue times from the SRT, because per-segment
timestamps are the deep-link index a video RAW is built around. (A plain
`audio.txt` would lose them.) SenseVoice has no native timestamps, so its wrapper
derives them from a VAD pass — see `scripts/asr_sensevoice.py` as the reference
for adding any further Python-API backend.

## What this buys the distributed skill

- **opencli becomes optional, not assumed.** Every source type has a no-opencli
  path above; only the *cleanliness* of web/social/video captures degrades, never
  the ability to capture.
- **The core is unchanged.** Frontmatter, naming, media relocation, immutability,
  capture-health, wiki routing — all still handled by `normalize_raw.py`, the same
  for every adapter. Swapping fetch tools never touches the wiki contract.
