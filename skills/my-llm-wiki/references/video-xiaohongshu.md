# 小红书 (Xiaohongshu) — video-note capture

Platform notes for `references/video-capture-sop.md` — the acceptance contract,
ASR recipe, assembly, long-run discipline, and verification all live there;
this file is only what 小红书 adds on top.

Field-tested live: one agent failed on every login-free path, then the opencli
recipe below captured the same 20-minute note (207 MB video + cover + full
metadata) in under 5 minutes. 小红书 has **no caption track** — always Path B
(audio → ASR; content is zh → SenseVoice).

The one viable fetcher is **`opencli xiaohongshu`** riding a logged-in Chrome
session — 小红书 login-walls everything else (see dead ends). Note that
agent-reach's xiaohongshu backend *is* opencli: `agent-reach doctor` showing
`xiaohongshu: off` usually means opencli is missing **from that process's
PATH**, not that a separate backend needs installing.

**Step 0 — preconditions.**

```bash
opencli xiaohongshu whoami     # logged_in: true — else surface `opencli xiaohongshu login`
```

App shares carry a short link `http://xhslink.com/o/<code>`; the note id is the
24-hex token in the resolved URL. Canonical `source_url` is
`https://www.xiaohongshu.com/discovery/item/<note_id>` (strip the query — the
`xsec_token` in it is per-share and ephemeral); `original_id` is the note id.

## The recipe

`download` accepts the **xhslink short link directly** — no resolving needed:

```bash
opencli xiaohongshu download 'http://xhslink.com/o/<code>' --output <tmpdir> \
  > download.log 2>&1
# → <tmpdir>/<note_id>/<note_id>_1.mp4 (the video) + _2.jpg… (cover/images)
```

Redirect the output: the progress bar spams `\r` frames (hundreds of KB) that
will flood an agent transcript. A 200 MB video takes a few minutes — for long
notes apply the SOP §4 background+poll discipline.

Metadata comes from `note`, which does **not** take a bare note id — it wants a
full signed URL (`xsec_token` included). Resolve the short link to get one:

```bash
url=$(curl -s -o /dev/null -w '%{url_effective}' -L --max-time 15 'http://xhslink.com/o/<code>')
opencli xiaohongshu note "$url" -f yaml
# → title, author, content (the note text — keep it as the 简介), likes,
#   collects, comments, tags.  NO publish time — if you need one, grab it from
#   the note page via `opencli web read --url "$url"`, else omit --publish-time.
```

## Common tail

Same as Douyin (`references/video-douyin.md`): `ffprobe` the mp4 duration for
sanity (SOP §5), extract 16 kHz mono wav, keep the downloaded cover jpg (or
take frame 0), **delete the mp4**, run the SOP §2 SenseVoice recipe, then SOP
§3 `srt_to_anchors.py --url
'https://www.xiaohongshu.com/discovery/item/<note_id>'` — the 小红书 player
ignores `?t=` params today, so anchors are index-only, same as Douyin. Then
assemble the SOP §1 `transcript.md` around the anchored output (real `# title`
H1, `>` header, cover, 简介 from the note's `content`) before normalizing —
never feed `anchored.md` straight to `--md` (mints a RAW titled `anchored`
with no cover; `normalize_raw.py` now refuses it).

## Dead ends — do not retry (from a live failed capture)

- **Login-free browser automation**: xiaohongshu.com redirects any fresh
  session to a `website-login/error` page (`error_code=300012 "IP at risk"`)
  before content renders.
- **tavily-extract / plain HTTP readers** on `discovery/item/<id>` URLs:
  `Failed to fetch url` — same login wall.
- **`opencli xiaohongshu note <bare-note-id>`**: hard error
  `requires a full signed URL` — always pass the resolved URL with `xsec_token`.
- **A daemon agent seeing `which opencli` fail while it's installed**: daemon
  terminals snapshot a `bash -l` env, which reads `~/.profile` /
  `~/.bash_profile` — **not** `~/.zprofile`, where npm-global PATH exports
  usually live. Fix the PATH (export in `~/.profile`, or symlink opencli into
  `~/.local/bin`) instead of concluding the tool is absent.
