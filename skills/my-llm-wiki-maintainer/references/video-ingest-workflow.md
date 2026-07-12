# Video Ingest Workflow (YouTube/Bilibili)

Optimized workflow for ingesting video transcripts into a wiki, developed from repeated
batch sessions (6+ videos in one session). Combines `my-llm-wiki-video` capture with
`my-llm-wiki-maintainer` ingest into a single streamlined flow.

## Quick Reference

```
0. Pre-flight: check if video already captured (source-page + ingest cache)
1. Background capture (per my-llm-wiki-video's video-capture-sop.md) → poll status.yaml
2. Polish transcript (ASR fixes, paragraph breaks, punctuation)
3. Clean transcript trailing content
4. Normalize into RAW (normalize_raw.py)
5. Probe source + source-page dedup
6. Read existing index → plan new vs update pages
7. Write blocks file → apply-blocks (new pages, no --overwrite)
8. Write update blocks → apply-blocks (--overwrite for existing pages)
9. Cache save via `wiki_ops.py --files-file`
10. Verify cache
```

## Step 0: Pre-Flight Dedup (avoid redundant captures)

Before spending minutes on Whisper transcription, check if this video was already captured:

```bash
# Check ingest cache for the video ID
grep "<video_id>" <root>/.llm-wiki/agent/ingest-cache.json 2>/dev/null

# Check source-page dedup
python3 <maintainer>/scripts/wiki_ops.py source-page <root> \
  --raw "dummy" --url "<video_url>"
```

If `source-page` returns `existing: "wiki/sources/..."`, the video was already synthesized.
Options:
- **Re-capture with better transcript**: proceed with fetch, but update the existing source page
  (don't create a new one). `normalize_raw.py` will create a `-v2` RAW file automatically.
- **Skip**: if the existing synthesis is complete and the user just wants it in the wiki,
  tell them it's already there.

**Pitfall: re-capture creates `-v2` RAW but existing source page still points to original.**
After a re-capture, update the source page's `sources:` field to reference the new RAW file,
or add both. The existing wiki pages (entities, concepts) don't need updating unless the
new transcript reveals content the original missed.

## Step Details

### 1. Background Capture

The capture itself is the `my-llm-wiki-video` skill's job — follow its
`references/video-capture-sop.md` (probe tools → captions first → else
audio + local ASR → assemble the anchored transcript). The contract that
matters here: run the long step as a **non-blocking background job** that
writes a `status.yaml` into a **fresh** temp dir (`/tmp/llmwiki-vidN`) as its
last act, then **poll that file yourself** — don't wait to be notified. When
this same turn will continue into polishing and ingest, launch it with
asynchronous completion delivery disabled (`notify_on_complete=false` in
Hermes). After the matching status appears, wait/reap the retained process
handle and confirm it has exited before sending the final result; a read-only
poll or timed-out wait can leave a stale completion push queued behind the real
conclusion.
Captions finish in seconds; an ASR pass takes minutes to tens of minutes.

**Pitfall: unique temp dir per video.** When processing multiple videos in one session,
use `/tmp/llmwiki-vid`, `/tmp/llmwiki-vid2`, `/tmp/llmwiki-vid3`, etc. Reusing the same
dir causes stale status.yaml from the previous video.

**Pitfall: stale data from a PREVIOUS session.** `mkdir -p` does NOT clean an existing
directory. If `/tmp/llmwiki-vid/` already exists from a prior session, it contains old
`status.yaml` and `transcript.md`. Reading those before the new background process
completes gives you the WRONG video's data — and you may polish/normalize the wrong
transcript without realizing it. **Always use a timestamped directory:**
```bash
TMPDIR="/tmp/llmwiki-vid-$(date +%s)"
mkdir -p "$TMPDIR"
```
After the background process completes, verify `status.yaml`'s `source_url` matches
the URL you requested. See `my-llm-wiki-video` skill's `references/video-asr.md`
(§4, background + poll discipline) for the full incident report.

**Pitfall: Bilibili silent partial download.** Without `--browser chrome`, yt-dlp
silently downloads only a fraction of Bilibili audio (e.g. 2.5 min of 25 min).
SenseVoice then reports `status: ok` with a tiny transcript. Before polishing,
**verify audio duration** matches the video:
```bash
ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 \
  /tmp/llmwiki-vid/audio.mp3
```
Also check `transcript_chars` in status.yaml — <1,000 chars for a 20+ min video
is a red flag. If truncated, re-run with `--browser chrome`.

### 2. Polish Transcript (ASR Fixes)

**WAIT until the background process is confirmed complete before reading the transcript.**
The `status.yaml` file is the completion signal. If it doesn't exist, shows a different
video URL/ID than expected, or the process is still running — do NOT read `transcript.md`.
Reading stale data from a previous capture and polishing it is a real, documented pitfall
(see `my-llm-wiki-video` skill's `references/video-asr.md` §4).

When `transcript_source: whisper(...)` or `sensevoice(...)`, the raw output has no
punctuation, no paragraph breaks, and frequent ASR errors — especially for proper
nouns (historical names, places, technical terms). Polish in place before normalizing.

**ASR proper-noun corrections are per-corpus DATA, not skill guidance.** Whisper and
SenseVoice mis-transcribe the same names differently, and the right corrections depend
entirely on the wiki's subject — do not hard-code a name table in this reference. Keep a
correction table scoped to the corpus: `data/asr-corrections-zh-history.md` ships the
Chinese-history (Qing/Ming) table accumulated from real sessions; start an analogous one
per wiki. The upstream `my-llm-wiki-video` skill's `references/video-capture-sop.md` (§4)
describes how to build corrections from the full transcript.

**Polish steps:**
1. Fix ASR errors (historical names, place names, technical terms)
2. Add paragraph breaks every 2-3 timestamp segments for readability
3. Add Chinese punctuation (commas, periods, question marks, exclamation marks)
4. Keep ALL `**[MM:SS](…&t=NNNs)**` timestamp anchors exactly as-is — they are the
   jump-back index. Edit only the prose after them.
5. Don't summarize, rewrite, or drop content — faithful repair only.

### 3. Clean Transcript Trailing Content

Caption/description-based pipelines often append repeated content from the video
description at the end of the transcript. This isn't markdown-structural damage (so `clean_md.py` won't catch
it) — it's content-level duplication. Verify the exact duplicated tail, then
rewrite the transcript as data with the runtime's normal file-edit/write tool.
Do not create an inline script that blindly deletes the last line.

### 4. Normalize

For dash-prefixed video IDs (e.g. `-dQhTC--Voo`), ALWAYS use equals-sign form:

```bash
python3 <skill>/scripts/normalize_raw.py \
  --from /tmp/llmwiki-vidN \
  --wiki /path/to/wiki \
  --source-type video \
  --title "<video title>" \
  --source-url "<url>" \
  --original-id="-VIDEO_ID" \    # equals form for dash-prefixed IDs
  --author "<channel>" \
  --publish-time "<date>" \
  --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

Safe bare form for alphanumeric IDs: `--original-id y0IkFsoQVp0`

**Pitfall: normalizing the bare `anchored.md`.** `srt_to_anchors.py` output is
an intermediate — normalize the assembled `transcript.md` (real `# title` H1 +
`>` header + cover) via `--from`, and/or pass `--title`. `normalize_raw.py`
refuses a title falling back to a generic working filename (`anchored`,
`transcript`, …); hitting that error means assemble first — never mint the RAW
and then patch frontmatter + rename it (the old workflow, retired after it
recurred in every capture one night).

**Pitfall: `-v2` suffix on re-capture.** When a RAW file for the same video already exists,
`normalize_raw.py` creates a `-v2.md` version instead of overwriting. This is correct
(immutability), but the existing source page still references the original RAW. After
re-capture, update the source page or note the discrepancy.

### 5. Probe + Dedup

```bash
python3 <maintainer>/scripts/wiki_ops.py probe-source <root> --raw "<raw_path>"
python3 <maintainer>/scripts/wiki_ops.py source-page <root> --raw "<raw_path>" --url "<url>"
```

### 6. Plan Pages: Create vs Update

Check each extracted entity/concept against the existing wiki with bounded
retrieval — do NOT read the full `wiki/index.md` into context (retrieval
discipline, `ingest-update.md` step 5):

```bash
python3 <maintainer>/scripts/wiki_ops.py retrieval-search <root> --q "<name>" --top 8
grep -F "[[entities/<name>" <root>/wiki/index.md                                    # disk sentinel, zero context
```

**Decision tree:**
- **Entity exists (search/grep hit)** → update (merge new content into existing page)
- **Entity is new** → create
- **Concept exists** (e.g. 明朝内阁制度, 明朝宦官制度) → update with new source cross-references
- **Concept is new** → create
- **Source page** → always create (one per raw source, enforced by source-page dedup)

**Typical video creates:** 1 source page + 1 entity + 2-4 concept pages
**Typical video updates:** 0-2 existing entity/concept pages

**Plus review suggestions — a required decision, 0–3 per video:** what research
directions does this video open? Claims worth verifying against written sources
(video history content routinely dramatizes), adjacent topics the wiki lacks,
tensions with existing pages. Emit each as a `---REVIEW: suggestion | <title>---`
block with 2–3 ready-to-run `searchQueries` (shape: `references/output-blocks.md`).
Zero is a legitimate call for a thin or rehash episode — the Nth video of a series
often adds pages but no new directions — but it must be decided, not skipped: this
streamlined flow historically dropped this step entirely and the deep-research
queue silently starved for weeks.

**Pitfall: don't let `tags: [inbox]` / `source_type: video` from the RAW frontmatter
stand in for real tags on the source page.** This is the shortcut a batch session
under time pressure reaches for — real topical tags take more thought than echoing
what you just read off the RAW file. `inbox` is never acceptable (it means "not yet
processed", already false once the wiki page exists); `video`/`bilibili` as *one*
tag alongside genuine ones is harmless (`[清初, video, 康熙朝, 索额图]` is fine), but
as the *entire* tag set it means no real tags ever got generated. Write real subject
tags per `schema.md`'s Tag & Domain Policy (e.g. `[清朝, 雍正朝, 官员, 正直讲史]`,
not `[video, inbox]`) — and reuse an existing combo from sibling source pages in the
same series where it fits, rather than inventing a one-off set per video.

### 7. Two-Pass Apply-Blocks

Write ALL new pages into one blocks file, then ALL updates into another.

**Pass 1 — new pages (no --overwrite):**
```bash
python3 <maintainer>/scripts/wiki_ops.py apply-blocks <root> \
  --blocks-file /tmp/llmwiki-ming-blocksN.txt \
  --source raw/sources/video/<filename>.md
```
New pages: source, entities, concepts, index delta, log delta — **and the REVIEW
blocks from step 6** (they ride in the same blocks file; `apply-blocks` persists
them to `.llm-wiki/review.json` and reports `"reviews": N`).

**Always pass `--source` on the pass that carries the REVIEW blocks** (Pass 1). Its
value is threaded into every persisted review as `sourcePath`. Omit it and the reviews
land with `sourcePath: null` — the Browser can no longer trace a suggestion back to the
raw source it came from, and the deep-dive「挖」floater loses its source match. This is
silent: `apply-blocks` still succeeds and reports `"reviews": N`. Pair it with a `PAGES:`
line in each REVIEW block so `affectedPages` is populated too.

**Pass 2 — existing pages (--overwrite):**
```bash
python3 <maintainer>/scripts/wiki_ops.py apply-blocks <root> \
  --blocks-file /tmp/llmwiki-ming-updateN.txt --overwrite \
  --source raw/sources/video/<filename>.md
```
Updated pages: existing entities, existing concepts that need expansion.

**Why two passes:** `apply-blocks` without `--overwrite` SKIPS existing content pages.
With `--overwrite` it backs up old content then overwrites. Mixing new and existing in
one pass with `--overwrite` would unnecessarily back up brand-new pages.

**Verify:** Every FILE path must appear in `written[]`, `warnings` must be empty,
and `reviews` must equal the number of REVIEW blocks you emitted — `"reviews": 0`
when you planned suggestions means they never left the chat. Then surface the new
suggestions in the final response per SKILL.md workflow step 6 (「可深挖方向」).

### 8. Cache Save (CJK-safe and approval-clean)

Write the affected page paths as a JSON array or one path per line using the
runtime's ordinary file-write tool, then pass that data file to `wiki_ops.py`.
This avoids long/CJK shell argv without generating a Python subprocess wrapper:

```bash
python3 "$MAINTAINER/scripts/wiki_ops.py" cache save "$ROOT" "$RAW" \
  --files-file "$TMPDIR/pages.json"
python3 "$MAINTAINER/scripts/wiki_ops.py" cache check "$ROOT" "$RAW"
```

The save command prints the persisted source and `filesWritten`; require it to
match the page list, then require `cache check` to report `hit: true`. Silent
success is worse than loud failure. Do not use an arbitrary-code tool for this
deterministic operation.

## Batch Processing Pattern

When processing 6+ videos from the same YouTube series:

1. Use incrementing temp dirs: `/tmp/llmwiki-vid`, `/tmp/llmwiki-vid2`, ...
2. Use incrementing block files: `/tmp/llmwiki-ming-blocks.txt`, `blocks2.txt`, ...
3. Separate update files: `/tmp/llmwiki-ming-update.txt`, `update2.txt`, ...
4. Each video's ingest cache save is independent
5. Existing pages accumulate updates across videos (e.g. 朱元璋 entity got updated
   after the first video, then again referenced in later videos)

## Series Accumulation Pattern

When processing **sequential episodes** from the same channel (e.g. 正直讲史's 康熙王朝 series — 雅克萨之战 → 二征雅克萨 → 尼布楚条约), the "typical video updates" count from Step 6 increases significantly:

- **Episode 1**: Mostly creates (1 source + 2-3 entities + 2-3 concepts). Updates: 0-1.
- **Episode 2**: Mix of creates and updates (1 source + 1-2 new entities/concepts + **2-3 updates** to pages from episode 1).
- **Episode 3+**: Heavy updates (1 source + 1-2 new + **3-4 updates** to accumulated pages).

**Why**: Entities like 索额图 or 康熙帝 accumulate new sections with each episode (negotiation tactics, military strategy, personal quotes). Concepts like 雅克萨之战 get expanded (first battle → second battle → treaty). The `sources:` field in updated pages grows to list all RAW files that contributed.

**Practical impact**: Write more update blocks per video as the series progresses. The two-pass pattern (new pages → updates) remains the same, but pass 2 gets larger. Each updated page needs:
1. New `sources:` entry appended
2. `updated:` date set to current
3. New sections added to body (don't replace — append)
4. `related:` expanded with new entities/concepts from this episode

**Batch block file naming**: Use incrementing numbers per video to avoid confusion:
- `/tmp/llmwiki-qing-blocks.txt` (episode 1 new pages)
- `/tmp/llmwiki-qing-update.txt` (episode 1 updates)
- `/tmp/llmwiki-qing-blocks2.txt` (episode 2 new pages)
- `/tmp/llmwiki-qing-update2.txt` (episode 2 updates)
- etc.

## Cross-Linking Patterns for Video Series

When ingesting a multi-part video series (e.g. a creator's 历史 series), each video typically:

- **Creates** new entities/concepts specific to that video's topic
- **Updates** existing pages from previous videos (adding cross-references)
- **Links backward** to concepts established in earlier videos
- **Links forward** with "下集" references where appropriate

The source page should explicitly list cross-links to both new and existing concepts,
making the wiki graph denser with each video.
