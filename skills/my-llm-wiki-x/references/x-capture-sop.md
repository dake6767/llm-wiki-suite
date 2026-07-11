# Single X/Twitter post capture SOP

Use this flow for one X post, thread, or long-form article. Produce a complete
temp adapter shape, then commit it through the sibling `my-llm-wiki` core.

## 1. Identify the source

Canonicalize the URL to `https://x.com/<handle>/status/<numeric-id>` when possible.
Use the numeric ID as `original_id`. Treat `/article/<id>` and article-length
`/status/<id>` pages as long-form candidates.

## 2. Probe and fetch the authoritative body

Run the shared core's `scripts/preflight.py --profile capture.x.single` and
inspect `capabilities.capture.x.single`.

Preferred composition:

1. Fetch the rendered post page with local images:
   `opencli web read --url "<post-url>" --download-images true`.
2. If the post contains video, fetch it separately with
   `opencli twitter download --tweet-url "<post-url>"`, move the best local mp4
   into the temp capture's `images/`, and add `![video](images/<file>.mp4)` to
   the Markdown.
3. Check the adapter's own `status` before continuing.

Never construct the body from a bookmark, timeline, search, or list result: its
text and media flags are discovery hints and are known to truncate long-form posts.

If opencli is genuinely unavailable or the fetch fails after PATH/login checks,
read `x-fallback-capture.md` and run its `scripts/fx_capture.py` — it lands the
fxtwitter payload on disk and assembles the same temp shape deterministically.
Never dump the fxtwitter API JSON to stdout/context.

## 3. Repair long-form metadata before commit

Inspect every capture for `title: untitled` or `author: "-"`, regardless of URL
shape. Read `x-article-pitfalls.md`, derive the title/author from the body or API,
and repair the temp folder/header before normalization. Never repair immutable RAW
afterward; recapture when a bad version was already written.

## 4. Verify the adapter shape

Require:

- substantial post/article text rather than only a `t.co` redirect;
- canonical source URL and numeric tweet ID;
- local image/video references for media that was downloaded;
- no remote mp4 URL left as an accidental duplicate after localization;
- a meaningful title for long-form content.

## 5. Route and normalize

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
