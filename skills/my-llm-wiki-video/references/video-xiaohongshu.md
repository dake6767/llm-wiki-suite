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
`xiaohongshu: off` can mean the selected OpenCLI Provider or live Browser
Bridge is unavailable, not that a separate backend needs installing.

**Step 0 — preconditions.**

```bash
opencli xiaohongshu whoami     # logged_in: true — else surface the platform login step
```

App shares may display a short link as `http://xhslink.com/o/<code>`; normalize
its scheme to HTTPS before passing it to a command. The note id is the 24-hex
token in the resolved URL. Canonical `source_url` is
`https://www.xiaohongshu.com/discovery/item/<note_id>` (strip the query — the
`xsec_token` in it is per-share and ephemeral); `original_id` is the note id.

## The recipe

`download` accepts the **xhslink short link directly** — no resolving needed:

```bash
opencli xiaohongshu download 'https://xhslink.com/o/<code>' --output <tmpdir> \
  > download.log 2>&1
# → <tmpdir>/<note_id>/<note_id>_1.mp4 (the video) + _2.jpg… (cover/images)
```

Redirect the output: the progress bar spams `\r` frames (hundreds of KB) that
will flood an agent transcript. A 200 MB video takes a few minutes — for long
notes apply the background+poll discipline (`references/video-asr.md` §4).

Metadata comes from `note`, which does **not** take a bare note id — it wants a
full signed URL (`xsec_token` included). Resolve the short link to get one:

```bash
curl --fail --silent --show-error --location --max-time 15 \
  --output /dev/null --write-out '%{url_effective}\n' \
  'https://xhslink.com/o/<code>' > "$TMPDIR/resolved-url.txt"
opencli xiaohongshu note '<resolved-https-url>' -f yaml
# → title, author, content (the note text — keep it as the 简介), likes,
#   collects, comments, tags.  NO publish time — if you need one, grab it from
#   the note page via opencli web read, else omit --publish-time.
```

Read the single line in `resolved-url.txt` as data and substitute it for the
placeholder in the second command. Do not wrap the network command in shell
command substitution.

## Common tail

Same as Douyin (`references/video-douyin.md`): `ffprobe` the mp4 duration for
sanity (`video-asr.md` §5), extract 16 kHz mono wav, keep the downloaded cover
jpg (or take frame 0), **delete the mp4**, run the SenseVoice recipe
(`video-asr.md` §3), then SOP §3 `srt_to_anchors.py --url
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
- **A daemon agent seeing bare `opencli` fail**: this is expected. Resolve it
  through `tool_exec.py --capability capture.web.authenticated`; if the official
  Provider is damaged, run `my-llm-wiki repair`, or select another Provider.
