---
name: my-llm-wiki-x
description: >-
  Capture X/Twitter content into an LLM-WIKI immutable RAW layer with complete
  per-post text, metadata, images, and downloadable video, or incrementally sync
  the logged-in user's X/Twitter bookmarks with tweet-id deduplication and resumable
  batching. Use together with my-llm-wiki whenever the user wants to save, archive,
  clip, or “沉淀” an x.com/twitter.com post, thread, long-form X article, or X media
  into their wiki/knowledge base, or asks to sync/import/pull X/Twitter 收藏,
  bookmarks, or bookmark folders into RAW. Do not use merely to summarize/analyze
  a tweet, search X, manage follows, post/reply, or download media without archiving
  the source into the wiki.
---

# my-llm-wiki-x — X posts and bookmarks to RAW

Capture each X item from its canonical post rather than from a lossy listing.
This skill owns X composition, fallbacks, and bookmark state. The sibling
`my-llm-wiki` skill owns wiki routing, the Adapter/RAW contracts, normalization,
and capture-health checks.

## Locate the shared core

Resolve the sibling core before starting:

```bash
X_SKILL="<this skill directory>"
CORE_SKILL="$(cd "$X_SKILL/../my-llm-wiki" && pwd)"
test -f "$CORE_SKILL/scripts/normalize_raw.py"
```

If the check fails, stop and install `my-llm-wiki`; do not copy its scripts into
this skill. Suite installs resolve this declared dependency automatically.

## Choose the flow

- **One post, thread, article, or pasted X URL:** read and follow
  `references/x-capture-sop.md`.
- **Bookmarks / 收藏 / bookmark folder:** read and follow
  `references/x-bookmarks.md`, then run every new item through the single-post SOP.

Run the matching probe first: `--profile capture.x.single` for one post or
`--profile capture.x.bookmarks` for bookmark sync, then inspect the same key
under `capabilities`.
Prefer a logged-in rendered fetch when available; use the fxtwitter fallback only
after ruling out a PATH/login problem. Surface the one-time login step instead of
scraping around an auth wall.

## Shared workflow

1. Extract the numeric tweet ID and canonical URL. Use listing results only for
   discovery and IDs, never as the archived body.
2. Fetch full text and images per post. Fetch X video separately when present and
   add it as a local Markdown media link before normalization.
3. Detect long-form/article posts even when the URL is `/status/…`; fix
   `untitled` metadata in the temp capture before normalization.
4. Resolve the target wiki from the captured content. For a bookmark batch,
   resolve before any writes because a routing mistake is multiplied across items.
5. Commit each verified item through the shared core:

   ```bash
   python3 "$CORE_SKILL/scripts/normalize_raw.py" --from <temp-folder> \
     --source-type x --wiki <wiki-root> --source-url "<canonical post url>" \
     --original-id "<tweet id>" --on-exists skip \
     --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   ```

   Use `--md ... --assets ...` instead when composing the fallback manually.
   Pass explicit title, author, and publish time when the adapter header is weak.
   Surface all capture-health warnings.
6. Report the wiki and new RAW paths. For bookmarks include listed, already
   captured, new, succeeded, and failed counts. For explicit synthesis intent,
   hand the exact wiki root and new RAW path(s) to `my-llm-wiki-maintainer`.

## Bookmark invariants

- Run `python3 "$X_SKILL/scripts/captured_ids.py" --wiki <root> --source x`
  before expensive per-post media downloads.
- Use a large newest-first limit for first sync and a small limit for later
  incremental runs; process large backlogs in resumable batches.
- Keep `original_id` authoritative so normalization can distinguish a real
  duplicate from a slug collision.
- Never unbookmark after capture unless the user explicitly asks and confirms.

## Invariants

- Never construct the archive from bookmark/search/listing text.
- Never normalize a failed adapter response or an `untitled` long-form capture.
- Never edit an existing RAW file. Recapture to a versioned file instead.
- Keep remote video URLs out of the body after the video has been localized.

## Reference map

- `references/x-capture-sop.md` — complete single-post composition and commit flow.
- `references/x-bookmarks.md` — stateful bookmark discovery, dedup, batching, and report.
- `references/x-fallback-capture.md` — browser-free fxtwitter adapter
  (`scripts/fx_capture.py`, disk-first).
- `references/x-article-pitfalls.md` — long-form title/author repair before commit.
- Sibling `my-llm-wiki/references/routing.md` — shared multi-wiki routing.
- Sibling `my-llm-wiki/references/adapter-contract.md` — accepted temp shapes.
