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
address. Request it with an iPhone UA — a desktop UA gets an empty JS shell —
and bypass proxies (China-domestic CDN):

```python
import json, re, requests
aweme = "<aweme_id>"
s = requests.Session(); s.trust_env = False            # drop any http(s)_proxy
r = s.get(f"https://www.iesdouyin.com/share/video/{aweme}/", timeout=15, headers={
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"})
r.encoding = "utf-8"
raw = re.search(r"window\._ROUTER_DATA\s*=\s*({.*?})\s*</script>", r.text, re.DOTALL).group(1)
# parse raw as-is — running it through .decode('unicode_escape') mojibakes the CJK (live incident)
item = json.loads(raw)["loaderData"]["video_(id)/page"]["videoInfoRes"]["item_list"][0]
meta = {
    "title":       item["desc"],
    "author":      item["author"]["nickname"],
    "sec_uid":     item["author"]["sec_uid"],
    "create_time": item["create_time"],                       # publish time, epoch seconds
    "duration_ms": item["video"].get("duration") or item.get("duration"),
    "play_url":    item["video"]["play_addr"]["url_list"][0], # …/aweme/v1/playwm/?video_id=… → 302 to CDN mp4
    "cover_url":   (item["video"].get("cover") or item["video"]["origin_cover"])["url_list"][0],
}
```

The `playwm` play address is watermarked — irrelevant here, only the audio is
used. Download it with the same mobile UA + `Referer: https://www.douyin.com/`.

## Common tail (both recipes)

- Download the mp4 (`curl -sL`), then `ffprobe` its duration against the
  metadata duration (SOP §5) before spending ASR time on it.
- Extract audio, take the cover from frame 0, and **delete the mp4** — the
  media is never kept:
  ```bash
  ffmpeg -v error -i video.mp4 -ar 16000 -ac 1 audio.wav
  ffmpeg -v error -i video.mp4 -frames:v 1 -q:v 3 images/cover.jpg
  rm video.mp4
  ```
  (Recipe B's `cover_url` serves **WebP** regardless of extension — if you use
  it instead of frame 0, convert: `ffmpeg -i cover.webp cover.jpg`.)
- Run the SOP §2 VAD-first SenseVoice recipe, then SOP §3
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
