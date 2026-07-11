# X/Twitter fallback capture without opencli

Use this only when the preferred `opencli web read` path is unavailable or fails and the user still wants the source archived into RAW. Do not encode the temporary failure as a rule; this is a fallback recipe. It is a second, minimal adapter for one source: it satisfies sibling `my-llm-wiki/references/adapter-contract.md` by handing the core `normalize_raw.py --md … --assets …`.

**First rule out a PATH problem.** `opencli` is an npm CLI that is frequently just not on the shell's `PATH` (agents that don't load the user's nvm/profile) — that is *not* a reason to fall back. Run the sibling core's `scripts/preflight.py --profile capture.x.single` first; opencli gives cleaner captures and localizes images for you. Only drop to this fxtwitter recipe when opencli is genuinely absent or the fetch truly fails.

## One deterministic command

```bash
python3 "$X_SKILL/scripts/fx_capture.py" \
  --tweet "<numeric-id or x.com URL>" \
  --out /tmp/llmwiki-x-<tweet_id>
```

The script fetches `https://api.fxtwitter.com/status/<id>`, **saves the full API
JSON to `<out>/fx.json` (disk-first — never `curl` this endpoint to stdout; a
measured ad-hoc `curl | head` put ~49KB of raw JSON into the conversation and
re-billed it on every later call)**, converts plain tweets and long-form
articles (draft-js blocks + entityMap) to Markdown, downloads cover/inline
images and the best mp4 variant into `<out>/images/`, and prints a compact JSON
summary: title, author, publish time, `md`/`assets_dir` paths, media counts,
and warnings. Only that summary belongs in context.

Check `warnings` before continuing. `--no-media` skips downloads;
`--from-json` re-parses an already saved `fx.json` without refetching. If the
script itself fails on a schema drift, inspect `fx.json` with **bounded** reads
(grep for the specific field, never cat the whole file) and assemble manually
per the field notes below.

## fxtwitter field notes (manual assembly / repair)

`https://api.fxtwitter.com/status/<tweet_id>` can expose long-form X article content that the regular tweet text reduces to a `t.co` link. Useful fields:

- `tweet.author.name` + `tweet.author.screen_name` → `--author`.
- `tweet.created_at` → `--publish-time` (format like `Fri Jun 05 14:26:41 +0000 2026`; `normalize_raw.py` handles this now).
- `tweet.article.title` → `--title`.
- `tweet.article.content.blocks` + `entityMap` → Markdown body.
- `tweet.article.cover_media.media_info.original_img_url` → cover image.
- `tweet.article.media_entities[]` → inline images and videos.

## Assembly rules

1. Create a temp folder, e.g. `/tmp/llmwiki-x-<tweet_id>/images` (fx_capture.py does 1–4 for you).
2. Download cover and inline `ApiImage` media into `images/`.
3. For `ApiVideo`, download the highest-bitrate `content_type == video/mp4` variant into `images/video-<media_id>.mp4` and optionally download its preview image.
4. Build `tweet.md` with a normal H1 and body. For media, use local relative Markdown links, e.g. `![image](images/image-<id>.jpg)` and `![video](images/video-<id>.mp4)`.
5. Normalize with:

```bash
python3 <core-skill>/scripts/normalize_raw.py \
  --md /tmp/llmwiki-x-<tweet_id>/tweet.md \
  --assets /tmp/llmwiki-x-<tweet_id>/images \
  --wiki <wiki> \
  --source-type x \
  --source-url "https://x.com/<handle>/status/<tweet_id>" \
  --original-id "<tweet_id>" \
  --title "<article title>" \
  --author "<display name> (@<handle>)" \
  --publish-time "<tweet.created_at>" \
  --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

## Pitfalls

- Do **not** build from bookmark/listing `text`; long-form posts often appear only as a short `t.co` link.
- If video was downloaded locally, do **not** also leave the remote mp4 URL as plain text in the body unless you intentionally want it recorded as an unlocalized video link. `normalize_raw.py` flags HTTP `.mp4` URLs as `video_links` / `has_video`.
- Preserve RAW immutability: if a bad capture was already written, delete the bad RAW file and its prefixed assets only after explicit user approval, then re-ingest. Otherwise leave it and capture a versioned file.
