# Douyin (抖音) — share-link video capture

Platform notes for `references/video-capture-sop.md` — the acceptance contract,
ASR recipe, assembly, long-run discipline, and verification all live there;
this file is only what Douyin adds on top.

Field-tested on the same live video by two independent agents: with opencli the
fetch took 4 commands; without it, ~14 approaches failed before the mobile
share page worked. This file encodes both working recipes and the dead ends
so the next capture is one straight line. Douyin has **no caption track** —
it is always Path B (audio → ASR; the content is almost always zh → SenseVoice).

**Step 0 — resolve the share link.** App shares look like
`https://v.douyin.com/<code>/`:

```bash
curl -sIL -o /dev/null -w '%{url_effective}\n' 'https://v.douyin.com/<code>/'
# → https://www.douyin.com/video/<aweme_id>?…      (aweme_id = the 19-digit id)
```

Canonical `source_url` is `https://www.douyin.com/video/<aweme_id>`;
`original_id` is the aweme_id.

## Recipe A — opencli (preferred when installed and logged in)

The douyin adapter has **no fetch-by-URL command** — compose from what exists.
Check `opencli douyin whoami` first; surface `opencli douyin login` if logged out.

```bash
opencli douyin search '<title keywords>' -f json
#   confirm the video + author exist (search results carry NO play address)
opencli web read --url 'https://www.douyin.com/video/<aweme_id>'
#   → saves the page as markdown; grep the author block for /user/<sec_uid>
#     (the same markdown also carries the publish time)
opencli douyin user-videos '<sec_uid>' --with_comments false -f json
#   → per item: aweme_id, title, duration (s), play_url (signed CDN mp4)
```

Match your aweme_id in the `user-videos` output and take its `play_url`.
Caveats: `user-videos` returns the author's **latest ≤20** — an older video
falls through to Recipe B; the `play_url` is signed and expires within
minutes — download immediately.

## Recipe B — mobile share page (no opencli, no login)

The **mobile** share page embeds full metadata including an unsigned play
address. The shipped helper sends the required iPhone UA, bypasses local
proxies for the China-domestic CDN, and parses `_ROUTER_DATA` as data:

```bash
python3 "$VIDEO_SKILL/scripts/douyin_probe.py" --aweme-id "$AWEME_ID" \
  --output "$VIDEO_WORKDIR/metadata.json"
```

Do not reproduce the parser as inline Python. The helper preserves CJK without
`unicode_escape` mojibake and returns title, author, sec_uid, create time,
duration, play URL, and cover URL.

The `playwm` play address is watermarked — irrelevant here, only the audio is
used. Download it with the same mobile UA + `Referer: https://www.douyin.com/`.

## Common tail (both recipes)

- Download the mp4 (`curl -sL`), then `ffprobe` its duration against the
  metadata duration (`video-asr.md` §5) before spending ASR time on it.
- Extract a validated 16 kHz mono WAV with `audio_to_wav.py`, take the cover
  from frame 0, and retain the mp4 as a recovery input until
  `commit_capture.py` verifies RAW:
  ```bash
  python3 "$VIDEO_SKILL/scripts/audio_to_wav.py" video.mp4 audio.wav
  ffmpeg -v error -i video.mp4 -frames:v 1 -q:v 3 images/cover.jpg
  ```
  `commit_capture.py` removes the staged mp4/audio only after successful
  normalization. On any assembly or normalize failure, reuse them.
  (Recipe B's `cover_url` serves **WebP** regardless of extension — if you use
  it instead of frame 0, convert: `ffmpeg -i cover.webp cover.jpg`.)
- Run the VAD-first SenseVoice recipe (`references/video-asr.md` §3), then SOP §3
  `srt_to_anchors.py --url 'https://www.douyin.com/video/<aweme_id>'` —
  anchors get best-effort `?t=<sec>s` links (the Douyin web player ignores
  them today, but the MM:SS text still indexes the video).
- The anchored output is an **intermediate** — assemble the SOP §1
  `transcript.md` around it (real `# title` H1, `>` header, cover, 简介)
  before normalizing. Never feed `anchored.md` straight to `--md`: that mints
  a RAW titled `anchored` with no cover, and `normalize_raw.py` now refuses it.

## Dead ends — do not retry (each burned real minutes in a live capture)

- **yt-dlp's Douyin extractor**: `ERROR: Fresh cookies … are needed` — with
  and without `--cookies-from-browser`, across yt-dlp versions; upgrading
  yt-dlp does not fix it, nor do fabricated `__ac_nonce`/`s_v_web_id` cookies.
- **Un-signed official web APIs**: `/aweme/v1/web/aweme/detail` returns an
  empty body; `/web/api/v2/aweme/iteminfo` returns `encrypt_data_miss`;
  `/oembed` carries no play data. All want X-Bogus-style request signing —
  not worth reimplementing.
- **Third-party resolvers**: TikWM rejects douyin.com URLs; cobalt v7 is shut
  down and v10 requires auth.
- **Generic browser automation on www.douyin.com** (navigate + scrape): page
  loads time out under anti-bot; only an already-authenticated adapter session
  (Recipe A) gets through reliably.
