---
name: my-llm-wiki
description: >-
  Ingest external content into the immutable RAW layer of an LLM-WIKI knowledge
  base as a faithful, self-contained original, not a summary, and initialize or
  create new Wiki repositories with routing registration. Use when the user
  wants to save, archive, clip, capture, file, or “沉淀” a URL, WeChat article,
  webpage, Xiaohongshu note, local document, X/Twitter post or bookmarks, online
  video, or their own note/idea; also use for “创建一个新的 wiki 仓 / 新建知识库 /
  initialize a wiki” or when explicitly invoked. Combine with my-llm-wiki-x for
  X/Twitter and my-llm-wiki-video for
  online video. For requests that combine capture with synthesis—such as “抓取并整理”,
  “抓取并理解/读懂/吃透”, or “capture then ingest”—own the full chain: write RAW,
  hand its exact wiki root and path to my-llm-wiki-maintainer, verify the ingest
  ledger, and report both stages. Do not stop after RAW or ask the user to repeat
  the synthesis request. Do not use merely to summarize/analyze content, search
  the web, judge whether it is worth reading, rip a video, convert a PDF only for
  reading, edit derived wiki pages, or sync notes between apps.
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

For capture + synthesize, record both completion gates in the working plan before
fetching. Choose the execution context at that point:

- **A delegation mechanism is available** (`Agent`, `delegate_task`, a subagent,
  or an independent run): keep capture in the parent, then delegate synthesis
  after normalization. **Do not load `my-llm-wiki-maintainer` in the parent**;
  tell the fresh worker to load it. Loading it on both sides pays the same large
  skill prefix twice without improving fidelity.
- **No delegation mechanism is available:** load `my-llm-wiki-maintainer` in the
  current context as soon as the combined intent is recognized, so the second
  gate survives a long fetch or transcription.

After normalization, pass the exact resolved wiki root and exact newly written
RAW path into the maintainer's Ingest flow.

## Fresh-context synthesis gate

Capture and synthesis are separate token-cost phases. The capture conversation
often contains fetched payloads, adapter diagnostics, or transcription recovery;
running many maintainer calls behind that prefix re-bills irrelevant history.
Therefore a capture + synthesize request **must use a fresh worker whenever the
host exposes delegation**. This is task isolation, not optional parallelism.

Send a compact, self-contained handoff containing only:

```text
operation: ingest
wiki_root: <absolute resolved root>
raw_paths: [<absolute newly written RAW path>]
required_skill: my-llm-wiki-maintainer
completion_gates: cache check hit=true; return written pages, warnings,
  review suggestions, and the online wiki link if available
```

Do not copy the fetched body, transcript, search output, or conversation summary
into the handoff. RAW already contains the faithful source. The worker loads the
maintainer skill and only the reference required for its operation, performs the
write-backed flow, and returns a compact result with verifiable paths.

If delegation is synchronous, wait for it and send one combined final report. If
the host only supports background delegation (as some Hermes surfaces do), a
successful dispatch is **not completion**: do not claim the request is done or
send a capture-only success. Let the completion turn verify the ledger and own
the terminal report. A short in-progress acknowledgement is acceptable only when
the host requires a response; the user must never have to repeat the synthesis
request.

The combined request is complete only when:

1. RAW normalization succeeded; and
2. maintainer ingest succeeded and `cache check` reports `hit: true` for that RAW.

The terminal report must relay — not summarize away — the maintainer's result:
list every review suggestion verbatim (never collapse them into a count like
“沉淀了 2 条建议”), and include the `线上 WIKI: [点击查看总结](...)` Markdown
link when the maintainer returned one. These are the two pieces the user acts
on next; dropping them makes the report incomplete even if both gates passed.

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

## Resolve tools through the Provider Resolver

This Skill depends on capabilities and output contracts, not one mandatory
runtime. Resolve command providers with the shipped runner on every platform:

```bash
python3 <my-llm-wiki>/scripts/tool_exec.py \
  --capability capture.web.authenticated opencli -- web read \
  --url "https://example.com" --output "<temp>"
python3 <my-llm-wiki>/scripts/tool_exec.py \
  --capability document.to-markdown markitdown -- \
  "/path/document.pdf" -o "<temp>/doc.md"
```

Resolution order is: the Provider explicitly requested for this task, a saved
capability override, the healthy official Provider, a matching system command,
then configured custom Providers. A task-level choice is temporary; persist it
only when the user asks to use that Provider in future work. Pass an explicit
choice with `--provider <id>`.

The Browser one-click path installs and prefers the project-tested official
toolchain. The open path remains valid without Browser or Setup Core: the
resolver may use a healthy system or custom Provider, and the Skill applies the
same RAW validation before accepting its output. Do not install third-party
dependencies ad hoc during capture. If no Provider satisfies the capability,
offer `my-llm-wiki ensure-pack toolchain-base` as the supported fallback or ask
the user to select/configure another Provider.

ASR entry points resolve and relaunch under the selected `asr-zh` or `asr-other`
Python Provider themselves, including its scoped model environment. Never
construct `PYTHONPATH` or activate an unrelated virtualenv by hand.

## Fetch payloads are disk-first

The staging rule above is also a context-budget rule, not only a safety rule.
Anything printed to stdout enters the conversation and is re-billed as prompt
prefix on every later call in the session (a measured fallback fetch put ~49KB
of raw API JSON into context this way). So:

- **Fetchers write to files, never to stdout.** Use the tool's own output flag
  (`--output`, `-o`, `>file`) into the temp capture dir. Piping through `head`
  is not a budget — a truncated dump is still tens of KB of prompt prefix.
- **Verify with bounded probes.** Confirm a fetch from file size, the adapter's
  own status field, or a grep for the specific field you need — not by printing
  the payload.
- **Only compact excerpts enter context**: title, author, publish time, media
  counts, health warnings — what routing and verification actually need.
- When the only available fetcher returns content in-band (an agent built-in
  like WebFetch), that one pass is the price of the path; save it to the temp
  file immediately and do not additionally print the file back.

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

New Wiki creation belongs to this skill, not the maintainer's `wiki_ops.py init`.
The canonical initializer both scaffolds the repository and upserts the shared
topic-routing registry; a directory tree without that registry entry is an
incomplete result.

For an additional Wiki, first resolve the collection chosen during initial Setup:

```bash
python3 <skill>/scripts/wikis.py collection-root
```

That directory is the default home for every new Wiki repository. Ask for a
human-facing name and a distinct one-line topical description; infer and show a
safe directory slug. Do not ask the user to invent another location when the
collection root resolves. Ask for an explicit path only when resolution fails or
the user requested a different location.

Create a sibling repository without moving the current default:

```bash
python3 <skill>/scripts/init_wiki.py --slug <directory-name> \
  --name "<name>" --description "<one-line topical scope>"
```

For an intentional location override, replace `--slug` with `--path <root>`.
Use `--default` only when the user explicitly wants the new Wiki to receive
otherwise-unclassified captures.

The command is idempotent and registers the wiki. Make descriptions distinct
enough for topic routing. Before reporting success, run `wikis.py list --json`
and verify the exact path, non-empty description, and intended default flag.
Read `references/routing.md` for registry operations.

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
pass `--wiki` explicitly to normalization and state the choice briefly; it takes
either a wiki root path or a registered name from that list. Read both
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

The probe is a hard gate: do not select a fetcher before it runs, and do not
fall back to a generic extractor (tavily/WebFetch/built-in browser) while the
probe shows the scenario's preferred adapter installed — that is silent
degradation, not a fallback.

Use the best available adapter, always writing into a fresh temp directory. A
valid adapter produces Markdown plus local media in one of the shapes defined by
`references/adapter-contract.md`. Check the adapter's own status; never normalize
a failed fetch. When a recommended tool is missing, explain the capture-specific
benefit and provide its install command plus project URL. A Provider reported as
`unverified` is installed and resolved — only its health check ran out of time,
so use it and never offer to reinstall the pack it already comes from. Use `cn-mirrors` when
the network requires domestic install routes.

## Normalize into RAW

Commit only a verified temp capture through the deterministic core:

```bash
python3 <skill>/scripts/normalize_raw.py \
  --from <temp-adapter-folder> --wiki <resolved-wiki-path-or-name> \
  --source-type <wechat|web|xiaohongshu|doc|x|video|note> \
  --source-url "<original-url>" --original-id "<stable-id>" \
  --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

Use `--md ... --assets ...` for a manually assembled adapter shape. For local
documents, always pass `--source-file` so the original is archived. The script
parses metadata, moves media into `raw/assets/`, rewrites links, repairs structural
Markdown damage, refuses clobbering, and prints the final wiki/path/assets summary.
It treats `original_id` as identity across the entire source-type bucket, so
`--on-exists skip|fail|version` still applies when an adapter changes the title
and therefore the slug between captures.

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
- **Capture + synthesize intent:** cross the Fresh-context synthesis gate above,
  passing the exact resolved wiki root and new RAW path. Verify the ingest-cache
  entry before the terminal reply; an async dispatch acknowledgement is not that
  terminal reply.
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
