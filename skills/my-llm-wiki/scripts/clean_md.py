#!/usr/bin/env python3
r"""Deterministic readability cleanup for captured Markdown.

opencli's `web read` (and turndown-style HTML→MD converters in general) produce
structurally degraded Markdown on rich pages: inline links explode into
multi-line blocks, headings get split from their text, ASCII punctuation is
over-escaped, and social pages leak navigation chrome. None of that is wrong
content — it's just unreadable. This module repairs the markup *losslessly*
(content-preserving) so RAW stays faithful but legible.

What it fixes, in order:
  1. Collapse exploded link/image blocks  `[\n\n X \n\n](url)` → `[X](url)`
  2. Merge broken headings                `##\n\n Title`        → `## Title`
  3. Un-escape over-escaped punctuation    `1\. \[\[x\]\]`       → `1. [[x]]`
  4. Collapse 3+ blank lines               → a single blank line

For X/Twitter captures (`source_type == "x"`) it also trims the recognizable
leading and trailing page chrome (author card, @handle, like/repost/view counts,
"Upgrade to Premium", "View quotes", …) — conservatively, only contiguous runs of
clearly-chrome lines at the very start/end, never anything in the middle, so real
content is never cut.

Use as a library (`clean_markdown(body, source_type, title, author)`) — this is
what `normalize_raw.py` calls — or as a CLI to repair an existing RAW file in
place:  `python3 clean_md.py path/to/index.md`
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 1. Exploded link block: `[` immediately followed by a newline, then inner
#    (possibly several lines — turndown sometimes splits the link text across
#    lines), then a line that is just `](url)`. `[` + newline is a strong opener
#    (normal links are inline `[t](u)`); inner is non-greedy and the closing must
#    start a line, so the image's own internal `](` and adjacent blocks don't
#    confuse it. Inner is flattened to one line on collapse.
_LINK_BLOCK = re.compile(r"\[[ \t]*\n(.*?)\n[ \t]*\]\(([^)]*)\)", re.S)

# 3. Over-escaped ASCII punctuation that turndown adds spuriously. We unescape the
#    safe set (brackets/parens/period/hash/bang) and leave * _ ~ ` alone so we
#    never destroy intentional emphasis/code escapes.
_UNESCAPE = re.compile(r"\\([\[\]().#!])")

_HEADING_ONLY = re.compile(r"^(#{1,6})[ \t]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _collapse_link_blocks(text: str) -> str:
    prev = None
    # Run to a fixed point: nested/adjacent blocks can need a second pass.
    # Inner whitespace/newlines collapse to single spaces so the result is one
    # line (a link's display text is inline by definition).
    while prev != text:
        prev = text
        text = _LINK_BLOCK.sub(lambda m: f"[{' '.join(m.group(1).split())}]({m.group(2)})", text)
    return text


def _merge_headings_and_unescape(text: str) -> str:
    """Single line-oriented pass: merge `#`-only lines with the following text
    line, and unescape punctuation — but never touch fenced code blocks."""
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if not in_fence:
            m = _HEADING_ONLY.match(line)
            if m:
                # Look past blank lines for the heading's orphaned text.
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and not _HEADING_ONLY.match(lines[j]) and not lines[j].lstrip().startswith("#"):
                    out.append(f"{m.group(1)} {lines[j].strip()}")
                    i = j + 1
                    continue
            line = _UNESCAPE.sub(r"\1", line)
        out.append(line)
        i += 1
    return "\n".join(out)


def _collapse_blanks(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text)


# ── X / Twitter chrome ──────────────────────────────────────────────────────
#
# Conservative by design: we KEEP every image and every prose line (a post's
# cover image and body must never be lost), and only drop lines that are
# unambiguously page furniture — like/repost/view counts, the analytics link, a
# bare @handle, the author-name echo — and only within a bounded window at the
# very start, plus the recognizable footer block at the very end.

_HEAD_WINDOW = 20
_TAIL_WINDOW = 40
_NUMERIC = re.compile(r"^[\d,]+(?:\.\d+)?\s*[KMB]?$")              # 5 / 12 / 1.3K / 1,328
_COUNT_LINK = re.compile(r"^\[[\d,]+(?:\.\d+)?\s*[KMB]?\]\([^)]*\)$")  # [1.3K](…/analytics)
_AUTHOR_CARD = re.compile(r"^\[@[\w]+\]\(https?://[^)]*\)$")       # [@handle](https://x.com/…)
_FOOTER = re.compile(
    r"(Upgrade to Premium|Want to publish|\bViews?\b|View quotes|Relevant|"
    r"^\[?\d{1,2}:\d{2}\s*[AP]M)", re.I
)
# The author avatar / display-name / @handle are each rendered as a link (or
# image-link) to the author's BARE profile URL (x.com/<handle> — no /status,
# /article, /photo, /media after it). That single signal separates them from the
# cover image and inline article images, which link to .../media|photo|article.
# So we drop profile-links and keep media-links → the 题图 and embedded images,
# and their in-body positions, are preserved.
_PROFILE_URL = re.compile(r"^https?://(?:x|twitter)\.com/[A-Za-z0-9_]+/?$", re.I)
# hrefs that are always page furniture, never content: the analytics/quotes views
# and the premium upsell.
_CHROME_HREF = re.compile(r"/(?:analytics|quotes)(?:[/?]|$)|premium_sign_up|/i/premium", re.I)
_LAST_HREF = re.compile(r"\]\(([^)]*)\)\s*$")


def _is_single_link_line(s: str) -> bool:
    return (s.startswith("[") or s.startswith("![")) and s.endswith(")")


def _is_chrome_link(s: str) -> bool:
    """A single link/image-link whose target is the author's bare profile (avatar
    / name / @handle) or a furniture endpoint (analytics, quotes, premium)."""
    if not _is_single_link_line(s):
        return False
    m = _LAST_HREF.search(s)
    if not m:
        return False
    href = m.group(1).strip()
    return bool(_PROFILE_URL.match(href) or _CHROME_HREF.search(href))


def _is_head_chrome(s: str) -> bool:
    if _NUMERIC.match(s) or _COUNT_LINK.match(s):   # like/repost/view counts
        return True
    if _is_chrome_link(s):                          # avatar / name / @handle / analytics
        return True
    return False                                    # keep cover + inline images


def _trim_x_chrome(text: str, title: str, author: str) -> str:
    lines = text.split("\n")

    # Head: within the opening window, drop the author byline (avatar/name/handle,
    # which link to the bare profile) and bare count lines; keep the cover image,
    # inline images, headings, and all prose untouched.
    head = min(_HEAD_WINDOW, len(lines))
    kept = [ln for i, ln in enumerate(lines)
            if i >= head or not _is_head_chrome(ln.strip())]

    # Tail: the footer is a contiguous block at the very end. Anchor on the
    # author card if it appears late in the doc and cut from there; otherwise peel
    # a trailing run of counts/footer markers. Either way, never cross into prose.
    anchor = None
    for i in range(len(kept) - 1, max(-1, len(kept) - _TAIL_WINDOW) - 1, -1):
        if _AUTHOR_CARD.match(kept[i].strip()):
            anchor = i
            break
    if anchor is not None:
        kept = kept[:anchor]
    else:
        end = len(kept)
        bound = max(0, len(kept) - _TAIL_WINDOW)
        for i in range(len(kept) - 1, bound - 1, -1):
            s = kept[i].strip()
            if (s == "" or _NUMERIC.match(s) or _COUNT_LINK.match(s)
                    or _FOOTER.search(s) or _is_chrome_link(s)):
                end = i
            else:
                break
        kept = kept[:end]

    return "\n".join(kept).strip() + "\n"


def clean_markdown(body: str, source_type: str = "", title: str = "", author: str = "") -> str:
    body = _collapse_link_blocks(body)
    body = _merge_headings_and_unescape(body)
    body = _collapse_blanks(body)
    if source_type == "x":
        body = _trim_x_chrome(body, title, author)
        body = _collapse_blanks(body)  # removing count lines can leave gaps
    return body.strip() + "\n"


# ── CLI: repair an existing RAW file in place ───────────────────────────────

# A plain YAML scalar can't START with one of these indicator chars; if it does,
# the whole frontmatter is invalid and Obsidian shows no properties. The usual
# culprit is `author: @handle`. Re-quote such unquoted values, line by line, so
# repairing an old file fixes its properties without touching list values.
_FM_LEAD = set("@`!&*?|>%#,-:[]{}\"'~")


def _safe_frontmatter(fm_block: str) -> str:
    out = []
    for line in fm_block.split("\n"):
        m = re.match(r"^([A-Za-z0-9_]+):[ \t]+(\S.*?)[ \t]*$", line)
        if m:
            key, val = m.group(1), m.group(2)
            if val[0] not in ("\"", "'") and val[0] in _FM_LEAD:
                esc = val.replace("\\", "\\\\").replace("\"", "\\\"")
                line = f'{key}: "{esc}"'
        out.append(line)
    return "\n".join(out)


def _split_frontmatter(text: str) -> tuple[str, str, dict]:
    m = re.match(r"^(---\n.*?\n---\n)(.*)$", text, re.S)
    if not m:
        return "", text, {}
    fm_block, rest = m.group(1), m.group(2)
    meta = {}
    for line in fm_block.splitlines():
        mm = re.match(r"^(\w+):\s*(.*)$", line)
        if mm:
            meta[mm.group(1)] = mm.group(2).strip().strip("\"'")
    return fm_block, rest, meta


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: clean_md.py <raw-file.md> [more.md ...]")
    for arg in sys.argv[1:]:
        p = Path(arg).expanduser()
        text = p.read_text(encoding="utf-8")
        fm, body, meta = _split_frontmatter(text)
        cleaned = clean_markdown(
            body, meta.get("source_type", ""), meta.get("title", ""), meta.get("author", "")
        )
        fm = _safe_frontmatter(fm)  # repair invalid-YAML frontmatter (e.g. author: @handle)
        p.write_text(fm + ("\n" if fm and not fm.endswith("\n") else "") + cleaned, encoding="utf-8")
        print(f"cleaned: {p}")


if __name__ == "__main__":
    main()
