# Video capture pitfalls & post-normalize verification

Real failure modes earned from live video captures (mostly Bilibili + the local
ASR fallback). Read this when a video capture misbehaves. Per-corpus ASR homophone
glossaries are **not** here — they live in `data/asr-corrections.zh-history.md`
(content data, not skill guidance).

## Post-normalize verification (run after §4 for every video capture)

`normalize_raw.py` derives the RAW slug and title from the fetched folder's `.md`
filename. `fetch_video.py` names its output `transcript.md`, so without intervention
the RAW title/slug both come out as `transcript` — not the video's real title.
**Always verify after normalizing:**

1. **Title field** — if it says `transcript` or something generic, patch the
   frontmatter to the real video title (quoted).
2. **Slug/filename** — rename `raw/sources/video/YYYY-MM-DD-transcript.md` to a
   meaningful slug; if you rename, also rename any `raw/assets/<old-slug>-*` files
   and update the image paths inside the md.
3. **Cover image** — confirm the localized cover landed in `raw/assets/`. If
   `assets: 0` was reported but a cover exists in the temp dir, copy it and add the
   `![封面]` reference.
4. **Missing metadata** — add `duration`, `transcript_source`, `has_timestamps`,
   and relevant `tags` if the normalize step missed them.

## Backgrounding is environment-specific; polling `--status-file` is universal

The §8 contract is: launch `fetch_video.py` as a **non-blocking** job that writes
`--status-file`, then **actively poll that file** until it appears. *How* you
background the job depends on the runtime — but the polling never changes, and you
must **not** depend on the runtime to notify you on completion.

- **Plain shell / cron / CI** → `nohup … &` (or `setsid`), redirecting to a `run.log`.
- **Hermes Agent** → the terminal tool **rejects** `nohup`/`disown`/`setsid`/trailing
  `&` and tells you to use `terminal(background=true)`; launch that way and poll with
  `process(action='poll', session_id=…)` **plus** a read of `status.yaml`.
- **Any other agent** → use its own non-blocking exec primitive; the `--status-file`
  poll contract is identical.

**Do not rely on `notify_on_complete` / "the runtime will tell me when it's done."**
Observed failure (2026-06): leaning on Hermes' completion notification instead of
polling left the agent idle long after the transcript was ready — it only reported
"done" when the user proactively asked. The background mechanism has **zero** effect
on transcription speed (it's the same subprocess + the same Whisper either way); it
only changes how promptly you *notice* completion. Polling `status.yaml` yourself is
what guarantees you notice promptly, in every environment.

## Pitfall: Bilibili URL silently resolves to a *different* video

`fetch_video.py` has resolved a Bilibili BV ID to a **different** video (a real case:
`BV17o4BeYE1c` about 鳌拜 came back as YouTube `y0IkFsoQVp0` about 朱元璋), reporting
`status: ok` with the wrong title/id. **After every Bilibili fetch, verify
`status.yaml` before normalizing:** `title` matches the intended video, `original_id`
is the BV ID (not a YouTube videoId), `source_url` is the Bilibili URL. If they don't
match, kill the process, clear orphan Whisper children, and re-run — or fall back to
the manual yt-dlp pipeline below.

## Pitfall: stale temp dir / polishing the wrong transcript

`mkdir -p` does **not** clean an existing dir, and the background process writes
`status.yaml` / `transcript.md` only when it finishes — so reading them before
completion returns a **previous** run's data. A real failure: a new capture reused
`/tmp/llmwiki-vid/`, the agent read the prior video's stale `transcript.md`, polished
+ normalized + synthesized the **wrong** video, then the background process overwrote
the polish with the correct (unpolished) transcript.

- **Always use a FRESH temp dir per capture:** `TMPDIR="/tmp/llmwiki-vid-$(date +%s)"`,
  or `rm -rf /tmp/llmwiki-vid && mkdir -p /tmp/llmwiki-vid` first.
- **Never read/write the output dir until the process is confirmed complete** —
  `status.yaml` existing AND showing the expected URL/ID is the completion signal.
  Do not polish `transcript.md` before that.

## Pitfall: orphan Whisper processes on restart

When a `fetch_video.py` background process is killed (timeout, abort, re-run), its
**child Whisper process does not die** — it keeps eating ~300-400% CPU, and a re-run
launches a second one. Before restarting a video fetch, kill orphans and verify
they're gone:

```bash
ps aux | grep whisper | grep -v grep | awk '{print $2}' | xargs kill 2>/dev/null
```

## Whisper medium on CPU: wall-time expectations

Whisper `medium` on CPU (no GPU/MPS, no faster-whisper) is much slower than expected —
measured on Apple Silicon: a 28-min video ≈ **55 min** wall time, a 33-min video ≈
**60+ min**. The §8 background+poll pattern is essential (a foreground call hits the
300 s timeout and dies). For time-sensitive sessions, install **faster-whisper**
(`pip install whisper-ctranslate2`, 5-10× faster) or use `--whisper-model turbo` —
the same 33-min video then finishes in ~5-8 min.

**SenseVoice is dramatically faster than Whisper for Chinese.** Measured on Apple
Silicon (CPU, no GPU): a 28-min video finished ASR in **~5 minutes** (rtf_avg ~0.09).
SenseVoice processes in ~30-second chunks, so wall time scales roughly linearly with
audio length. For Chinese content, always prefer `--asr auto` (routes to SenseVoice)
rather than forcing Whisper. The §8 background+poll pattern is still recommended but
SenseVoice rarely exceeds 10 min for videos under 1 hour.

## Diagnostic: silent partial download (truncated audio, `status: ok`)

A Bilibili audio download without cookies can silently fetch only a fraction of the
audio; ASR runs fine on the truncated file and reports `status: ok` — the only clue
is an absurdly short transcript (a real case: a 25:34 video yielded 2:30 of audio →
23 segments / 947 chars). After `status.yaml` appears, sanity-check before polishing:

```bash
# audio vs video duration (status.yaml reports `duration`)
ffprobe -v error -show_entries format=duration \
  -of default=noprint_wrappers=1:nokey=1 /tmp/llmwiki-vid/audio.mp3
```
Rules of thumb: Chinese speech ≈ 300-600 chars/min (a 25-min video → 7.5k-15k chars;
<1,000 chars for 20+ min ⇒ truncated); at `--segment-seconds=30`, expect ~2
segments/min (a 25-min video → ~50 segments). If any check fails, kill, re-run with
`--browser chrome` (Bilibili needs cookies), and confirm the audio duration matches.

## How to approach ASR corrections (no per-video tables here)

Local-ASR transcripts (Whisper/SenseVoice) carry systematic homophone errors on
Chinese proper nouns — Whisper and SenseVoice err *differently* (Whisper: systematic
homophones but knows more historical terms; SenseVoice: better on common words and
code-switching, but mangles Mongolian/Manchu names and produces more variants per name
on longer videos, sometimes mixed-script artifacts like "苏mer尔"). Both need the §8
polish pass; the patterns are complementary.

**Build the correction dict from the full transcript, not just the title** — grep for
all variant spellings of each name before bulk-replacing (a 36-min video produced 7+
spellings of 索额图; 施襄夏 hit 11+). A growing glossary of corrections seen so far for
this user's Qing-history / 围棋 corpus is in `data/asr-corrections.zh-history.md` —
that's **content data tied to a specific corpus**, kept out of the skill's loadable
references on purpose. Its proper long-term home is the user's wiki.

## Pitfall: yt-dlp `-x` downloads merged video+audio on Bilibili, not audio-only

On Bilibili, `yt-dlp -x --audio-format mp3 --cookies-from-browser chrome` can
select a **merged video+audio format** (e.g. `100027+30280`, 280 MiB) even though
`-x` means "extract audio". The download takes 30+ minutes at slow speeds, then
the audio conversion silently truncates to 1–2 minutes because the container
is video-primary. You end up with a 2.4 MB / 113-second mp3 that passes
`status: ok` but is 95% missing.

**Fix: force audio-only format explicitly.** First list formats, then pick the
audio stream directly:

```bash
yt-dlp --cookies-from-browser chrome --list-formats "<bilibili-url>" | grep audio
# Example output:
# 30216  m4a audio only  | ≈  7.59MiB   38k
# 30232  m4a audio only  | ≈ 15.85MiB   79k
# 30280  m4a audio only  | ≈ 29.30MiB  146k

yt-dlp -f 30280 --cookies-from-browser chrome \
  -o "/tmp/llmwiki-vid/audio.m4a" "<bilibili-url>"
```

**Sanity check after download:** always verify `ffprobe` duration matches the
expected video length. A truncated file (e.g. 113s for a 28-min video) means
the wrong format was selected.

**Why this happens:** Bilibili serves DASH streams where video and audio are
separate. yt-dlp's `-x` extracts audio from a downloaded container, but on
Bilibili it selects the merged format first (for quality) and the extraction
step can silently truncate. Forcing `-f 30280` (audio stream ID) bypasses the
merge entirely.

## Pitfall: `fetch_video.py` fails on Bilibili — manual fallback pipeline

When `fetch_video.py` can't download the full Bilibili audio (HTTP 412, partial
download, or `--browser chrome` doesn't help), fall back to yt-dlp + SenseVoice
directly. The error message `"audio download incomplete: got Xmin of Ymin expected"`
is the clearest signal — don't retry fetch_video.py, go straight to the manual
pipeline below. yt-dlp with `--cookies-from-browser chrome` often succeeds where
fetch_video.py's embedded downloader fails (different cookie extraction paths).

**`opencli bilibili download`** is a browser-based alternative but has a **60-second
default timeout** that silently kills downloads of longer videos (30+ min). Use
`OPENCLI_BROWSER_COMMAND_TIMEOUT=600` env var (the `--timeout` flag is not recognized
by the CLI). For longer videos, set this env var before running the command.
However, the downloaded mp4 may also have AAC corruption — extract audio with
`afconvert` or `ffmpeg` and verify duration.

**1. Resolve a short link → real URL**
```bash
curl -sI -o /dev/null -w '%{redirect_url}' "https://b23.tv/xxxxx"
```
**2. Download audio with cookies (use aria2c to avoid AAC corruption)**
```bash
mkdir -p /tmp/llmwiki-vidN
yt-dlp -f 30280 --cookies-from-browser chrome --downloader aria2c \
  -o "/tmp/llmwiki-vidN/audio.m4a" "https://www.bilibili.com/video/BV…/"
# Convert to wav (SenseVoice needs wav, not m4a)
afconvert -f WAVE -d LEI16@16000 /tmp/llmwiki-vidN/audio.m4a /tmp/llmwiki-vidN/audio.wav
```
**3. Metadata + thumbnail**
```bash
yt-dlp --cookies-from-browser chrome --skip-download \
  --print '{"title":"%(title)s","author":"%(channel)s","publish_time":"%(upload_date)s","original_id":"%(id)s","duration":"%(duration)s"}' \
  "https://www.bilibili.com/video/BV…/"
yt-dlp --cookies-from-browser chrome --skip-download --write-thumbnail \
  --convert-thumbnails jpg -o "/tmp/llmwiki-vidN/images/cover" "https://www.bilibili.com/video/BV…/"
```
**4. SenseVoice ASR on the wav** — it needs the dedicated ASR venv via
`LLM_WIKI_ASR_PYTHON`, and uses a **whisper-compatible CLI shape** (positional audio
path first, then flags — NOT `--model/--input/--output`):
```bash
LLM_WIKI_ASR_PYTHON=~/.local/share/llm-wiki/asr-venv/bin/python \
  python3 <skill>/scripts/asr_sensevoice.py /tmp/llmwiki-vidN/audio.wav \
  --output_format srt --output_dir /tmp/llmwiki-vidN --language zh
```
SenseVoice outputs `<input-basename>.srt` (e.g. `audio.srt` from `audio.wav`).
- `--output_format srt` is REQUIRED (omitting it → argparse "ambiguous option").
- `--model` / `--initial_prompt` are accepted but ignored (SenseVoiceSmall is fixed).
- Without `LLM_WIKI_ASR_PYTHON` you get `"sensevoice: funasr is not importable"`.

**5. Convert the SRT → `transcript.md`** in the `--from` shape (`# title`, the
`> 作者/发布时间/原文链接` header, `![](images/cover.jpg)`, then a `## 文字转写` body
where each segment is `**[M:SS](<url>?t=<seconds>)** <text>`), then **6.** polish +
`normalize_raw.py --from`.

> `yt-dlp --cookies-from-browser chrome` can hang 10-30 s (or longer) if Chrome holds
> many cookies — closing Chrome or using another browser helps.

## Pitfall: Bilibili m4a AAC corruption — use `--downloader aria2c` first

Bilibili DASH audio streams (format 30280, m4a container) can carry AAC frames that
**ffmpeg's decoder rejects** ("Reserved bit set", "Prediction is not allowed in AAC-LC",
"Invalid data found when processing input"). Two severity levels observed:

**Level 1 — truncation at ~8 min** (older pattern): ffmpeg produces ~8 min from a 34-min
file. `afconvert` (macOS Core Audio) handles this correctly:
```bash
afconvert -f WAVE -d LEI16@16000 audio.m4a audio.wav  # full duration
```

**Level 2 — corruption at ~20 min** (newer pattern, observed 2026-06 on two videos):
ffmpeg truncates at ~20 min (55% of a 36-min video). `afconvert` also fails with
`'bada'` error. The corruption is in the **downloaded m4a itself**, not just the
decoder — lower-quality formats (30232, 30216) have the same corruption at the same
point. No amount of decoder juggling fixes a corrupted source file.

**Root cause:** yt-dlp's default HTTP downloader can produce corrupted m4a files on
Bilibili. The standard download reports 100% and `ffprobe` shows the correct duration,
but the AAC payload is damaged mid-stream.

**Fix: use `--downloader aria2c`** to get a clean download:
```bash
yt-dlp -f 30280 --cookies-from-browser chrome --downloader aria2c \
  -o "/tmp/llmwiki-vidN/audio.m4a" "https://www.bilibili.com/video/BV…/"
```
Then convert with either `afconvert` or `ffmpeg` — both work on clean files:
```bash
afconvert -f WAVE -d LEI16@16000 /tmp/llmwiki-vidN/audio.m4a /tmp/llmwiki-vidN/audio.wav
# OR: ffmpeg -y -i audio.m4a -ar 16000 -ac 1 -c:a pcm_s16le audio.wav
```

**Diagnostic after download — always verify:**
```bash
ffprobe -v error -show_entries format=duration \
  -of default=noprint_wrappers=1:nokey=1 /tmp/llmwiki-vidN/audio.m4a
# Compare to expected duration from metadata. If mismatch → re-download with aria2c.
```

**When to suspect Level 2 corruption:** ffmpeg produces significantly more than 8 min
but less than the full duration (e.g. 20 min from a 36-min file), AND `afconvert`
fails with `'bada'`. Don't waste time trying different decoders — re-download with
`aria2c`.

**SenseVoice's `asr_sensevoice.py` loads audio via `librosa`/`soundfile`, which
cannot read m4a directly** — it raises `soundfile.LibsndfileError: Format not
recognised`. Always convert to wav first:

```bash
# Step 2b: convert m4a → wav
afconvert -f WAVE -d LEI16@16000 /tmp/llmwiki-vidN/audio.m4a /tmp/llmwiki-vidN/audio.wav

# Step 4: SenseVoice ASR on the wav
LLM_WIKI_ASR_PYTHON=~/.local/share/llm-wiki/asr-venv/bin/python \
  python3 <skill>/scripts/asr_sensevoice.py /tmp/llmwiki-vidN/audio.wav \
  --output_format srt --output_dir /tmp/llmwiki-vidN --language zh
```

## Diagnostic: transient AAC corruption — afconvert 'bada' on first download, succeeds on retry

A download that reports 100% and shows the correct `ffprobe` duration can still fail
`afconvert` with `'bada'` **and** truncate in ffmpeg — but a **fresh re-download** of
the same format can produce a clean file. This is **distinct from Level 2 persistent
corruption** (same file always fails regardless of decoder). Observed pattern (2026-06,
Bilibili BV1SP411G7QS, 25-min video):

1. First download with `--downloader aria2c`: 34.94 MiB, ffprobe reports 1513s, but
   `afconvert` → `'bada'`, ffmpeg → 9:55 truncated.
2. Re-download (same command, same format): 34.94 MiB, ffprobe 1513s, `afconvert`
   succeeds cleanly, full 25:14 wav.

**How to tell transient from persistent:** re-download once. If the fresh file passes
`afconvert` and `ffprobe` duration matches, it was transient. If it fails at the same
point, it's Level 2 — switch to a different format quality (30232/30216) or wait and
retry later.

**When Bilibili returns HTTP 412 on retry:** the bot protection kicked in after too many
requests. Options:
- Wait a few minutes and retry with `--cookies-from-browser chrome`
- Use `opencli bilibili download <bvid> --output <dir>` (browser-based, 60s default
  timeout — increase with `--timeout 600`)
- The downloaded mp4 from opencli may also have AAC corruption; extract audio with
  `afconvert` or `ffmpeg` and verify duration

## Pitfall: Bilibili sponsor segments in transcripts

Bilibili creators (especially 正直讲史-李正Str) embed **mid-video sponsored ad
segments** that SenseVoice faithfully transcribes as if they were content. These are
not metadata noise — they are spoken content lasting 1–3 minutes, seamlessly mixed
into the lecture.

**Detection:** after building `transcript.md`, scan for known ad-indicator keywords.
Common patterns for Chinese history channels:
- Product names (海丽生, 海力生, 小蓝瓶, etc.)
- Health/supplement terms (鱼油, omega3, 欧米伽, 高纯度, 甘油三酯)
- Call-to-action phrases (评论区链接, 专属福利, 赠品, 一键三连, 点击链接)
- Shopping event references (618活动, 双11, etc.)

**Handling:** when building `transcript.md` from SRT segments, skip any segment
containing ≥2 ad keywords. Don't strip them from the SRT file (RAW principle) —
filter at the transcript.md construction step. Log how many segments were skipped
for traceability.

**No per-video tables here** — ad patterns grow with the corpus. If a new channel's
ad style isn't covered, add its keywords to the detection list for that session.

## Bilibili metadata: channel name via API

`yt-dlp --print '%(channel)s'` returns "NA" on most Bilibili videos. Use the public
API instead:

```bash
curl -s "https://api.bilibili.com/x/web-interface/view?bvid=BV..." | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data']['owner']['name'])"
```

This reliably returns the UP主 name (e.g. "正直讲史-李正Str"). Pipe to python3 is
safe here — the Bilibili API returns structured JSON, not executable content.

## Pitfall: Bilibili AI captions transcribe outro music/ads as garbage text

Bilibili's AI-generated captions (`transcript_source: captions`) faithfully transcribe
non-speech audio at the end of videos — outro music lyrics, ad jingles, subscribe/like
animation sounds — as nonsensical Chinese text. This is **distinct** from the "transcript
stops at 80-85%" pattern below (which is about ASR producing *no* segments for non-speech
audio). Here the captions *do* produce segments, but they're garbage.

**Detection:** after fetching, read the last 30-50 lines of `transcript.md`. Real speech
ends with a natural sentence; garbage typically starts with disjointed short phrases,
repeated characters, or nonsensical combinations (e.g. "花港无处跨界早餐 / 别撒谎 / 我只跨界高达").
The boundary is usually abrupt — one line is a complete thought, the next is gibberish.

**Fix:** find the last line of real content, truncate everything after it. In Python:
```python
# Find the last meaningful line (heuristic: >5 chars and contains a verb or particle)
for i, line in enumerate(reversed(lines)):
    if len(line.strip()) > 5 and any(c in line for c in '的是在有不了'):
        cutoff = len(lines) - i
        break
lines = lines[:cutoff]
```
Or manually identify the boundary (faster for a 16-min video with ~860 lines).

**Why `clean_md.py` doesn't catch this:** the garbage is content-level, not structural
markdown damage. It looks like legitimate transcript lines — just nonsensical ones.

This pattern is common on Bilibili but rare on YouTube (YouTube captions typically end
cleanly when speech ends).

## Pitfall: transcript stops at ~80-85% of video length (non-speech outro)

SenseVoice / Whisper transcripts for longer Bilibili videos (30+ min) often stop at
80-85% of the total duration. This is usually **not** a truncation bug — it's the
speaker transitioning to a non-speech outro (background music, preview clips, end
screen with subscribe/like animations). ASR engines produce no segments for non-speech
audio.

**Diagnostic:** if `transcript.md` ends at e.g. 33:00 for a 40:04 video, and the last
segment is a natural sentence ending (not mid-word), the coverage is normal. Compare:
- Normal: 33 min of speech in a 40-min video (82%) — outro is music/previews
- Abnormal: 5 min of speech in a 25-min video (20%) — partial download or audio corruption

**Rule of thumb:** Chinese speech ≈ 300-600 chars/min. For a 40-min video:
- <5,000 chars → likely truncated, investigate
- 7,000-15,000 chars → normal coverage, polish and proceed
- Don't re-run ASR just because the transcript is shorter than the video duration

**When to worry:** if the last segment ends mid-sentence or mid-word, or if the
char count is far below the expected range. In those cases, check audio duration
with ffprobe and consider re-downloading.

## Pitfall: missed ASR corrections after normalize — delete + re-normalize

RAW is immutable. If you discover ASR errors **after** `normalize_raw.py` has already
written the file (e.g. you missed a variant like 陈廷静 vs 陈廷敬), you cannot edit the
RAW in place. The fix:

1. Fix the corrections in the temp dir's `transcript.md`
2. **Delete** the existing RAW file and its localized asset:
   ```bash
   rm <wiki>/raw/sources/video/YYYY-MM-DD-<slug>.md
   rm <wiki>/raw/assets/YYYY-MM-DD-<slug>--cover.jpg 2>/dev/null
   ```
3. Re-run `normalize_raw.py --from <same temp dir>` with identical flags

The script will create a fresh file (not `-v2`, since the original is gone).

**Prevention: run a completeness check BEFORE normalizing.** After building
`transcript.md`, grep for all known variants of each character name. A name like
陈廷敬 can produce 7+ SenseVoice variants (陈廷净/陈婷静/陈情净/陈宁静/陈亭静/陈婷净/陈廷静).
Use `sed -i ''` for bulk replacement — it's faster and more reliable than `patch` for
many-to-one corrections:

```bash
# Replace all variants at once
sed -i '' 's/陈廷净/陈廷敬/g; s/陈婷静/陈廷敬/g; s/陈情净/陈廷敬/g' transcript.md
# Verify zero remaining
grep -c '陈廷净\|陈婷静\|陈情净' transcript.md  # should be 0
grep -c '陈廷敬' transcript.md  # should be 30-100+
```

**Order matters:** apply longer/more-specific variants before shorter ones to avoid
partial matches. The ASR correction glossary at `data/asr-corrections.zh-history.md`
lists variants per video — check it before starting the polish pass.

## Bilibili-specific issues

See `references/sources.md` → "Pitfalls — Bilibili": yt-dlp HTTP 412 bot protection,
0 official subtitles on most videos (check the API first), the Bilibili public API as
a metadata fallback, and Whisper model-cache availability.
