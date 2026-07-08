---
name: my-llm-wiki
description: >-
  Ingest external content into the immutable RAW layer of an LLM-WIKI knowledge
  base: fetch a source, convert it to self-contained Markdown, and download its
  images locally so the result is a faithful, archivable original (not a summary).
  Fetching is delegated to whatever tools the machine has (opencli, agent-reach,
  yt-dlp, WebFetch, …) — this skill carries the scenario SOPs and the acceptance
  contract, not the fetchers. Use this whenever the user
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
images, rewrite links) — but this skill **does not ship or maintain fetchers**.
It carries the **scenario SOPs** (how to capture each kind of source well, with
whatever tools are installed: opencli, agent-reach, yt-dlp, markitdown, the
agent's own WebFetch) plus the core that's tool-independent: it **routes** a
source to the right wiki, **normalizes** the output into the RAW contract, and
**composes** multi-step cases (single X posts, batch bookmarks, video). Any tool
satisfying `references/adapter-contract.md` plugs in; the engine only consumes a
fixed on-disk shape, it never calls a scraper.

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

**Probe what's installed first, then take the best available recipe.** This skill
ships no fetchers — the machine's own tools do the fetching, and which tool is
best differs per machine:

```bash
python3 <skill>/scripts/preflight.py        # per-source-type capability map
agent-reach doctor --json 2>/dev/null       # per-platform availability, if installed
```

`preflight.py`'s `adapters:` map reports, per source type, the best available
path (`ok` → go; `degraded` → works with a caveat; `unavailable` → relay its
`install` hint). It never installs anything. A bare machine can still capture
web text, X, and notes — opencli/agent-reach/yt-dlp/markitdown only *upgrade*
coverage. **When the recommended tool for the user's scenario is missing, guide
the install instead of silently degrading**: say what the tool would improve for
this capture, and give the install command *with the project's home URL*
(preflight's recommendations and the stack table in `sources.md` carry both) so
the user can vet the source before installing.

### Scenario index

Per-scenario SOPs (acceptance shape + recipes per available tool) are in
`references/sources.md`. Match on the host:

| Source | Scenario SOP | source_type |
|------|---------|-------|
| `mp.weixin.qq.com` | `sources.md` → WeChat | `wechat` |
| `x.com` / `twitter.com` (single post) | compose — §3 + `sources.md` → X | `x` |
| **online video** (YouTube, Bilibili, …) | §8 + `references/video-capture-sop.md` | `video` — transcript, never the file |
| **local document** (PDF, docx, …) | `sources.md` → Local documents | `doc` — pass `--source-file` |
| anything else (小红书, web, …) | `sources.md` → Any web page | `xiaohongshu` for xhs, else `web` |

Always capture into a **temp** dir. On a login/auth wall, surface the tool's
login step (e.g. `opencli twitter login`) — don't scrape around auth.

---

## §3 — Single X/Twitter post

An X post needs composing, not one command. The rule that matters: **don't build the
capture from a bookmarks-listing's fields** — that listing's `text` is lossy
(a long-form tweet can show as a bare `t.co` link) and `has_media` can be wrong. Fetch
per tweet — a rendered-page fetch for full text + images, a separate video download
only when the post has video — then normalize with `--from`. Recipes per tool
(opencli / the fxtwitter API): `references/sources.md` → "X / Twitter"; the
browser-free fxtwitter path in detail: `references/x-fallback-capture.md`.
Long-form/article posts come back as `title: untitled` (fix before normalizing) —
see `references/x-article-pitfalls.md`.

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

## §5 — Bookmark-sync mode (batch X)

Pulling X bookmarks into RAW in bulk is a stateful, incremental job. It needs a
tool that can list the logged-in user's bookmarks (`opencli twitter` is the
documented recipe). Two rules drive it: **dedupe by tweet id before downloading** (listing is
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

1. **Produce the transcript** per **`references/video-capture-sop.md`** — the
   complete scenario SOP: the acceptance shape (`transcript.md` + `images/cover.jpg`
   in a fresh temp dir), captions first (free, seconds) → else audio-only download +
   **local, language-routed ASR** (zh → SenseVoice, else faster-whisper; audio deleted
   after), the cue→anchor assembly recipe, and the Bilibili/ASR pitfall list. Two
   rules that survive any tooling:
   - **Long ASR runs go in the background; poll a status file yourself** — never a
     blocking foreground call (command timeouts kill it mid-transcribe), and never
     "wait to be notified" (a documented way to sit idle long after completion).
   - **Verify before ingesting** — metadata matches the requested video, audio
     duration matches video duration, char count is plausible. `status: error` or a
     failed check → surface the reason, don't ingest a stub.

2. **Polish + translate** — *your* job, editing the temp dir's `transcript.md` in
   place. Light, content-preserving polish of the `## 文字转写` body (paragraph breaks,
   punctuation, fix obvious ASR mishearings) — don't rewrite/summarize/drop, same
   "faithful repair" spirit as `clean_md.py`, and **keep every `**[MM:SS](…&t=…)**`
   anchor exactly where it is** (edit only the prose after it). **If the video is
   non-Chinese**, append a `## 中文译文` section below the original with a full
   Chinese translation, carrying the same anchors.

3. **Route to the right wiki (§1)** from the title + channel + transcript.

4. **Normalize into RAW** (pass the collected metadata as flags):
   ```bash
   python3 <skill>/scripts/normalize_raw.py --from <tmp dir> \
     --source-type video --wiki <wiki from §1> \
     --source-url "<video url>" --original-id "<videoId>" \
     --author "<channel>" --publish-time "<publishDate>" \
     --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   ```
   It writes `raw/sources/video/<date>-<slug>.md`, localizes the cover, and — because
   `video` is a deliberately-not-downloaded source — adds **no** "can't download"
   callout (the transcript *is* the capture). **Verify after:** the title/slug default
   to `transcript` (from the `transcript.md` filename) — patch them to the real video
   title and confirm the cover landed (`video-capture-sop.md` §5).

5. **Report** concisely: wiki, path, transcript source (captions vs `<asr>(model)`),
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
- `references/sources.md` — per-scenario capture SOPs & tool recipes (§2, §3)
- `references/adapter-contract.md` — the on-disk shape any fetch tool must satisfy
- `references/raw-contract.md` — frontmatter schema, layout, readability, notes (§4, §6)
- `references/video-capture-sop.md` — the video scenario: acceptance contract, recipes, pitfalls (§8)
- `references/x-bookmarks.md` — batch bookmark-sync procedure (§5)
- `references/x-fallback-capture.md` — browser-free X capture via fxtwitter (§3)
- `references/routing-pitfalls.md` — genre/topic mis-routing traps (§1)
- `references/x-article-pitfalls.md` — X long-form `untitled` fix (§3)
- `data/` — per-corpus content data (e.g. ASR glossaries); runtime-generated, not shipped, not loaded as guidance
