---
name: my-llm-wiki-video
description: >-
  Capture ONLINE VIDEO content into an LLM-WIKI immutable RAW layer as a faithful,
  timestamped transcript with jump-back links and a localized cover, while keeping
  the source URL and never retaining the video file. Use together with my-llm-wiki
  whenever the user wants to save, archive, clip, distill, or “沉淀” a YouTube,
  Bilibili/B站, Douyin/抖音, Xiaohongshu/小红书 video, or another online video into
  their wiki or knowledge base; also use when they ask for video-to-RAW, captions
  or local-ASR ingestion, transcript translation, or preservation of a video's
  content in the wiki. Do not use merely to summarize/analyze a video, transcribe
  it for temporary reading, or download/rip the video file as mp4.
---

# my-llm-wiki-video — online video to timestamped RAW

Capture the video's spoken content, provenance, and cover. Produce a faithful
transcript rather than a summary. Keep the URL as the original; delete temporary
audio after transcription and never retain the video file.

This skill owns the video SOP and its platform variants. The sibling
`my-llm-wiki` skill owns wiki routing, the Adapter/RAW contracts, normalization,
and capture-health checks.

## Locate the shared core

Resolve the sibling core before starting:

```bash
VIDEO_SKILL="<this skill directory>"
CORE_SKILL="$(cd "$VIDEO_SKILL/../my-llm-wiki" && pwd)"
test -f "$CORE_SKILL/scripts/normalize_raw.py"
```

If the check fails, stop and install `my-llm-wiki`; do not duplicate or improvise
the core. Suite installs declare this runtime dependency and install it
automatically.

## Workflow

1. **Read the complete SOP.** Always read
   `references/video-capture-sop.md`. For Bilibili, Douyin, or Xiaohongshu also
   read the matching platform reference listed below. Read
   `references/video-asr.md` only when the SOP lands on the no-captions path
   (Path B) — the captions path never needs it.
2. **Probe capability.** Run
   `python3 "$CORE_SKILL/scripts/preflight.py" --profile capture.video` and
   inspect `capabilities.capture.video`.
   Use captions first; when captions are absent, require audio download,
   `ffmpeg`, and a language-appropriate local ASR backend. Relay install hints
   with their project URLs. When installs are blocked or slow, use `cn-mirrors`.
3. **Probe metadata without executable pipes.** Run
   `python3 "$VIDEO_SKILL/scripts/video_probe.py" --url <url> --output <temp>/metadata.json`.
   Retry with `--cookies-from-browser <browser>` only after a real auth/bot wall
   and explicit permission to read that browser's login state. Do not pipe
   yt-dlp/opencli/network output into an interpreter.
4. **Build the temp acceptance shape.** Produce a fresh directory containing
   `transcript.md`, `images/cover.jpg`, and `status.yaml` as specified by the SOP.
   Fetch caption tracks with the shipped `scripts/caption_fetch.py` — the
   payload and a normalized `subs.srt` land in the temp dir; only its compact
   JSON summary enters the conversation. Never dump or pipe the fetched
   captions into context to inspect them.
   For local ASR, invoke the shipped runners (`scripts/sensevoice_to_srt.py`
   for Chinese, `scripts/faster_whisper_to_srt.py` for everything else); never
   copy their implementation into a temporary script or an arbitrary-code tool. Write semantic transcript repairs as data
   with the runtime's normal file-write tool.
   Convert subtitle cues with
   `python3 "$VIDEO_SKILL/scripts/srt_to_anchors.py" ...`. Never normalize the
   converter's bare intermediate output.
5. **Handle long work safely.** Run long ASR in the background and poll its
   `status.yaml`; do not rely on a foreground tool timeout or passive notification.
   When the same turn will wait for the result, explicitly disable asynchronous
   completion delivery (`notify_on_complete=false` in Hermes). After the matching
   status appears, wait/reap the retained process handle and confirm it exited
   before reporting success; a status-file check or read-only process poll alone
   is not a lifecycle barrier. Confirm the status belongs to the requested video
   before reading the transcript.
6. **Polish faithfully.** Repair punctuation, paragraphing, and obvious ASR
   errors without summarizing or dropping content. Preserve every timestamp anchor
   exactly. For non-Chinese videos, append a full `## 中文译文` with the same anchors.
7. **Verify before commit.** Confirm URL/ID/title, duration plausibility,
   transcript length, timestamp coverage, and cover presence. Stop on an error or
   stub rather than writing degraded RAW silently.
8. **Resolve the wiki.** Apply the sibling core's routing policy using title,
   channel, description, and transcript. Pass the chosen wiki explicitly when
   auto-classification was used.
9. **Normalize once.** Commit the verified temp directory through the shared core:

   ```bash
   python3 "$CORE_SKILL/scripts/normalize_raw.py" --from <temp-dir> \
     --source-type video --wiki <wiki-root> \
     --title "<video title>" --source-url "<canonical url>" \
     --original-id "<platform id>" --author "<channel>" \
     --publish-time "<publish date>" \
     --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   ```

   Require a meaningful title and at least one localized asset (the cover).
   Surface every `capture_health` warning.
10. **Report and optionally synthesize.** Report the wiki, RAW path, transcript
   source, timestamp/translation status, and cover. For capture-only intent, leave
   it pending. For explicit synthesis intent, hand the exact wiki root and new RAW
   path to `my-llm-wiki-maintainer`; do not start a second ingest path when the
   desktop app is already watching the wiki.

## Invariants

- Preserve timestamp anchors through transcription, polish, translation, and RAW.
- Keep only the source URL, transcript, and cover; delete temporary audio/video.
- Treat `status: error`, wrong metadata, implausible duration, or a short stub as
  failure, not a capture.
- Never edit an existing RAW file. Recapture to a versioned file instead.
- Surface auth/login steps rather than scraping around a platform wall.

## Reference map

- `references/video-capture-sop.md` — acceptance shape, captions-first
  decision, anchor assembly, verification, and platform index.
- `references/video-asr.md` — the no-captions fallback: audio download,
  language→backend routing gate, VAD-first SenseVoice runner, background
  discipline for long ASR runs, audio-specific checks. Read only on Path B.
- `references/video-bilibili-pitfalls.md` — Bilibili-specific traps.
- `references/video-douyin.md` — Douyin share-link recipes and dead ends.
- `references/video-xiaohongshu.md` — Xiaohongshu video-note recipe and dead ends.
- Sibling `my-llm-wiki/references/routing.md` — shared multi-wiki routing.
- Sibling `my-llm-wiki/references/raw-contract.md` — normative video RAW shape.
