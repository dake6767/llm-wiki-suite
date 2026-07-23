# Video capture SOP — online video → RAW transcript (bring your own fetcher)

This skill does **not** ship a video fetcher. A video capture is an ordinary
adapter job (sibling `my-llm-wiki/references/adapter-contract.md`): lay the acceptance shape below
down in a temp dir with whatever tools this machine has, then hand off to
`normalize_raw.py`. This file is the scenario SOP — the acceptance contract,
the captions-first decision order, the §3 assembly script
(`scripts/srt_to_anchors.py`, shipped — that step is deterministic and
stack-independent), and the generic field-tested pitfalls. The no-captions
fallback (audio download → local ASR, with its VAD-first recipe and
background-run discipline) lives in `references/video-asr.md` — read it only
when §2 lands on Path B. Platform-specific recipes and dead ends (Bilibili,
抖音, 小红书) live in sibling `references/video-*.md` files — the index is in
§5. Everything a capture needs is on this page, in the file it points to, or
in `scripts/`.

The scenario: the user wants a video's **content** in RAW with the link kept —
**never** the video file, and never the audio after transcription. The faithful
"original" is the **URL**; the body is a timestamped **transcript** (a lossy text
extraction, like a `doc`'s markitdown text). source_type = `video`.

## Contents

- [1. Acceptance contract](#1-the-acceptance-contract-any-pipeline-must-produce-this)
- [2. Captions-first decision](#2-probe-then-take-the-cheapest-path)
- [3. Cue-to-anchor assembly](#3-cues--anchored-transcript-the-assembly-step)
- [4. Verification](#4-verify--before-and-after-normalizing)
- [5. Platform pitfalls](#5-field-tested-pitfalls--the-platform-index)

---

## 1. The acceptance contract (any pipeline must produce this)

A finished capture is a temp dir in the `--from` folder shape:

```
/tmp/llmwiki-vid-<unique>/
  transcript.md        # see layout below
  images/cover.jpg     # the thumbnail — the ONLY stored media
  status.yaml          # completion signal when running backgrounded (video-asr.md §4)
```

`transcript.md` layout (full spec: sibling
`my-llm-wiki/references/raw-contract.md` → "Online video"):

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

Probe before fetching and apply the Provider Resolver rule from both loaded
SKILL.md files to every command in this reference. Run
`python3 <core-skill>/scripts/preflight.py --profile capture.video` on every
platform. It reports the selected Provider for each capability and the official
fallback pack when one is missing. Do not install dependencies ad hoc as part
of the capture; select another healthy Provider or, with the user's approval,
run the returned `my-llm-wiki ensure-pack <id>` command. Then:

### Path A — captions (free, no download, seconds)

Most YouTube and many Bilibili videos have caption tracks (official or
auto-generated — Bilibili's AI `ai-zh` track counts). Fetch them **disk-first**
with the shipped wrapper — it tries opencli when installed (drives the
logged-in browser, so it sidesteps yt-dlp's "Sign in to confirm you're not a
bot" wall), falls back to yt-dlp, lands the raw payload plus a normalized
`subs.srt` in the temp dir, and prints only a compact JSON summary (tool used,
subs path, cue count, text chars, first/last anchor):

```bash
python3 "$VIDEO_SKILL/scripts/caption_fetch.py" --url "$URL" --out "$TMPDIR"
```

- **exit 0 / `status: ok`** — captions are on disk; feed the reported `subs`
  file straight to §3. The summary's `cues` / `text_chars` already cover the
  §4 char-count sanity check.
- **exit 2 / `status: no-captions`** — a tool ran fine and proved there is no
  caption track: this is the Path B branch signal.
- **exit 1 / `status: error`** — captions are *unknown*, not absent (bot wall,
  auth, missing tools). Fix the named cause first — behind an auth/bot wall
  retry with `--cookies-from-browser <browser>` only after the user allows
  browser-login access — do not fall through to ASR and hit the same wall on
  the audio download.

**Never re-fetch the payload with an ad-hoc pipe to inspect it** — a measured
ad-hoc `opencli … transcript | head -200` put ~11KB of caption JSON into the
agent context and re-billed it on every later call that session. The summary
plus bounded checks on the on-disk files is all §4 needs. The same rule as
everywhere in this suite: fetched output is data, saved to a file, never piped
into an interpreter.

Metadata: use the shipped approval-clean wrapper, which invokes yt-dlp with
an argv list and parses JSON in-process:

```bash
python3 "$VIDEO_SKILL/scripts/video_probe.py" --url "$URL" \
  --output "$TMPDIR/metadata.json"
```

On a confirmed auth/bot wall, retry with `--cookies-from-browser chrome` only
after the user allows browser-login access. Its `subtitle_languages` /
`automatic_caption_languages` fields tell you up front whether Path A can
succeed; on Path B, `audio_format_id` is the pre-picked audio-only stream to
pass to `yt-dlp -f` (with the ranked `audio_formats` list as backup) — no
separate `--list-formats` pass needed. Thumbnail:
`yt-dlp --skip-download --write-thumbnail --convert-thumbnails jpg -o "<tmp>/images/cover" <url>`.

### Path B — no captions: audio-only download + local ASR

`caption_fetch.py` exited 2 (or the platform file says the host never has
captions — 抖音, 小红书) ⇒ audio-only download + local ASR. **Read
`references/video-asr.md` before starting** — it is the whole recipe: the
audio-only download, the language→backend routing gate (Chinese → SenseVoice,
everything else → faster-whisper; **a gate, not a preference** — a missing
backend means stop and put the install-or-degrade choice to the user, never a
silent substitute), the shipped VAD-first runner that produces timestamped
SRT, the background+poll discipline for the minutes-long run, and the
audio-specific checks. Its output is a standard SRT — §3 consumes it
unchanged. The audio is deleted after transcription, always.

---

## 3. Cues → anchored transcript (the assembly step)

This step is **deterministic and stack-independent** — whatever produced the
cues (captions or any ASR backend), it's the same text transform — so it ships
as a script. Don't hand-transcode cue lines or rewrite the logic ad hoc:

```bash
python3 <video-skill>/scripts/srt_to_anchors.py subs.srt \
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

Related trap: with **multiple `.md` files in the `--from` dir**, "first by
alphabet" would pick `anchored.md` over `transcript.md` (also a live
incident — the unpolished intermediate got normalized while the deliverable
sat beside it). The script now prefers `transcript.md`, skips known
intermediate names, and refuses when still ambiguous — but keep the temp dir
clean anyway: the §1 contract is *one* deliverable md per capture dir.

---

## 4. Verify — before and after normalizing

**Before polishing/normalizing** (cheap checks that catch 90 % of bad captures):

- `status`/metadata matches the video you asked for — title, `original_id`,
  `source_url` (a Bilibili BV id has resolved to a *different* video before;
  `status: ok` alone proves nothing).
- **Char-count sanity:** Chinese speech ≈ 300–600 chars/min. A 25-min video →
  7.5k–15k chars; **<1,000 chars for 20+ min ⇒ truncated**. At 30-s anchors,
  expect ~2 segments/min. But a transcript ending at ~80-85 % of a long video
  with a *natural sentence ending* is normal (non-speech outro music/previews) —
  don't re-run ASR for that.
- On the ASR path, also run the audio-input checks in `video-asr.md` §5
  (audio-vs-video duration, truncated/corrupt download) — bad audio makes
  every downstream check lie.

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

## 5. Field-tested pitfalls & the platform index

Generic pitfalls, any platform:

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
contract (§1), captions decision (§2), assembly (§3), and verification (§4)
stay here, and the ASR recipe + long-run discipline stay in
`references/video-asr.md`.
