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

### If a bad RAW was already written

Do not edit or rename immutable RAW. Repair the temp capture and normalize it again
to create a versioned file. Leave the bad capture as provenance; remove it and its
prefixed assets only when the user explicitly approves that destructive cleanup.

### Image download

X article media URLs (`/article/.../media/...`) are protected. Even with
`--download-images true`, the images typically don't download without an X
login session. Accept 0 images for X articles unless the browser is logged in.

### Regular tweets vs articles

Any X post written in the long-form/article format can trigger this — including
standard `/status/<id>` URLs. X Premium posts sometimes surface with `/status/`
URLs instead of `/article/` URLs. **Always check the YAML `title:` field**,
regardless of whether the URL contains `/article/` or `/status/`.
