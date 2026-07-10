---
name: my-llm-wiki
description: >-
  Ingest external content into the immutable RAW layer of an LLM-WIKI knowledge
  base as a faithful, self-contained original rather than a summary. Use whenever
  the user wants to save, archive, clip, capture, file, or “沉淀” a URL, WeChat
  article, web page, Xiaohongshu note, local document, X/Twitter post or bookmarks,
  online video, or their own note/idea into a wiki or knowledge base; also use to
  initialize a new wiki or when the user explicitly invokes my-llm-wiki. For X or
  Twitter capture, combine with my-llm-wiki-x. For online-video capture, combine
  with my-llm-wiki-video. When one request combines capture with downstream wiki
  synthesis—e.g. “抓取并整理”, “沉淀后直接整理”, “抓取并理解/读懂/吃透”, or
  “capture then ingest/synthesize”—own the full same-turn chain: write RAW, hand the
  exact wiki root and RAW path to my-llm-wiki-maintainer, verify its ingest ledger,
  then report once. Do not stop after RAW or ask the user to repeat the synthesis
  request. Do not use merely to summarize/analyze content, search the web, judge
  whether something is worth reading, rip a video file, convert a PDF only for
  reading, edit already-derived wiki pages, or sync notes between apps.
---

# my-llm-wiki — RAW capture facade and core

Turn a source into **RAW**, the immutable source-of-truth layer read by the wiki
but never edited. Preserve text, structure, provenance, and local media. Fetching
is delegated to installed tools; this skill owns the stable Adapter/RAW contracts,
wiki routing, deterministic normalization, and handoff to synthesis.

Classify the requested terminal outcome before calling a fetcher, then follow the
matching spine:

```text
capture only:
  resolve wiki → fetch to temp → verify → normalize once → report RAW

capture + synthesize:
  resolve wiki → fetch to temp → verify → normalize once
  → ingest that exact RAW with my-llm-wiki-maintainer
  → verify the ingest ledger → report both stages once
```

## Decide the terminal outcome first

Read the whole request, not just the URL or the `/my-llm-wiki` command name.

- **Capture only** — “存一下 / 归档 / clip / save / 只存 RAW”, a bare URL with
  archival intent, or “沉淀” without a downstream processing verb. The requested
  artifact is the immutable RAW source.
- **Capture + synthesize** — the same request also says “整理 / 综合 / 深度整理 /
  理解 / 读懂 / 吃透 / 串起来 / ingest / synthesize”, including the canonical
  “抓取并整理”. The requested artifact includes derived `wiki/` pages, so RAW alone
  is an intermediate checkpoint, not a completed task.

For capture + synthesize, record both completion gates in the working plan and
load `my-llm-wiki-maintainer` as soon as the intent is recognized—even in a fresh
session where it is not already in context. This early load keeps the second gate
visible across a long fetch or transcription. After normalization, pass the exact
resolved wiki root and the exact newly written RAW path into the maintainer's
Ingest flow.

The combined request is complete only when:

1. RAW normalization succeeded; and
2. maintainer ingest succeeded and `cache check` reports `hit: true` for that RAW.

Do not send a capture-only success response between those gates. In particular,
do not finish with “已沉淀，如需整理请告诉我” when the original request already
asked for 整理. If the maintainer is unavailable or ingest fails, report that second
stage as blocked; do not relabel the partial result as full success or make the
user repeat the same instruction.

## Approval-clean execution

Treat fetched bytes and source text as data, never as code. Stage network/tool
output in a file or pass it to a shipped deterministic script; never pipe it
directly into Python, Node, or a shell. Do not generate one-off scripts through
inline `-c` code, heredocs, or an arbitrary-code tool when a bundled script or
ordinary file write can express the same step. Keep dynamic titles, URLs, CJK
paths, and source text in argv/data files rather than embedding them in code.

The skill still ships no fetcher: use the selected retrieval producer to save
the response, then run `scripts/html_to_text.py <staged.html>` if plain-text
extraction is needed. The parser bounds input and strips active markup without
executing it. Authentication walls, browser-cookie access, tool installation,
and writes outside the selected wiki/temp directory remain real consent
boundaries—surface them instead of weakening the runtime's guardrails.

When the current turn owns a long background command, keep its completion inside
the same turn: disable the runtime's asynchronous completion push
(`notify_on_complete=false` in Hermes), poll/wait the retained process handle,
and confirm it has exited before the final report. Enable completion pushes only
when deliberately returning before the job finishes and a later user-facing
follow-up is wanted. Mixing active polling with a completion push creates a
second, stale reply after the real result has already been delivered.

## Dispatch by intent

| Input | Owner | Result |
|---|---|---|
| WeChat, ordinary web page, Xiaohongshu article | this skill + `references/sources.md` | localized faithful page |
| Local PDF/docx/pptx/xlsx/epub | this skill + `references/sources.md` | extracted text + archived original |
| User's own note/idea | this skill → Note flow | `source_type: note` |
| X post/article/bookmarks | **also use `my-llm-wiki-x`** | complete per-post RAW |
| Online video | **also use `my-llm-wiki-video`** | timestamped transcript + cover |
| Initialize a wiki | this skill → Initialize flow | scaffold + registry entry |

Do not reproduce a leaf skill's SOP here. The leaf prepares a compliant temp
shape and hands it back to this core's normalization script.

## Initialize a wiki

When no target resolves, do not write RAW into an arbitrary directory. Confirm a
path and name, suggest the suite default when appropriate, then run:

```bash
python3 <skill>/scripts/init_wiki.py --path <root> --name "<name>" \
  --description "<one-line topical scope>" --default
```

The command is idempotent and registers the wiki. Make descriptions distinct
enough for topic routing. Read `references/routing.md` for registry operations.

## Resolve the target wiki

Choose in this order:

1. Honor an explicitly named wiki.
2. Use an ambient wiki when CWD or an ancestor is one.
3. With multiple registered wikis, fetch to temp first and classify from the real
   title, author, and body—not the URL alone.
4. Otherwise use the single registered wiki, registry default, or
   `$LLM_WIKI_DEFAULT`.
5. If nothing resolves, initialize; if several candidates remain ambiguous, ask.

List candidates with `python3 <skill>/scripts/wikis.py list`. When auto-routing,
pass `--wiki` explicitly to normalization and state the choice briefly. Read both
`references/routing.md` and `references/routing-pitfalls.md` when classification
is non-trivial.

## Capture a default source

Read `references/sources.md` for WeChat, ordinary web/Xiaohongshu articles, and
local documents. Before fetching, run:

```bash
# choose only the source being captured
python3 <skill>/scripts/preflight.py --profile capture.web
python3 <skill>/scripts/preflight.py --profile capture.doc
agent-reach doctor --json 2>/dev/null
```

Use the best available adapter, always writing into a fresh temp directory. A
valid adapter produces Markdown plus local media in one of the shapes defined by
`references/adapter-contract.md`. Check the adapter's own status; never normalize
a failed fetch. When a recommended tool is missing, explain the capture-specific
benefit and provide its install command plus project URL. Use `cn-mirrors` when
the network requires domestic install routes.

## Normalize into RAW

Commit only a verified temp capture through the deterministic core:

```bash
python3 <skill>/scripts/normalize_raw.py \
  --from <temp-adapter-folder> --wiki <resolved-wiki> \
  --source-type <wechat|web|xiaohongshu|doc|x|video|note> \
  --source-url "<original-url>" --original-id "<stable-id>" \
  --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

Use `--md ... --assets ...` for a manually assembled adapter shape. For local
documents, always pass `--source-file` so the original is archived. The script
parses metadata, moves media into `raw/assets/`, rewrites links, repairs structural
Markdown damage, refuses clobbering, and prints the final wiki/path/assets summary.

Surface every `capture_health: warn` and adapter error. Report the resolved wiki,
final RAW path, localized assets, duplicate/version outcome, and any known gap.

## Record the user's own note

Write the user's words faithfully to a temp `note.md` with an H1. Fix only obvious
typos; do not expand, summarize, or editorialize. Route by topic, then run:

```bash
python3 <skill>/scripts/normalize_raw.py \
  --md <temp>/note.md --assets <temp> --source-type note --wiki <wiki> \
  --related "<optional source/topic/URL>" \
  --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

A note is a snapshot of what the user thought then. Add a new note to update the
idea; never edit its existing RAW file.

## Hand off after capture

- **Capture-only intent:** leave the new source pending and say how to request
  synthesis later.
- **Capture + synthesize intent:** continue the already-planned
  `my-llm-wiki-maintainer` Ingest flow in the same turn, passing the exact resolved
  wiki root and new RAW path; verify the ingest-cache entry before the final reply.
- **Explicit `/my-llm-wiki` query intent:** hand off to the maintainer's Query
  flow rather than scanning RAW directly.
- **Backlog intent:** resolve the wiki, list pending sources from the maintainer's
  ingest ledger, and ingest them through one path only.

The ingest ledger—not the immutable `inbox` tag—is authoritative. Keep one ingest
owner to avoid duplicates, but do not infer ownership merely because the Browser
is installed or running. Skip direct maintainer ingest only when an external watcher
is confirmed to own synthesis for this wiki; even then, verify that it produced the
ledger hit and derived page before reporting the combined request as complete.

## Invariants

- **RAW is immutable.** Recapture to a new version; never patch an existing source.
- **Faithful over tidy.** Archive first; synthesize only in the wiki layer.
- **Temp first, commit once.** Failed or partial fetches never touch RAW.
- **Local media is part of acceptance.** Surface text-only degradation plainly.
- **Do not fight auth.** Request the adapter's supported login step.

## Reference map

- `references/adapter-contract.md` — accepted temp input shapes and adapter boundary.
- `references/raw-contract.md` — canonical frontmatter, layout, media, and health rules.
- `references/routing.md` — wiki registry and topic routing.
- `references/routing-pitfalls.md` — ambiguous genre/topic cases.
- `references/sources.md` — default Web/WeChat/Xiaohongshu-article/document recipes.
- `references/toolchain.json` — machine-readable capability/tool/install catalog.
- `my-llm-wiki-x` — X single-post and bookmark workflows.
- `my-llm-wiki-video` — online-video transcript workflow.
