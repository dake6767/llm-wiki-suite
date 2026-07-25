# Bilibili — field-tested pitfalls

Platform notes for `references/video-capture-sop.md` — the recipes themselves
are the SOP's Path A/B (Bilibili often *has* captions, including the AI `ai-zh`
track, so try Path A first); these are the traps Bilibili adds on top. Generic
any-platform pitfalls stay in the SOP §5; the ASR-path recipe and audio checks
are in `references/video-asr.md`.

- **`-x` is not audio-only on Bilibili.** `yt-dlp -x` can pick a merged
  video+audio DASH format (280 MiB, 30-min download) whose audio extraction
  silently truncates to ~2 min. **Force an audio-only stream id.** The §2
  `video_probe.py` metadata already carries `audio_format_id` (best lossy
  audio-only stream, Hi-Res FLAC / Dolby deprioritized) plus a ranked
  `audio_formats` list — pass it straight to `yt-dlp -f <audio_format_id> …`,
  no separate `--list-formats` pass needed. Manual fallback if the probe found
  nothing (or you want a different tier):
  `yt-dlp --cookies-from-browser chrome --list-formats <url> | grep audio` →
  `yt-dlp -f <audio-id> …`. Always ffprobe the result against the expected
  duration.
- **One audio format can resolve to a dead CDN while the next one works.**
  Connection refused, timeout, TLS, and HTTP 5xx errors are transport failures:
  retry the next ranked entry from `video_probe.py`'s `audio_formats` list with
  a fresh output name, then ffprobe it. Do not bake a format id or CDN port from
  one incident into the Skill. HTTP 401/403/412, login, or geo failures are
  access failures instead; do not churn through every format or silently add
  browser cookies.
- **m4a AAC corruption → download with `--downloader aria2c`.**
  yt-dlp's default HTTP downloader can produce m4a files whose AAC payload is
  damaged mid-stream (ffmpeg truncates; macOS `afconvert` errors `'bada'`).
  aria2c downloads are clean. The official `toolchain-base` Provider ships
  aria2c next to yt-dlp, and its yt-dlp runner puts that private binary on the
  child PATH, so `--downloader aria2c` resolves without a global install;
  system or custom Providers must expose both commands in the same execution
  environment. Corruption can also be *transient* — one fresh
  re-download is worth trying before switching formats. A damaged m4a
  truncates **everything downstream** at the damage point — ffmpeg
  conversion, funasr's internal loader, a VAD pass (one 67-min capture
  "stopped" at 20 min this way and was misdiagnosed as a VAD length limit;
  the fraction-of-duration check in `video-asr.md` §5 is the tell).
- **HTTP 412 = Bilibili bot protection.** Wait a few minutes, retry with
  `--cookies-from-browser chrome`. `--cookies-from-browser` itself can hang
  10-30 s when Chrome holds many cookies — close Chrome or use another browser.
- **Channel name:** `yt-dlp --print '%(channel)s'` returns "NA" on most
  Bilibili videos; use the public API:
  `curl -s "https://api.bilibili.com/x/web-interface/view?bvid=BV…"` →
  `.data.owner.name`.
- **AI-caption outro garbage:** Bilibili AI captions faithfully "transcribe"
  outro music/jingles as nonsense text. Read the last 30-50 lines; truncate
  after the last real sentence. (Distinct from the normal ~80-85 % non-speech
  outro cutoff, where ASR simply emits nothing.)
- **A cookie-less download can silently truncate** — 2 min fetched of a 25-min
  video, ASR reports `ok` on the fragment. The audio-duration check in
  `video-asr.md` §5 catches this; never skip it on Bilibili.
