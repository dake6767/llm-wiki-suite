# Local ASR fallback — no captions: audio-only download → VAD-first timestamped SRT

Read this file only when the capture SOP's §2 probe found **no usable caption
track** (`caption_fetch.py` exited 2), or the platform file says the host never
has one (抖音, 小红书). It owns everything specific to the ASR path: the
audio-only download, the language→backend routing gate, the shipped ASR
runners (VAD-first SenseVoice for Chinese, faster-whisper for the rest), the
background+poll discipline for the minutes-long run, and
the audio-specific verification and pitfalls. The acceptance contract (§1),
cue→anchor assembly (§3), and content verification (§4) stay in
`video-capture-sop.md` — this file's output is a standard SRT that feeds
`scripts/srt_to_anchors.py` unchanged.

## Contents

- [1. Audio-only download](#1-audio-only-download)
- [2. Route the ASR backend by language](#2-route-the-asr-backend-by-language-before-transcribing)
- [3. ASR → timestamped SRT: the shipped runners](#3-asr--timestamped-srt-the-shipped-runners)
- [4. Long-run discipline: background + poll one status file](#4-long-run-discipline-background--poll-one-status-file)
- [5. Audio-specific verification and pitfalls](#5-audio-specific-verification-and-pitfalls)

---

## 1. Audio-only download

Apply the loaded SKILL.md Provider Resolver rule to every external command
below. Use `tool_exec.py --capability capture.video.captions yt-dlp -- ...`
and `tool_exec.py --capability media.extract-audio ffmpeg -- ...` on every
platform. Download audio only with yt-dlp (never the video), e.g.
`yt-dlp -x --audio-format mp3 -o "<tmp>/audio.%(ext)s" <url>` — but prefer the
`audio_format_id` the §2 probe (`video_probe.py`) already picked from the
metadata's `formats` and force it: `yt-dlp -f <audio_format_id> -o "<tmp>/audio.%(ext)s" <url>`.
This avoids the Bilibili format trap in `video-bilibili-pitfalls.md`, where a
bare `-x` grabs a merged DASH stream whose audio silently truncates. If the
probe returned no `audio_format_id` (rare extractor gap), fall back to listing
formats manually as that file describes.

**Convert m4a → wav for Python-API ASR backends** (soundfile can't
read m4a): `ffmpeg -i audio.m4a -ar 16000 -ac 1 audio.wav` anywhere, or
`afconvert -f WAVE -d LEI16@16000 audio.m4a audio.wav` (macOS-only tool —
don't cite it in cross-platform notes).

## 2. Route the ASR backend by language BEFORE transcribing

Guess from the title/author (a title with ≥4 CJK chars and more CJK than latin
⇒ Chinese), or honor an explicit language:

- **Chinese → SenseVoice** (FunASR `SenseVoiceSmall`,
  [github.com/FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice)
  via [FunASR](https://github.com/modelscope/FunASR)): ~15× faster than
  Whisper on CPU and far better on Chinese proper nouns. The runner resolves
  the `python.asr-zh` Provider and relaunches under its isolated Python. If none is
  available, offer `my-llm-wiki ensure-pack asr-zh`; model weights remain a
  first-use download from ModelScope, a domestic CDN.
  It reads wav (not m4a — convert first, §1) and has **no native
  timestamps** — feeding it the whole file returns ONE cue stamped
  `00:00:00,000 --> 00:00:00,000` (a documented incident re-ran a 40-min
  ASR twice before noticing). **Passing `vad_model=` into the same
  `AutoModel` (the combined pipeline) does not fix it** — that pipeline
  concatenates all segments into one untimed blob regardless of how
  `merge_vad` / `merge_length_s` are set (a Windows capture burned three
  full attempts on those knobs). Timestamps come from a **VAD-first**
  pass — VAD run as its *own* model — the shipped runner in §3.
- **Everything else → faster-whisper**
  ([github.com/SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper);
  the CTranslate2 reimplementation — same models ~3-5× faster, makes
  `large-v3` affordable on CPU). It is a Python **library with no CLI**:
  resolve the `python.asr-other` Provider and run the shipped runner in §3,
  never an ad-hoc inline snippet. If none is available, offer
  `my-llm-wiki ensure-pack asr-other`. faster-whisper pulls its models from
  Hugging Face; the official Provider supplies its scoped mirror environment
  and the runner applies it at model load. Unlike the
  SenseVoice path it decodes
  m4a/mp3 itself (PyAV) — no wav conversion needed — and its segments carry
  native timestamps, so there is no VAD-first trick to apply.
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

Backend chosen, three rules that apply to every backend:

1. **Emit SRT, not plain text**: the shipped runners (§3) write SRT natively;
   the stock `whisper` CLI needs `--output_format srt`. Per-cue timestamps are
   the deep-link index — a plain `.txt` loses the entire anchor layer.
2. **Prime the decoder with the video's own vocabulary.** Whisper mangles
   code-switching and domain terms on smaller models ("token" → "偷肯",
   "GPT" → "吉皮提"). Pass `--initial-prompt` (stock whisper CLI:
   `--initial_prompt`) built from the video's **title +
   keywords/tags + first description line** (≤ ~600 chars) — free, and it
   travels with every video. Model size is the other big lever: `medium` is the
   floor, `turbo`/`large-v3` for term-dense content.
3. **Delete the audio when done.** Transcription is local and free; the media
   is never kept.

## 3. ASR → timestamped SRT: the shipped runners

Both local backends run through a reviewed script shipped with this skill —
run it directly with the dedicated ASR interpreter, ask the runtime to
background the command rather than generating another wrapper script, and do
not reproduce either script's source in a heredoc, inline interpreter flag, or
arbitrary-code tool.

Launch the shipped script with any Python capable of parsing its arguments; the
script immediately resolves `python.asr-zh` or `python.asr-other` and relaunches
under that Provider before importing the ASR library. On Windows this uses a
shell-free child process rather than `os.execve`, which avoids a Git Bash/MSYS
crash while preserving inherited stdio and the child exit code. Pass
`--provider <id>` only when the user chose a Provider for this task. Do not
select a shared Python or construct `PYTHONPATH` by hand.

### 3a. SenseVoice (Chinese) — VAD-first

The order is the whole trick: **run VAD first, then recognise each speech
segment separately — the cue time is the VAD segment's bounds.** Never hand
SenseVoice the full audio and hope for cue times; it is non-autoregressive and
will return a single untimed blob (see the bullet above):

```bash
python3 "$VIDEO_SKILL/scripts/sensevoice_to_srt.py" \
  "$TMPDIR/audio.wav" "$TMPDIR/transcript.srt" \
  --status "$TMPDIR/status.yaml" --language zh \
  --source-url "$URL" --original-id "$VIDEO_ID"
```

The script runs VAD first, recognises one bounded segment at a time, atomically
writes SRT, strips SenseVoice rich markers with escaped Unicode ranges, and
writes `status.yaml` as its last act.

Notes that earn their keep:

- The runner reads the already-normalized WAV directly with `soundfile`.
  Do not replace it with `librosa.load`: its numba-backed path may clean a
  large JIT cache and trip Agent safe-delete guards before ASR starts.
- `max_single_segment_time=30000` caps VAD segments at 30 s — SenseVoice's
  sweet spot, and conveniently the anchor granularity SOP §3 wants.
- `use_itn=True` gives punctuation + inverse text norm;
  `rich_transcription_postprocess` + the emoji strip remove SenseVoice's
  `<|zh|><|HAPPY|>` tags and 😊🎵 markers — without this the RAW body is
  littered with them (also a live incident).
- The `[[0, len(wav)//16]]` fallback covers VAD finding nothing (rare:
  music-only or very short clips) — you still get one honest cue.
- The output is a standard SRT, so SOP §3's `srt_to_anchors.py` consumes it
  unchanged.
- ~28× realtime on CPU: a 40-min video ≈ 2 min VAD+ASR. If it's taking tens of
  minutes, something is wrong — check you didn't feed the full file per cue.

### 3b. faster-whisper (everything else)

No VAD-first choreography here — Whisper is autoregressive and its segments
carry native timestamps; the runner just adds Silero VAD filtering (suppresses
hallucinated cues in silence), SRT assembly, and the atomic status contract:

```bash
python3 "$VIDEO_SKILL/scripts/faster_whisper_to_srt.py" \
  "$TMPDIR/audio.m4a" "$TMPDIR/transcript.srt" \
  --status "$TMPDIR/status.yaml" --model medium \
  --initial-prompt "<title + keywords + first description line>" \
  --source-url "$URL" --original-id "$VIDEO_ID"
```

- The audio can be the original m4a/mp3 — faster-whisper decodes via PyAV, no
  wav conversion needed.
- `--language` is optional (auto-detect by default); pass it when known — it
  skips detection and removes one misrouting risk.
- `--model medium` is the floor (§2 rule 2); use `large-v3` or `turbo` for
  term-dense content. Models download from Hugging Face on first use —
  mainland networks need `HF_ENDPOINT=https://hf-mirror.com` on that first
  run.
- The output is the same standard SRT + `status.yaml` shape as §3a, so SOP
  §3's `srt_to_anchors.py` and the §4 polling discipline apply unchanged.

## 4. Long-run discipline: background + poll one status file

Captions return in seconds, but an ASR pass takes **minutes to tens of
minutes** on CPU and exceeds single-command timeouts (some runtimes kill any
one command at 300 s). The universal contract:

- **Launch the download+ASR as a non-blocking job** (plain shell: `nohup … &`;
  other runtimes: their background-exec primitive) that writes a status file
  (e.g. `status.yaml`) as its **last** act, atomically.
- **Choose exactly one completion owner.** For the normal capture-and-organize
  flow, the current turn owns the job: launch with asynchronous completion
  delivery disabled (`notify_on_complete=false` in Hermes) and poll/wait it
  yourself. Enable a completion push only when intentionally ending the turn
  before the job finishes and a later follow-up is desired. Do not combine a
  push with active polling; Hermes will otherwise queue a second agent turn even
  if the result was already consumed and reported.
- **Poll that file yourself** with short commands until it appears. Do **not**
  rely on the runtime's completion notification — observed failure: an agent
  waiting to be *told* sat idle long after the transcript was ready. Batch the
  waiting into bounded sleeps (e.g. `sleep 60` then read the status file in
  one command) rather than dozens of instant checks — every check is a full
  model call carrying the whole session prefix.
- **Reap before the final answer.** Once the matching status file appears, wait
  on or read the retained process handle until it is terminal, then verify the
  output. In Hermes, `process poll` is intentionally read-only and does not
  consume a queued completion notification; a timed-out `process wait` also does
  not prove exit. Never send the capture/synthesis conclusion while a self-owned
  process can still generate a delayed notification.
- **Fresh temp dir per capture, always** — `mkdir -p` does not clean an
  existing dir, and a poller that reads a *previous* run's `status.yaml` /
  `transcript.md` will polish and ingest the **wrong video** (a real, documented
  incident). Always allocate `/tmp/llmwiki-vid-$(date +%s)`; do not reuse and
  recursively clear a shared directory. Keep the status file inside that fresh
  directory.
- Backgrounding changes nothing about speed — only how promptly you *notice*
  completion. Poll every ~30–60 s; a long video legitimately takes 10–25 min
  (SenseVoice: a 28-min zh video ≈ 5 min; whisper `medium` on CPU: the same
  video ≈ 55 min — install faster-whisper or use `turbo`).
- **Terminate only the process you launched.** Retain the runtime process handle
  or exact PID and stop that one when cancelling. Never sweep processes by name;
  it can kill unrelated ASR work from another capture.

## 5. Audio-specific verification and pitfalls

Run these **before** the SOP §4 content checks — they catch bad *inputs* that
make every downstream check lie:

- **Audio duration matches video duration** (`ffprobe -v error -show_entries
  format=duration …`) — a cookie-less Bilibili download can silently fetch 2 min
  of a 25-min video and ASR happily reports `ok` on the fragment.
- **VAD/ASR stopping cleanly at a *fraction* of a long video** (one capture:
  202 cues ending at 20 min of a 67-min video), especially with AAC/decode
  errors anywhere earlier in the logs, means the **downloaded audio itself is
  truncated or corrupt** — every consumer (ffmpeg convert, funasr's internal
  loader, a VAD pass) stops at the damage point. Re-download (Bilibili:
  `--downloader aria2c`, see its pitfalls file) and re-check duration with
  ffprobe. It is NOT a VAD length limit — fsmn-vad handles 40+ min fine —
  so don't reach for chunked-VAD workarounds before ruling out a bad file.
- **Never mutate PATH as a workaround.** Resolve FFmpeg through
  `tool_exec.py --capability media.extract-audio`. A damaged official Provider
  calls for `my-llm-wiki repair`; an absent one can be installed with
  `my-llm-wiki ensure-pack toolchain-base` after approval, or replaced by an
  explicitly selected system/custom Provider.
