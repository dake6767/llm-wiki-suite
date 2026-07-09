# Video capture SOP — online video → RAW transcript (bring your own fetcher)

This skill does **not** ship a video fetcher. A video capture is an ordinary
adapter job (`references/adapter-contract.md`): lay the acceptance shape below
down in a temp dir with whatever tools this machine has, then hand off to
`normalize_raw.py`. This file is the scenario SOP — the acceptance contract,
the decision order, the §2 VAD-first ASR recipe, the §3 assembly script
(`scripts/srt_to_anchors.py`, shipped — that step is deterministic and
stack-independent), and the generic field-tested pitfalls. Platform-specific
recipes and dead ends (Bilibili, 抖音, 小红书) live in sibling
`references/video-*.md` files — the index is in §6. Everything a capture needs
is on this page, in the platform file §6 points to, or in `scripts/`.

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
   Bilibili format trap in `video-bilibili-pitfalls.md` (on Bilibili, list
   formats and pick the audio-only stream id explicitly).
2. **Route the ASR backend by language BEFORE transcribing.** Guess from the
   title/author (a title with ≥4 CJK chars and more CJK than latin ⇒ Chinese),
   or honor an explicit language:
   - **Chinese → SenseVoice** (FunASR `SenseVoiceSmall`,
     [github.com/FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice)
     via [FunASR](https://github.com/modelscope/FunASR)): ~15× faster than
     Whisper on CPU and far better on Chinese proper nouns. Install into a
     dedicated venv: `python3 -m venv ~/.local/share/llm-wiki/asr-venv &&
     ~/.local/share/llm-wiki/asr-venv/bin/pip install funasr torch torchaudio`.
     It reads wav (not m4a — convert first, §6) and has **no native
     timestamps** — feeding it the whole file returns ONE cue stamped
     `00:00:00,000 --> 00:00:00,000` (a documented incident re-ran a 40-min
     ASR twice before noticing). Timestamps come from a **VAD-first** pass —
     the recipe at the end of this section.
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

### SenseVoice → timestamped SRT: the VAD-first recipe

The order is the whole trick: **run VAD first, then recognise each speech
segment separately — the cue time is the VAD segment's bounds.** Never hand
SenseVoice the full audio and hope for cue times; it is non-autoregressive and
will return a single untimed blob (see the bullet above). One pass, no re-runs:

```python
# <asr-venv-python> this_script.py audio.wav out.srt [zh|en|yue|ja|ko|auto]
import re, sys, librosa
from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess
audio, out = sys.argv[1], sys.argv[2]
lang = sys.argv[3] if len(sys.argv) > 3 else "zh"
wav, _ = librosa.load(audio, sr=16000, mono=True)          # wav/mp3, not m4a (§6)
vad = AutoModel(model="fsmn-vad", max_single_segment_time=30000, disable_update=True)
spans = (vad.generate(input=audio) or [{}])[0].get("value") or [[0, len(wav)//16]]  # [[ms,ms],…]
sv = AutoModel(model="iic/SenseVoiceSmall", disable_update=True)
emoji = re.compile(r"[\U0001F000-\U0001FAFF☀-➿️]")          # SenseVoice injects 😊🎵 etc.
def ts(sec):
    ms=int(round(sec*1000)); h,ms=divmod(ms,3_600_000); m,ms=divmod(ms,60_000); s,ms=divmod(ms,1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
cues = []
for s_ms, e_ms in spans:
    chunk = wav[int(s_ms*16):int(e_ms*16)]                  # 16 samples/ms @ 16 kHz
    if len(chunk) < 160: continue                           # skip <10 ms slivers
    r = sv.generate(input=chunk, cache={}, language=lang, use_itn=True)
    text = emoji.sub("", rich_transcription_postprocess(r[0]["text"])).strip() if r else ""
    if text: cues.append((s_ms/1000, e_ms/1000, text))
open(out, "w").write("\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n" for i,(a,b,t) in enumerate(cues,1)))
```

Notes that earn their keep:

- `max_single_segment_time=30000` caps VAD segments at 30 s — SenseVoice's
  sweet spot, and conveniently the anchor granularity §3 wants.
- `use_itn=True` gives punctuation + inverse text norm;
  `rich_transcription_postprocess` + the emoji strip remove SenseVoice's
  `<|zh|><|HAPPY|>` tags and 😊🎵 markers — without this the RAW body is
  littered with them (also a live incident).
- The `[[0, len(wav)//16]]` fallback covers VAD finding nothing (rare:
  music-only or very short clips) — you still get one honest cue.
- The output is a standard SRT, so §3's `srt_to_anchors.py` consumes it
  unchanged.
- ~28× realtime on CPU: a 40-min video ≈ 2 min VAD+ASR. If it's taking tens of
  minutes, something is wrong — check you didn't feed the full file per cue.

---

## 3. Cues → anchored transcript (the assembly step)

This step is **deterministic and stack-independent** — whatever produced the
cues (captions or any ASR backend), it's the same text transform — so it ships
as a script. Don't hand-transcode cue lines or rewrite the logic ad hoc:

```bash
python3 <skill>/scripts/srt_to_anchors.py subs.srt \
  --url 'https://www.youtube.com/watch?v=<id>' > anchored.md   # or a .vtt
```

It handles both SRT and VTT, picks the deep-link form from the URL's host
(§1 table), and applies the assembly rules — which are the contract, whether
or not you use the script:

- Parse SRT/VTT cue blocks: take the left side of each `START --> END` line as
  the segment start; strip inline `<tags>`.
- **Merge fine-grained cues into ~30 s chunks** (one anchor per caption line is
  noise). Drop empty cues and consecutive exact duplicates (rolling
  auto-captions repeat lines).
- **CJK join rule:** joining cues with spaces corrupts Chinese text — join
  Chinese-dominant chunks with `""`, everything else with `" "`.
- Render each chunk as `**[M:SS](<deeplink>)** <text>` (H:MM:SS past the hour).

**The script's output is an intermediate, not the deliverable.** Assemble the
§1 `transcript.md` around it — real `# <video title>` H1, the `>` 作者/发布时间/
原文链接 header, the cover image line, the 简介 — and normalize the temp dir
with `--from`. Feeding the bare `anchored.md` to `normalize_raw.py --md` mints
a RAW file titled/slugged `anchored` with no cover (`assets: 0`), which then
needs frontmatter patching and a rename — a recurring live incident.
`normalize_raw.py` now hard-refuses a title falling back to a generic working
filename for exactly this reason.

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

1. **Title/slug** — set at normalize time, never patched after: the title
   comes from `--title` or the markdown's H1, and `normalize_raw.py` refuses
   to fall back to a generic working filename (`transcript`, `anchored`, …).
   Hitting that refusal means the §3 assembly step was skipped — build the §1
   `transcript.md` (or pass `--title`), don't rename the RAW file afterwards.
   If a wrong title still lands, fixing means frontmatter + renaming the RAW
   file **and** `raw/assets/<old-slug>--*` and the body links — expensive,
   which is why prevention is the rule.
2. **Cover** — confirm it landed in `raw/assets/` (the summary's `assets:`
   count ≥ 1; a video capture with `assets: 0` is flagged `capture_health: warn`).
3. `normalize_raw.py` special-cases `source_type: video`: no `has_video` /
   "can't download" callout — the transcript *is* the capture.
4. Found ASR errors **after** normalizing? RAW is immutable — fix the temp-dir
   `transcript.md`, delete the RAW file + its assets, re-run `normalize_raw.py`
   (same flags). Don't edit RAW in place.

---

## 6. Field-tested pitfalls & the platform index

Generic pitfalls, any platform:

- **Convert m4a → wav for Python-API ASR backends** (librosa/soundfile can't
  read m4a): `afconvert -f WAVE -d LEI16@16000 audio.m4a audio.wav` (or the
  ffmpeg equivalent).
- **Sponsor segments are spoken content** — ASR transcribes mid-video ad reads
  (1–3 min) as if they were lecture. Scan the built transcript for ad-indicator
  keyword clusters (product names, 评论区链接/专属福利/一键三连, 618/双11) and skip
  segments matching ≥2; log how many were skipped.
- **Auth walls:** don't scrape around them — surface the login step (browser
  login for cookie-based tools, `opencli youtube login` for opencli).

**Platform-specific recipes and dead ends live in sibling files** — before
capturing from one of these hosts, read its file first:

| Host | File | What it holds |
|------|------|---------------|
| Bilibili | `references/video-bilibili-pitfalls.md` | download/caption traps on top of Path A/B (`-x` trap, aria2c, HTTP 412, channel name, outro garbage) |
| 抖音 `v.douyin.com` / `douyin.com` | `references/video-douyin.md` | share-link resolution, opencli + mobile-share-page recipes, dead ends |
| 小红书 `xhslink.com` / `xiaohongshu.com` | `references/video-xiaohongshu.md` | opencli-only recipe (login-walled otherwise), dead ends |

Each platform file assumes this SOP: it supplies only fetch + metadata; the
contract (§1), ASR (§2), assembly (§3), long-run discipline (§4), and
verification (§5) stay here.
