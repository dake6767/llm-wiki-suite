# Bookmark-sync mode (batch X → RAW)

Goal: pull the user's X/Twitter bookmarks into the wiki as individual RAW items,
incrementally — running it again later should only ingest what's new, not
re-capture everything. This is the one genuinely stateful flow in the skill.

> This recipe is part of the **default adapter** (it uses `opencli twitter`). The
> wiki-core pieces (dedupe by id, per-item normalize) are tool-agnostic; only the
> listing/fetch commands are opencli-specific — see `references/adapter-contract.md`.

## 1. Resolve the wiki (same as §1 of SKILL.md)

Decide the target wiki before fetching anything (captures land under its
`raw/sources/x/`). Batch writes amplify a wrong-repo mistake, so if resolution is
ambiguous, confirm with the user.

## 2. List the bookmarks

```bash
opencli twitter bookmarks --limit <N> -f yaml
```

Returns rows with: `id, author, text, likes, retweets, bookmarks, created_at,
url, has_media, media_urls`. **Use this listing for discovery and dedup only —
its `text`, `has_media`, and `media_urls` are unreliable** (a long-form tweet can
show up as a bare `t.co` link with `has_media: false`). The real content comes
from a per-tweet fetch in step 4. What you trust from the listing is `id` and
`url`. Defaults to newest-first. Useful options:
- `--limit N` — how many to pull (default 20). Ask the user how far back to go.
- `--top-by-engagement N` — re-rank by engagement and keep the top N instead of
  recency, if the user wants "my best saves" rather than "my latest".

For folder-organized bookmarks:
```bash
opencli twitter bookmark-folders -f yaml          # list folders (id, name, count)
opencli twitter bookmark-folder --id <folder-id> -f yaml
```
Let the user pick a folder if they mention one ("同步我的 AI 收藏夹").

If this hits an auth wall, have the user run `opencli twitter login` once.

## 3. Dedupe by tweet id — guard the expensive step

The whole sync hinges on one principle: **listing is cheap, media download is
expensive, so filter by id between them.** `twitter bookmarks` is a single
(paginated) call; `twitter download` opens the browser and pulls media per tweet.
Never download media for a tweet you already have.

A tweet's `id` is its stable identity. Get the ids already in RAW:

```bash
python3 <skill>/scripts/captured_ids.py --wiki "<wiki>" --source x
```

Subtract that set from the bookmark listing, and only run `twitter download` +
`normalize_raw.py` for the **remaining (new) ids**. This is also what makes a
sync *resumable*: if a big run dies partway, just run it again — every id already
on disk is skipped at this filter, so nothing is re-downloaded.

`normalize_raw.py` keys identity on `original_id` too, so it's a correct backstop
(same id → `--on-exists skip` no-ops; a different id with a colliding slug is
auto-disambiguated, never skipped). But do the id-filter *before* downloading —
the backstop alone would still pay the media-download cost.

## 3.5 Volume — first sync vs incremental

Bookmarks return **newest-first**, and the adapter pages via `--limit`; there is
no "older-than" cursor exposed. That shapes two distinct modes:

**First sync (large backlog).** A small `--limit` only ever sees the newest
slice, so to reach an old backlog the first run needs a `--limit` at least as big
as your total bookmark count (ask the user roughly how many they have; err high,
e.g. `--limit 3000`). That run is heavy — make it survivable rather than perfect:
- Stream through the new ids in **batches**, normalizing each as you go; don't try
  to download everything before writing anything.
- It's **resumable by construction**: if it dies (browser hiccup, timeout, rate
  limit), re-run the same command. The id-filter (§3) skips everything already on
  disk, so it continues where it stopped and re-downloads nothing.
- If the user organizes bookmarks in **folders**, sync folder-by-folder
  (`bookmark-folders` → `bookmark-folder --id`). Natural chunks, clearer
  progress, each folder independently resumable.
- Be polite: a short sleep between per-tweet downloads, and report progress every
  N items so a long run is legible.

**Incremental sync (ongoing).** Once the backlog is in, new bookmarks land at the
top. A small `--limit` (e.g. 50) plus the id-filter catches them cheaply; when
nothing is new, the run is near-free. This is a good habit to run periodically.

## 4. Ingest each new bookmark

For each survivor, run the **single-post flow** in `references/sources.md` → X —
the same path as a single pasted tweet. In short, per tweet:
1. `web read --url <tweet-url> --download-images true` → full text + local images.
   This is the content source — never reconstruct the body from the listing's
   `text` field (it's lossy; see step 2).
2. If the tweet has video, `twitter download --tweet-url <url>`, move the `.mp4`
   into the web-read folder's `images/`, and add `![video](images/<file>.mp4)`.
3. `normalize_raw.py --from <web-read folder> --source-type x --source-url <url>
   --original-id <id> --on-exists skip
   --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"`.

Be polite to the API: a small delay between items and a sane `--limit` avoid
hammering X. For large syncs, process in batches and report progress.

You don't need to hand-craft unique slugs. Two tweets from the same author on
the same day slugify identically, but `normalize_raw.py` keys identity on
`original_id`: a different id with a colliding slug is auto-disambiguated (a short
id tail is appended) rather than skipped, while the *same* id is treated as a
real duplicate. So just pass each tweet's real `--original-id` and let the script
keep them distinct.

## 5. Report

Summarize: how many bookmarks were listed, how many were new vs already in RAW,
how many ingested successfully, and any that failed (with why — e.g. auth, a
protected tweet). List the new `raw/sources/x/<date>-<slug>.md` paths so the user
can spot-check.

## Notes

- **Unbookmarking is out of scope.** Capturing to RAW doesn't remove the X
  bookmark. Only unbookmark if the user explicitly asks (`opencli twitter
  unbookmark`), and confirm first — it's an outward-facing, hard-to-undo action.
- Re-running the sync later is safe and cheap thanks to id-dedup — encourage the
  user to treat it as a periodic "drain my bookmarks into the wiki" habit.
