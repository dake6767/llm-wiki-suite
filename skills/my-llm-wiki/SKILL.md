---
name: my-llm-wiki
description: >-
  Ingest external content into the immutable RAW layer of an LLM-WIKI knowledge
  base: fetch a source, convert it to self-contained Markdown, and download its
  images locally so the result is a faithful, archivable original (not a summary).
  A pluggable fetch adapter does the fetching — opencli is the bundled default but
  is NOT required; a no-opencli profile is provided. Use this whenever the user
  wants to save / archive / clip / capture / "沉淀" a link into their wiki /
  knowledge base — a WeChat 公众号 article (mp.weixin.qq.com), an X/Twitter post, a
  小红书 note, or any web page; to batch-sync X/Twitter bookmarks (收藏/bookmarks)
  into RAW; to file the user's OWN note / idea / 想法 / 观点 as `source_type: note`
  ("记一下我的想法", "存个 idea", "沉淀一个观点"); or to distill an ONLINE VIDEO
  (YouTube / Bilibili / 视频) into RAW as a timestamped transcript — keeping the
  link, never the video file ("把这个视频存进知识库"). Also use it to set up /
  initialize a new wiki when none exists ("初始化 wiki", "建一个知识库", "set up my
  LLM-wiki") — it scaffolds a repo compatible with the open-source llm_wiki app and
  Obsidian. Strongly prefer this skill the moment the user pastes a URL with intent
  to keep / file / archive it, or says "存成 RAW", "存到知识库", "clip this", "save
  this article", "同步我的 X 收藏", or invokes /my-llm-wiki — even without naming
  opencli or the word "RAW". Do NOT use it to consume or transform rather than
  archive: summarizing or analyzing an article / tweet / video, searching or
  querying the wiki itself ("查一下知识库", "wiki 里有没有…" — that reading path is
  my-llm-wiki-maintainer's Query flow), searching the web for
  content, judging whether something is worth reading, ripping a video FILE to keep,
  converting a local PDF to Markdown merely for reading (but DO use it to archive
  that document into the wiki — "把这个 PDF 存到知识库"), organizing or deduping
  local files, writing a scraper, generating or editing the wiki's own derived /
  Wiki-layer pages, or syncing notes between apps (e.g. Obsidian → Notion).
---

# my-llm-wiki — ingest sources into the RAW layer

This skill turns external content into **RAW**: the immutable source-of-truth
layer of an [LLM-WIKI](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).
RAW files are originals the wiki reads from but never edits — so the goal is a
faithful, self-contained capture (text + local media), **not a summary**.

A **fetch adapter** does the heavy lifting (fetch a page → Markdown, download
images, rewrite links); the bundled default is `opencli` (web/social) + `markitdown`
(local docs). This skill's core is **adapter-agnostic** — it **routes** a source to
the right wiki, **normalizes** the output into the RAW contract, and **composes**
the cases the default adapter only half-covers (single X posts, batch bookmarks,
video). Swapping fetch tools = satisfying `references/adapter-contract.md`; the
engine only consumes a fixed on-disk shape, it never calls a scraper.

**Every capture follows the same spine:** resolve the wiki (§1) → fetch to a temp
dir (§2) → normalize into RAW (§4) → optionally hand off to synthesis (§7). Pick the
entry by what the user gave you:

- **Link** (one URL or a few) — most common → §1–§4.
- **Bookmark-sync** ("同步我的 X 收藏") — batch → §5.
- **Note** (the user's own thought) → §6.
- **Video** (YouTube/Bilibili/…) → §8.
- **No wiki yet** ("初始化 wiki") → start at §0.

---

## §0 — First run: initialize a wiki (if none exists)

When no target wiki resolves (§1) — common on a new machine — don't dump RAW into a
random folder. Confirm **where** it lives (suggest `~/llm-wiki`) and **a name**, then:

```bash
python3 <skill>/scripts/init_wiki.py --path <root> --name "<name>" \
  --description "<one line: what content belongs here>" --default
```

This scaffolds a repo the `llm_wiki` app and Obsidian open directly (`.llm-wiki/`,
`raw/sources/` + `raw/assets/`, `wiki/`, `schema.md`, `purpose.md`, `.obsidian/`) and
**registers** it into the shared wiki table so captures can auto-route by topic (§1).
It's idempotent. The `--description` is the one line the topic classifier matches, so
make each wiki's distinct ("AI/编程/工具" vs "美食/旅行"); `--default` marks the
fallback. Re-running `init` with a new `--description` re-describes an existing wiki
without touching its files. Full registry detail: `references/routing.md`.

---

## §1 — Resolve the target wiki

A user can keep several wikis. Decide where this capture lands, highest priority first:

1. **Explicit** — the user named one ("存到人文库", `--wiki <path>`). Always wins.
2. **Ambient** — CWD (or an ancestor) **is** a wiki (`schema.md` + `wiki/`, or
   `.llm-wiki/project.json`). `normalize_raw.py` walks up when you omit `--wiki`.
3. **Auto-classify by topic** — neither of the above **and** the registry holds ≥2
   wikis (the "丢链接它自己归档" path; see box).
4. **Single / default** — one registered wiki, else the registry default, else
   `$LLM_WIKI_DEFAULT`. `normalize_raw.py` falls through these when you omit `--wiki`.
5. **Nothing resolves** — don't guess (RAW is permanent). If a wiki exists, ask which;
   if none, init via §0.

> **Auto-classify** (run it **after** the §2 temp fetch — classify from the real
> title + author + body, not the URL): read the candidates with
> `python3 <skill>/scripts/wikis.py list`, weigh the source against each wiki's
> one-line description, and **act on confidence** (the user's choice was "拿不准才问我"):
> a **clear best match** → route automatically (`--wiki <path>` to `normalize_raw.py`)
> and state the choice + reason in one clause; **ambiguous or no match** → stop and
> `AskUserQuestion` (offer the candidates + "新建一个"). An explicit instruction always
> overrides the classifier. Full rules: `references/routing.md`; real
> mis-routing traps (the genre/topic seam — e.g. a 影评 about a historical subject
> belongs in the life wiki, not the history one): `references/routing-pitfalls.md`.

---

## §2 — Obtain the source (via an adapter)

Fetch into a **temp** dir (e.g. `/tmp/llmwiki-<x>`) with media as local files, in the
shape of the **Adapter Contract** (`references/adapter-contract.md`) — then §4
normalizes it into RAW. Temp-first keeps half-finished fetches out of RAW.

**Probe what's installed first** (especially on a fresh machine, or if the skill was
distributed to someone without opencli):

```bash
python3 <skill>/scripts/preflight.py
```

Its `adapters:` map reports, per source type, the best available path (`ok` → go;
`degraded` → works with a caveat; `unavailable` → relay its `install` hint). It never
installs anything. A bare machine can still capture web text, X, and notes —
opencli/markitdown/yt-dlp only *upgrade* coverage.

**opencli is the default adapter, not a dependency.** If it's missing (or the user
runs a different stack), `references/adapters-without-opencli.md` is a complete
no-opencli profile for every source type.

### Default-adapter quick reference

Match on the host. Dedicated adapters give cleaner captures; `web read` is the
universal fallback for **any** site. If a tool isn't found, run the Preflight in
`references/sources.md` first (same shell block as the command).

| Source | Command | Notes |
|------|---------|-------|
| `mp.weixin.qq.com` | `opencli weixin download --url <url> --output <tmp> --download-images true -f yaml` | `wechat`. If `images/` is empty (newer 图文 hide images in a JS gallery), redo with `web read` — see `references/sources.md`. |
| `x.com` / `twitter.com` (single post) | compose — see §3 | `x` |
| anything else (小红书, web, …) | `opencli web read --url <url> --output <tmp> --download-images true -f yaml` | `xiaohongshu` for xhs, else `web` |
| **online video** (YouTube, Bilibili, …) | see §8 | `video` — transcript, never the file |
| **local document** (PDF, docx, …) | `markitdown <file> -o <tmp>/doc.md` | `doc`. Normalize with `--md <tmp>/doc.md` **and `--source-file <file>`** (markitdown text is lossy). See `references/sources.md`. |

Always capture into a **temp** dir. `-f yaml` reports the `saved:` folder and the
title. If an opencli command hits a login/auth wall, tell the user to run the
adapter's `login` once (e.g. `opencli twitter login`) — don't scrape around auth.

---

## §3 — Single X/Twitter post (default adapter)

An X post needs composing, not one command. The rule that matters: **don't build the
capture from the `twitter bookmarks` listing fields** — that listing's `text` is lossy
(a long-form tweet can show as a bare `t.co` link) and `has_media` can be wrong. Fetch
per tweet — `opencli web read` for full text + images, `opencli twitter download` only
when the post has video — then normalize with `--from`. Exact commands:
`references/sources.md` → "X / Twitter — single post"; no-opencli fallback:
`references/x-fallback-capture.md`. Long-form/article posts come back as
`title: untitled` (fix before normalizing) — see `references/x-article-pitfalls.md`.

---

## §4 — Normalize into the RAW contract

Run the bundled script — the deterministic heart of ingestion (parses the adapter
header into YAML frontmatter, moves media into `raw/assets/`, rewrites links, repairs
HTML→Markdown structural damage, and refuses to clobber existing RAW). Pass the **UTC
capture time** so the record is honest:

```bash
python3 <skill>/scripts/normalize_raw.py \
  --from <tmp adapter output folder> \
  --wiki <wiki from §1 — pass explicitly when auto-classifying; omit to auto-detect> \
  --source-type wechat \
  --source-url "<original url>" \
  --original-id "<platform id, e.g. the mp.weixin /s/ token>" \
  --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

For the composed X case, swap `--from <dir>` for `--md tweet.md --assets <dir>`.

It writes `raw/sources/<source_type>/<YYYY-MM-DD>-<slug>.md`, localizes images into the
shared `raw/assets/` (relative links), versions (`-v2.md`) instead of overwriting, and
runs `clean_md.py` automatically to repair converter damage. Frontmatter schema, layout
reasoning, readability cleanup, and video handling: `references/raw-contract.md`.

**Surface capture-health warnings — don't bury them.** Sites and adapters drift, so a
capture can silently come back degraded. `normalize_raw.py` runs sanity checks and, when
something looks off, prints `capture_health: warn` with a `warnings:` list, stamps it in
the RAW frontmatter, and appends to `<wiki>/.llm-wiki/capture-issues.log`. Tell the user
plainly what may be incomplete and why so they can retry or update the adapter (an
adapter fault is a job for `opencli-autofix`). Also check opencli's own `status:` field —
a failed fetch should be reported, not normalized into RAW.

After it runs, report concisely: which wiki, the final path, how many images localized,
and any videos left as links. Then check **§7** for the synthesis handoff.

---

## §5 — Bookmark-sync mode (batch X, default adapter)

Pulling X bookmarks into RAW in bulk is a stateful, incremental job (uses `opencli
twitter`). Two rules drive it: **dedupe by tweet id before downloading** (listing is
cheap, media download is expensive and per-tweet — get captured ids with
`scripts/captured_ids.py --wiki <root> --source x`, subtract, download only the new
ones, which also makes runs resumable); and **first sync needs a big `--limit`,
incremental a small one** (bookmarks are newest-first with no older-than cursor). Full
procedure — listing, folders, id-filter, per-item compose+normalize, batching,
reporting — is in `references/x-bookmarks.md`; then loop each new bookmark through §3 + §4.

---

## §6 — Record the user's own thought (note)

When the user wants to file **their own** idea/observation ("记一下我的想法", "存个 idea",
"沉淀一个观点"), that's a first-party RAW capture with `source_type: note` — same
machinery, no opencli. Usual entry is **dictation**: they say it, you file it.

1. **Write it up faithfully** to `/tmp/llmwiki-note-XXXX/note.md` with an `# H1` title.
   You're archiving *their* voice — fix only obvious typos; **don't editorialize,
   summarize, or expand**. For referenced local images, add `![](path)` refs and pass
   `--assets <dir>`.
2. **Route by topic (§1)** — a note goes to whichever wiki fits its subject.
3. **Normalize as a note:**
   ```bash
   python3 <skill>/scripts/normalize_raw.py \
     --md /tmp/llmwiki-note-XXXX/note.md --assets /tmp/llmwiki-note-XXXX \
     --source-type note --wiki <wiki from §1> \
     --related "<optional: a captured-source slug, [[wikilink]], URL, or topic>" \
     --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   ```
   `--related` is the high-value link — point a responding thought at the source it
   responds to. Writes `raw/sources/note/<date>-<slug>.md` (note-shaped frontmatter, no
   `source_url`; `tags: [inbox, note]`), skips readability cleanup, won't false-flag a
   short thought.

A note is an **immutable snapshot** of what you thought *then* — don't edit it to
"update"; add a new one. Growing notes into living wiki pages is the **wiki layer's**
job, not this skill's. Boundary detail: `references/raw-contract.md` → notes.

---

## §7 — Hand off to the synthesis layer (optional, pluggable)

Capturing fills **RAW**. Turning RAW into derived **wiki** pages (synthesis) is a
*different* skill's job — typically **`my-llm-wiki-maintainer`**. If it's installed you
can chain straight in; if not, leave the capture marked `inbox` and stop (the handoff is
a bonus, not a dependency). **Always hand it the explicit wiki root you resolved in §1** —
it has no idea about the registry or multiple wikis; never make it guess.

Decide by the user's intent **this turn**:

- **Synthesis intent** — the ask implies processing into the wiki ("整理 / 沉淀 / 理解 /
  读懂 / 吃透 / ingest / 串起来 / 存进 wiki 并…"; "理解" & kin mean *distill into the
  wiki*, so treat "抓取并理解" as synthesis for the **same** source). After §4 succeeds,
  in the **same turn** use `my-llm-wiki-maintainer` to ingest **the file you just wrote**
  — pass the §4 summary's `wiki:` (root) and `dest:` (source path). Report both results.
- **Query intent** — the ask is to *read* the wiki, not add to it ("查一下 / 搜 /
  有没有 / 之前存过…吗 / what does my wiki say about X"). Users reach for this skill's
  name for *any* wiki interaction, so treat this as a normal entrance, not a mistake.
  This skill only writes RAW; do **not** answer by scanning raw files. Hand off to
  **`my-llm-wiki-maintainer`**'s Query flow, passing the wiki root resolved per §1
  (the topic → wiki mapping applies to reads too: "历史库里查 X" resolves the same way
  a capture would). If the maintainer isn't installed, say so and point the user at
  the wiki's `wiki/index.md` instead of improvising retrieval.
- **Capture-only intent** — "存一下 / clip / save this", or a bare URL with no
  processing ask. Do **not** synthesize. End with one line: how many items pending + how
  to trigger ("已存入 RAW（inbox）。说「整理」即可综合进 wiki。").
- **Batch / cold start** — "把 inbox / 积压都整理了". Resolve the wiki via §1 first (≥2 and
  unspecified → `AskUserQuestion`, optionally offering "全部"). Then run
  `my-llm-wiki-maintainer`'s `scripts/wiki_ops.py cache pending <root>` to list
  un-synthesized sources and ingest each.

**`inbox` vs the ingest ledger.** `tags: [inbox]` is a "freshly captured" hint, **never
edited** (RAW is immutable). The authoritative "already synthesized" record is the
maintainer's `.llm-wiki/agent/ingest-cache.json`; "pending" = not in that cache, so
`cache pending` (not the inbox tag) is the truth. **One ingest path only** — if the
desktop `llm_wiki` App is watching this wiki, don't also auto-chain a skill ingest
(duplicate pages). Make the ingest path singular before chaining.

---

## §8 — Online video → RAW (transcript, not the video file)

The user wants a video's **content** in RAW, with the link kept, but no disk for the
file. So a video capture = a faithful **transcript** filed as `source_type: video`,
media never stored. The transcript is **timestamped**: each ~30s chunk is prefixed with
a clickable `**[MM:SS](…&t=NNNs)**` deep link, so the wiki can answer *"a point was made
somewhere in some video — where?"* Preserve these anchors through every step.

Built on `scripts/fetch_video.py` (YouTube + Bilibili first-class via opencli; other
hosts via yt-dlp). **opencli is optional** — it degrades automatically to yt-dlp +
local ASR. Zero API cost: captions are free; the no-caption fallback transcribes
audio with a **local** backend (Whisper, or SenseVoice for Chinese via `--asr auto`).
Full recipe, quality levers, and audio-fallback gotchas: `references/sources.md` →
"Online video"; no-opencli stack: `references/adapters-without-opencli.md`. **Live
failure modes** (wrong-video resolution, stale temp dir, orphan Whisper, partial
download, the Hermes `nohup` ban, post-normalize title fixes, the manual Bilibili
pipeline): `references/video-capture-pitfalls.md` — read it before a Bilibili / ASR
capture.

1. **Fetch the transcript — background it, then actively POLL `--status-file`.**
   Captions return in seconds, but the no-caption Whisper pass takes **minutes to tens
   of minutes** on CPU and **exceeds single-command timeouts** (e.g. hermes kills any
   one command at 300 s), so a blocking foreground call gets killed mid-transcribe.

   The **universal contract** (works in any runtime): start `fetch_video.py` as a
   **non-blocking** job that writes `--status-file`, then **poll that file yourself**
   with short commands until it appears. **Do not rely on the runtime to notify you on
   completion** — `--status-file` polling is the only portable, reliable signal; a
   background job whose completion you wait to be *told* about can leave you idle long
   after it finished. Poll, don't wait.

   *How you background it is environment-specific; the polling is not:*
   - **Plain shell** → `nohup … &` (shown below).
   - **Hermes** (rejects `nohup`/`&`) → `terminal(background=true)` **without**
     `notify_on_complete`, then poll `status.yaml`. Setting `notify_on_complete=true`
     here is worse than useless: the status file is written before the process exits,
     so you always finish the workflow first and the exit notification lands *after*
     your final reply as a stale `[IMPORTANT: Background process … exited]` message
     that re-wakes the session for nothing.
   - **Other agents** → whatever their non-blocking exec primitive is; the
     `--status-file` + poll contract is unchanged.

   Use a **fresh** temp dir per capture — a reused `/tmp/llmwiki-vid` can serve a
   previous run's stale transcript.
   ```bash
   # plain-shell form; swap the launch for your runtime's non-blocking exec:
   mkdir -p /tmp/llmwiki-vid
   nohup python3 <skill>/scripts/fetch_video.py --url "<video url>" \
     --output /tmp/llmwiki-vid --status-file /tmp/llmwiki-vid/status.yaml \
     > /tmp/llmwiki-vid/run.log 2>&1 &
   # key flags: --whisper-model medium (turbo/large-v3 = better) · --asr auto (LEAVE
   #   at auto — routes Chinese → SenseVoice; only override with a specific reason) ·
   #   --browser chrome (cookies for the audio fallback) · --lang en (force lang)
   ```
   ```bash
   # poll in a separate short command, repeat until status.yaml exists:
   test -f /tmp/llmwiki-vid/status.yaml && cat /tmp/llmwiki-vid/status.yaml \
     || { echo "still transcribing…"; tail -2 /tmp/llmwiki-vid/run.log; }
   ```
   Captions finish on the first poll or two; the Whisper path needs more (poll every
   ~30–60 s; a long video legitimately takes 10–25 min). The background mechanism does
   not affect transcription speed — only how promptly you *notice* it finished, which
   is why you poll. The script tries captions
   first, else downloads audio-only → local ASR → **deletes the audio**, lays down
   `transcript.md` in the `--from` shape, and writes a YAML summary
   (`transcript_source`, `has_timestamps`, `needs_translation`, `title`/`author`/
   `original_id`/`publish_time`, `warnings`, …) to both `run.log` and `--status-file`.
   - **`status: error`** → surface the reason, don't ingest a stub (common: a YouTube
     auth wall → `opencli youtube login`; the audio fallback needing browser cookies).
   - **`audio download incomplete` / SenseVoice / torch errors** → see the
     audio-fallback gotchas in `references/sources.md` (the fix is usually adding
     `--browser`, keeping `--asr auto`; never debug torch with the system `python3`).

2. **Polish + translate** — *your* job, editing `/tmp/llmwiki-vid/transcript.md` in
   place. Light, content-preserving polish of the `## 文字转写` body (paragraph breaks,
   punctuation, fix obvious ASR mishearings) — don't rewrite/summarize/drop, same
   "faithful repair" spirit as `clean_md.py`, and **keep every `**[MM:SS](…&t=…)**`
   anchor exactly where it is** (edit only the prose after it). **If
   `needs_translation: true`** (a non-Chinese video), append a `## 中文译文` section
   below the original with a full Chinese translation, carrying the same anchors.

3. **Route to the right wiki (§1)** from the title + channel + transcript.

4. **Normalize into RAW** (pass the summary's metadata as flags):
   ```bash
   python3 <skill>/scripts/normalize_raw.py --from /tmp/llmwiki-vid \
     --source-type video --wiki <wiki from §1> \
     --source-url "<video url>" --original-id "<videoId>" \
     --author "<channel>" --publish-time "<publishDate>" \
     --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   ```
   It writes `raw/sources/video/<date>-<slug>.md`, localizes the cover, and — because
   `video` is a deliberately-not-downloaded source — adds **no** "can't download"
   callout (the transcript *is* the capture). **Verify after:** the title/slug default
   to `transcript` (from the `transcript.md` filename) — patch them to the real video
   title and confirm the cover landed. See `references/video-capture-pitfalls.md`.

5. **Report** concisely: wiki, path, transcript source (captions vs `whisper(model)`),
   that it's timestamped with jump-back links, whether you added a translation, the
   cover. Then check **§7** for the synthesis handoff.

---

## Principles

- **RAW is immutable.** Never edit an existing RAW file to "update" it — capture a new
  version. The script enforces this; hold the mindset too.
- **Faithful over tidy.** Keep the original's structure and images — you're archiving a
  source, not writing a digest. The Wiki layer does the digesting.
- **Temp first, then commit.** Capture to `/tmp`, normalize into the wiki. A failed
  fetch leaves RAW untouched.
- **Don't fight auth.** If a site needs login, surface the `… login` step instead of
  scraping around it.

## Reference map

- `references/routing.md` — multi-wiki registry & topic classification (§0, §1)
- `references/sources.md` — default-adapter recipes, Preflight, video, docs (§2, §3, §8)
- `references/adapter-contract.md` — the on-disk shape any fetch tool must satisfy
- `references/adapters-without-opencli.md` — complete no-opencli / no-browser profile
- `references/raw-contract.md` — frontmatter schema, layout, readability, notes (§4, §6)
- `references/x-bookmarks.md` — batch bookmark-sync procedure (§5)
- `references/x-fallback-capture.md` — X capture without opencli (§3)
- `references/routing-pitfalls.md` — genre/topic mis-routing traps (§1)
- `references/video-capture-pitfalls.md` — live video/ASR failure modes & verification (§8)
- `references/x-article-pitfalls.md` — X long-form `untitled` fix (§3)
- `data/` — per-corpus content data (e.g. ASR glossaries); runtime-generated, not shipped, not loaded as guidance
