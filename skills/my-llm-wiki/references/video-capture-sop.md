# Video capture SOP — online video → RAW transcript (bring your own fetcher)

This skill does **not** ship a video fetcher. A video capture is an ordinary
adapter job (`references/adapter-contract.md`): lay the acceptance shape below
down in a temp dir with whatever tools this machine has, then hand off to
`normalize_raw.py`. This file is the scenario SOP — the acceptance contract,
the decision order, and the field-tested pitfalls — distilled from a reference
implementation (`scripts/fetch_video.py`, ~1,100 lines) that used to ship with
the skill; it lives in git history (`git log --diff-filter=D -- '*fetch_video.py'`)
if you ever want to read working code for any step.

The scenario: the user wants a video's **content** in RAW with the link kept —
**never** the video file, and never the audio after transcription. The faithful
"original" is the **URL**; the body is a timestamped **transcript** (a lossy text
extraction, like a `doc`'s markitdown text). source_type = `video`.

---

## 1. The acceptance contract (any pipeline must produce this)

A finished capture is a temp dir in the `--from` folder shape:

```
/tmp/llmwiki-vid-<unique>/
  transcript.md        # see layout below
  images/cover.jpg     # the thumbnail — the ONLY stored media
  status.yaml          # completion signal when running backgrounded (§4)
```

`transcript.md` layout (full spec: `references/raw-contract.md` → "Online video"):

```markdown
# <video title>

> 作者: <channel / uploader>
> 发布时间: <original publish date>
> 原文链接: <canonical video URL>

![封面](images/cover.jpg)

*时长 15:48 · 转写来源：官方字幕 · 含可跳转时间戳*

## 简介

<the video's own description>

## 文字转写

**[0:00](https://www.youtube.com/watch?v=<id>&t=0s)** …first ~30s of speech…

**[0:31](https://www.youtube.com/watch?v=<id>&t=31s)** …next chunk…
```

**The timestamp anchor is the point of the whole capture.** Every chunk
(~30 s of speech) is prefixed with a bold clickable `**[MM:SS](<deeplink>)**`
that opens the video *at* that second — this is what lets the wiki answer
*"a point was made somewhere in some video — where?"*. Deep-link formats:

| Host | Deep link |
|------|-----------|
| YouTube | `https://www.youtube.com/watch?v=<videoId>&t=<sec>s` |
| Bilibili | `https://www.bilibili.com/video/<BVid>/?t=<sec>` |
| anything else | append `?t=<sec>s` (or `&t=` if the URL has a query) — best effort |

Metadata to collect for `normalize_raw.py` flags: `title`, `author`,
`publish_time`, `original_id` (YouTube videoId / BV id), `source_url`,
`duration`. Track for your own report: `transcript_source` (captions vs
`<asr-backend>(<model>)`), `has_timestamps`, `segment_count`,
`needs_translation` (non-Chinese video → the agent appends `## 中文译文`).

**Fail loudly.** If no transcript can be produced, report the reason and stop —
never normalize a half-empty capture. Make failure messages *actionable* (which
tool to install, which site to log into), not just descriptive.

---

## 2. Probe, then take the cheapest path

Probe before fetching — `python3 <skill>/scripts/preflight.py` maps what's
installed (its recommendations carry install commands **and project home
URLs** — relay both when suggesting an install, so the user can vet the
source); `agent-reach doctor --json` (if present) reports per-platform
availability; `which yt-dlp ffmpeg` fills the rest. Then:

### Path A — captions (free, no download, seconds)

Most YouTube and many Bilibili videos have caption tracks (official or
auto-generated — Bilibili's AI `ai-zh` track counts). Whichever tool retrieves
them, keep the **per-cue start times**:

- **opencli** (if installed): `opencli youtube transcript <url> --mode grouped -f json` /
  `opencli bilibili subtitle <bvid> -f json` — drives the logged-in browser, so
  it sidesteps yt-dlp's "Sign in to confirm you're not a bot" wall.
- **yt-dlp**: `yt-dlp --skip-download --write-subs --write-auto-subs --sub-langs all
  -o "<tmp>/subs.%(ext)s" <url>` → parse the VTT/SRT cues (§3). Add
  `--cookies-from-browser chrome` behind an auth/bot wall.
- Metadata: `yt-dlp --dump-single-json --skip-download <url>` (or the opencli
  `youtube video` / `bilibili video` equivalents). Thumbnail:
  `yt-dlp --skip-download --write-thumbnail --convert-thumbnails jpg -o "<tmp>/images/cover" <url>`.

### Path B — no captions: audio-only download + local ASR

1. **Download audio only** with yt-dlp (never the video), e.g.
   `yt-dlp -x --audio-format mp3 -o "<tmp>/audio.%(ext)s" <url>` — but see the
   Bilibili format trap in §6 (on Bilibili, list formats and pick the
   audio-only stream id explicitly).
2. **Route the ASR backend by language BEFORE transcribing.** Guess from the
   title/author (a title with ≥4 CJK chars and more CJK than latin ⇒ Chinese),
   or honor an explicit language:
   - **Chinese → SenseVoice** (FunASR `SenseVoiceSmall`,
     [github.com/FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice)
     via [FunASR](https://github.com/modelscope/FunASR)): ~15× faster than
     Whisper on CPU and far better on Chinese proper nouns. Install into a
     dedicated venv: `python3 -m venv ~/.local/share/llm-wiki/asr-venv &&
     ~/.local/share/llm-wiki/asr-venv/bin/pip install funasr torch torchaudio`.
     It reads wav (not m4a — convert first, §6) and has no native timestamps —
     derive them from a VAD pass or per-utterance offsets.
   - **Everything else → faster-whisper** (`pip install whisper-ctranslate2`,
     [github.com/Softcatala/whisper-ctranslate2](https://github.com/Softcatala/whisper-ctranslate2);
     same models ~3-5× faster, makes `large-v3` affordable on CPU) →
     stock `whisper` (`brew install openai-whisper`,
     [github.com/openai/whisper](https://github.com/openai/whisper)) as last resort.
   - **Cloud fallback** — `agent-reach transcribe <url>` (Groq/OpenAI Whisper
     API) is fast and zero-install, **but returns plain text without
     timestamps**, so it breaks the anchor contract. Use it only when no local
     backend is viable and the user accepts a `has_timestamps: false` capture
     (plain paragraphs, no jump-back links) — or call the same Whisper API
     yourself with `response_format=srt` to keep the cues.

   **This routing is a gate, not a preference.** If the backend the language
   routes to is missing, do **not** quietly substitute another one — for
   Chinese audio, whisper is both ~10× slower on CPU *and* worse on proper
   nouns, so "lighter install" buys a strictly worse capture the user never
   agreed to. Stop and put the choice to the user: install the routed backend
   (relay the command + home URL above, and say up front that the torch
   download is ~2 GB), or have them explicitly accept a degraded path
   (whisper-for-zh, or the plain-text cloud fallback). Installing a toolchain —
   or settling for less — is the user's call, never a silent side effect of a
   capture.
3. **Emit SRT, not plain text.** Whichever backend: `--output_format srt`
   (whisper-family CLIs support it directly). Per-cue timestamps are the
   deep-link index — a plain `.txt` loses the entire anchor layer.
4. **Prime the decoder with the video's own vocabulary.** Whisper mangles
   code-switching and domain terms on smaller models ("token" → "偷肯",
   "GPT" → "吉皮提"). Pass `--initial_prompt` built from the video's **title +
   keywords/tags + first description line** (≤ ~600 chars) — free, and it
   travels with every video. Model size is the other big lever: `medium` is the
   floor, `turbo`/`large-v3` for term-dense content.
5. **Delete the audio when done.** Transcription is local and free; the media
   is never kept.

---

## 3. Cues → anchored transcript (the assembly recipe)

Don't hand-transcode cue lines token by token — run a small script. The rules:

- Parse SRT/VTT cue blocks: take the left side of each `START --> END` line as
  the segment start; strip inline `<tags>`.
- **Merge fine-grained cues into ~30 s chunks** (one anchor per caption line is
  noise). Drop empty cues and consecutive exact duplicates (rolling
  auto-captions repeat lines).
- **CJK join rule:** joining cues with spaces corrupts Chinese text — join
  Chinese-dominant chunks with `""`, everything else with `" "`.
- Render each chunk as `**[M:SS](<deeplink>)** <text>` (H:MM:SS past the hour).

```python
import re, sys
def secs(ts):
    p=[float(x) for x in ts.replace(",",".").split(":")]; s=0
    for x in p: s=s*60+x
    return s
def label(t):
    t=int(t); h,r=divmod(t,3600); m,s=divmod(r,60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
segs=[]
for block in re.split(r"\n\s*\n", open(sys.argv[1]).read().strip()):
    lines=[l for l in block.splitlines() if l.strip()]
    i=next((j for j,l in enumerate(lines) if "-->" in l), None)
    if i is None: continue
    text=re.sub(r"<[^>]+>","", " ".join(lines[i+1:])).strip()
    if text: segs.append((secs(lines[i].split("-->")[0].strip().split()[0]), text))
URL="https://www.youtube.com/watch?v=VIDEOID"   # adapt deeplink per host
out,cur,start,last=[], [], None, None
for sec,text in segs:
    if text==last: continue
    last=text
    if start is not None and sec-start>=30 and cur:
        t="".join(cur) if len(re.findall(r"[一-鿿]","".join(cur)))>len(re.findall(r"[A-Za-z]"," ".join(cur)))*0.3 else " ".join(cur)
        out.append(f"**[{label(start)}]({URL}&t={int(start)}s)** {t}"); cur,start=[],None
    if start is None: start=sec
    cur.append(text)
if cur:
    t="".join(cur) if len(re.findall(r"[一-鿿]","".join(cur)))>len(re.findall(r"[A-Za-z]"," ".join(cur)))*0.3 else " ".join(cur)
    out.append(f"**[{label(start)}]({URL}&t={int(start)}s)** {t}")
print("\n\n".join(out))
```

(A doc-embedded recipe, not a shipped script — adapt freely; the contract is
the *output shape*, not this code.)

---

## 4. Long-run discipline: background + poll one status file

Captions return in seconds, but an ASR pass takes **minutes to tens of
minutes** on CPU and exceeds single-command timeouts (some runtimes kill any
one command at 300 s). The universal contract:

- **Launch the download+ASR as a non-blocking job** (plain shell: `nohup … &`;
  other runtimes: their background-exec primitive) that writes a status file
  (e.g. `status.yaml`) as its **last** act, atomically.
- **Poll that file yourself** with short commands until it appears. Do **not**
  rely on the runtime's completion notification — observed failure: an agent
  waiting to be *told* sat idle long after the transcript was ready.
- **Fresh temp dir per capture, always** — `mkdir -p` does not clean an
  existing dir, and a poller that reads a *previous* run's `status.yaml` /
  `transcript.md` will polish and ingest the **wrong video** (a real, documented
  incident). Use `/tmp/llmwiki-vid-$(date +%s)` or `rm -rf` first; also delete
  any stale status file living outside the temp dir.
- Backgrounding changes nothing about speed — only how promptly you *notice*
  completion. Poll every ~30–60 s; a long video legitimately takes 10–25 min
  (SenseVoice: a 28-min zh video ≈ 5 min; whisper `medium` on CPU: the same
  video ≈ 55 min — install faster-whisper or use `turbo`).
- **Kill orphans before re-running:** a killed wrapper does *not* kill its
  child ASR process (it keeps eating 300-400 % CPU):
  `ps aux | grep -E 'whisper|funasr' | grep -v grep | awk '{print $2}' | xargs kill 2>/dev/null`

---

## 5. Verify — before and after normalizing

**Before polishing/normalizing** (cheap checks that catch 90 % of bad captures):

- `status`/metadata matches the video you asked for — title, `original_id`,
  `source_url` (a Bilibili BV id has resolved to a *different* video before;
  `status: ok` alone proves nothing).
- **Audio duration matches video duration** (`ffprobe -v error -show_entries
  format=duration …`) — a cookie-less Bilibili download can silently fetch 2 min
  of a 25-min video and ASR happily reports `ok` on the fragment.
- **Char-count sanity:** Chinese speech ≈ 300–600 chars/min. A 25-min video →
  7.5k–15k chars; **<1,000 chars for 20+ min ⇒ truncated**. At 30-s anchors,
  expect ~2 segments/min. But a transcript ending at ~80-85 % of a long video
  with a *natural sentence ending* is normal (non-speech outro music/previews) —
  don't re-run ASR for that.

**Then polish** (the agent's job, in the temp dir, per SKILL.md §8): light
content-preserving repair — punctuation, paragraph breaks, ASR homophone fixes —
**keeping every `**[MM:SS](…)**` anchor exactly where it is**. Build homophone
corrections from the *full transcript*, not the title (one 36-min video produced
7+ spellings of the same name); apply longer variants before shorter; verify
zero remaining with grep. Per-corpus glossaries are content data — they belong
in the wiki's `data/`, not in this skill.

**After normalizing:**

1. **Title/slug** — `normalize_raw.py` derives them from the `.md` filename;
   a file named `transcript.md` yields title/slug `transcript`. Patch the
   frontmatter title and rename the RAW file to a real slug (renaming means
   also renaming `raw/assets/<old-slug>--*` and the links in the body).
2. **Cover** — confirm it landed in `raw/assets/`.
3. `normalize_raw.py` special-cases `source_type: video`: no `has_video` /
   "can't download" callout — the transcript *is* the capture.
4. Found ASR errors **after** normalizing? RAW is immutable — fix the temp-dir
   `transcript.md`, delete the RAW file + its assets, re-run `normalize_raw.py`
   (same flags). Don't edit RAW in place.

---

## 6. Field-tested pitfalls (mostly Bilibili)

Earned from live captures; read before any Bilibili or ASR-fallback run.

- **`-x` is not audio-only on Bilibili.** `yt-dlp -x` can pick a merged
  video+audio DASH format (280 MiB, 30-min download) whose audio extraction
  silently truncates to ~2 min. **List formats and force the audio stream id:**
  `yt-dlp --cookies-from-browser chrome --list-formats <url> | grep audio` →
  `yt-dlp -f <audio-id> …`. Always ffprobe the result against the expected
  duration.
- **Bilibili m4a AAC corruption → download with `--downloader aria2c`.**
  yt-dlp's default HTTP downloader can produce m4a files whose AAC payload is
  damaged mid-stream (ffmpeg truncates; macOS `afconvert` errors `'bada'`).
  aria2c downloads are clean. Corruption can also be *transient* — one fresh
  re-download is worth trying before switching formats.
- **Convert m4a → wav for Python-API ASR backends** (librosa/soundfile can't
  read m4a): `afconvert -f WAVE -d LEI16@16000 audio.m4a audio.wav` (or the
  ffmpeg equivalent).
- **HTTP 412 = Bilibili bot protection.** Wait a few minutes, retry with
  `--cookies-from-browser chrome`. `--cookies-from-browser` itself can hang
  10-30 s when Chrome holds many cookies — close Chrome or use another browser.
- **Channel name:** `yt-dlp --print '%(channel)s'` returns "NA" on most
  Bilibili videos; use the public API:
  `curl -s "https://api.bilibili.com/x/web-interface/view?bvid=BV…"` →
  `.data.owner.name`.
- **Sponsor segments are spoken content** — ASR transcribes mid-video ad reads
  (1–3 min) as if they were lecture. Scan the built transcript for ad-indicator
  keyword clusters (product names, 评论区链接/专属福利/一键三连, 618/双11) and skip
  segments matching ≥2; log how many were skipped.
- **AI-caption outro garbage:** Bilibili AI captions faithfully "transcribe"
  outro music/jingles as nonsense text. Read the last 30-50 lines; truncate
  after the last real sentence. (Distinct from the normal ~80-85 % non-speech
  outro cutoff, where ASR simply emits nothing.)
- **Auth walls:** don't scrape around them — surface the login step (browser
  login for cookie-based tools, `opencli youtube login` for opencli).

---

*Reference implementations, preserved in git history of this repo:*
`skills/my-llm-wiki/scripts/fetch_video.py` *(the full pipeline: caption
fetching, language-routed ASR, anchor rendering, status-file contract) and*
`scripts/asr_sensevoice.py` *(a whisper-CLI-compatible SenseVoice wrapper that
derives SRT timestamps via VAD). Recover with*
`git log --diff-filter=D --oneline -- 'skills/my-llm-wiki/scripts/fetch_video.py'`.
