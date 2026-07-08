# X/Twitter Article Capture Pitfalls

## `opencli web read` returns "untitled" for X long-form posts

X's long-form article format (URLs with `/article/<id>` OR `/status/<id>` when
the post is article-length, used for X Premium posts) causes `opencli web read`
to produce a header with `title: untitled` and `author: "-"`. The real title and
author are in the body content. This affects all three X posts captured in a
2026-06-16 session — including two `/status/` URLs.

`normalize_raw.py` reads the header title and folder name, so without intervention
the RAW file ends up with:
- `title: untitled` in frontmatter
- filename like `untitled.md`

### Fix BEFORE normalization (preferred)

This avoids post-hoc frontmatter edits and file renames:

1. **Read the YAML output** from `web read` — if `title: untitled`, proceed.
2. **Read the `.md` body** to find the real title (first `#` heading, or the
   first prominent text line near the top).
3. **Rename the output folder**: `mv <tmp>/untitled <tmp>/<real-slug>`
4. **Fix the header lines** in the `.md` (the `# untitled` H1 and the `author:` line).
5. **Then normalize**: `normalize_raw.py --from <tmp>/<real-slug> ...`

### Fix AFTER normalization (fallback)

If you already normalized with `untitled`:

1. **Edit frontmatter title**: set `title:` to the real article title.
2. **Rename the file**: `mv <path>/untitled.md <date>-<real-slug>.md`
3. **Fix author if needed**: pass `--author` explicitly or edit frontmatter.

### Image download

X article media URLs (`/article/.../media/...`) are protected. Even with
`--download-images true`, the images typically don't download without an X
login session. Accept 0 images for X articles unless the browser is logged in.

### Regular tweets vs articles

Any X post written in the long-form/article format can trigger this — including
standard `/status/<id>` URLs. X Premium posts sometimes surface with `/status/`
URLs instead of `/article/` URLs. **Always check the YAML `title:` field**,
regardless of whether the URL contains `/article/` or `/status/`.
