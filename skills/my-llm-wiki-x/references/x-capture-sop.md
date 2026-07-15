# Single X/Twitter post capture SOP

Use this flow for one X post, thread, or long-form article. Produce a complete
temp adapter shape, then commit it through the sibling `my-llm-wiki` core.

## 1. Identify the source

Canonicalize the URL to `https://x.com/<handle>/status/<numeric-id>` when possible.
Use the numeric ID as `original_id`. Treat `/article/<id>` and article-length
`/status/<id>` pages as long-form candidates.

## 2. Deduplicate before any network fetch

Resolve every wiki candidate that is knowable without fetching: an explicitly
named wiki first, then an ambient/default/sole registered wiki. Check each candidate
for the numeric tweet id:

```bash
python3 <x-skill>/scripts/captured_ids.py \
  --wiki "<wiki-root>" --source x --find "<tweet-id>"
```

Exit `0` prints matching absolute RAW paths, oldest `captured_at` first; exit `1`
means this wiki has no match. When routing is still ambiguous, check all registered
wiki roots before fetching—the globally unique tweet id is enough to identify the
existing destination. Do not download the post merely to classify content that is
already captured.

On a hit:

- do not call a fetcher and do not run normalization again;
- treat the first path as the canonical RAW; if more paths are printed, surface a
  duplicate warning instead of creating another copy;
- for capture-only intent, report that the source already exists;
- for capture + synthesis intent, hand the canonical RAW to
  `my-llm-wiki-maintainer`: return the existing result when its cache is
  `hit: true`, otherwise ingest that RAW and verify the new cache hit.

The core normalizer also scans the complete source bucket by `original_id`; that
is a race/fallback backstop, not permission to skip this cheaper pre-fetch check.

## 3. Probe and fetch the authoritative body

Run the shared core's `scripts/preflight.py --profile capture.x.single` and
inspect `capabilities.capture.x.single`.

Preferred composition:

1. Create a fresh output root for this one capture. Never reuse a fixed path such
   as `/tmp/llmwiki-x`, because stale adapter folders can be mistaken for the new
   result:

   ```bash
   CAPTURE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/llmwiki-x.XXXXXX")"
   STATUS_FILE="$CAPTURE_ROOT/opencli-status.yaml"
   ```

2. Fetch the rendered post page with local images, pinning OpenCLI's output and
   keeping its compact status off stdout/context:

   ```bash
   opencli web read --url "<canonical-post-url>" --download-images true \
     --output "$CAPTURE_ROOT" --stdout false -f yaml >"$STATUS_FILE"
   ```

   Inspect only the bounded `status`, `saved`, title, author, and publish-time
   fields in `STATUS_FILE`. Use the adapter's `saved` path as the capture folder
   (`dirname` when `saved` is a Markdown file). Require it to be inside
   `CAPTURE_ROOT`; never fall back to a home-wide or filesystem-wide search.

3. If the post contains video, fetch it separately with
   `opencli twitter download --tweet-url "<post-url>"`, move the best local mp4
   into the temp capture's `images/`, and add `![video](images/<file>.mp4)` to
   the Markdown.
4. Check the adapter's own `status` before continuing.

Never construct the body from a bookmark, timeline, search, or list result: its
text and media flags are discovery hints and are known to truncate long-form posts.

If opencli is genuinely unavailable or the fetch fails after PATH/login checks,
read `x-fallback-capture.md` and run its `scripts/fx_capture.py` — it lands the
fxtwitter payload on disk and assembles the same temp shape deterministically.
Never dump the fxtwitter API JSON to stdout/context.

## 4. Repair long-form metadata before commit

Inspect every capture for `title: untitled` or `author: "-"`, regardless of URL
shape. Read `x-article-pitfalls.md`, derive the title/author from the body or API,
and repair the temp folder/header before normalization. Never repair immutable RAW
afterward; recapture when a bad version was already written.

## 5. Verify the adapter shape

Require:

- substantial post/article text rather than only a `t.co` redirect;
- canonical source URL and numeric tweet ID;
- local image/video references for media that was downloaded;
- no remote mp4 URL left as an accidental duplicate after localization;
- a meaningful title for long-form content.

## 6. Route and normalize

Route from the actual captured author/title/body using the shared core's routing
policy. Then run:

```bash
python3 <core-skill>/scripts/normalize_raw.py --from <temp-folder> \
  --source-type x --wiki <wiki-root> \
  --source-url "<canonical-post-url>" --original-id "<tweet-id>" \
  --on-exists skip --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

For a manually assembled fallback, use `--md tweet.md --assets images/` and pass
`--title`, `--author`, and `--publish-time` explicitly.

Report the final RAW path, localized asset count, duplicate/skip result, and every
capture-health warning.
