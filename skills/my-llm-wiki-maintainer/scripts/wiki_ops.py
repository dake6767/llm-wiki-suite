#!/usr/bin/env python3
"""Deterministic helpers for the my-llm-wiki-maintainer skill."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = SKILL_DIR / "assets" / "templates"
APP_STATE_DIR = ".llm-wiki"
AGENT_STATE_DIR = ".llm-wiki/agent"
TODAY = dt.date.today().isoformat()


# Content stops at the block's own terminator, or — defensively — at the next
# block-start marker of EITHER kind (`---FILE:` / `---REVIEW:` at line start) or
# EOF. The final capture group holds the terminator actually matched: when it is
# not the proper `---END …---` (the group is empty), the block was unterminated.
# These fallbacks stop an unterminated block from silently swallowing the block(s)
# that follow it, regardless of their kind.
FILE_BLOCK_RE = re.compile(
    r"---FILE:\s*([^\n]+?)\s*---\n(.*?)(---END FILE---|(?=\n---(?:FILE|REVIEW):)|\Z)",
    re.DOTALL,
)
REVIEW_BLOCK_RE = re.compile(
    r"---REVIEW:\s*([\w-]+)\s*\|\s*(.+?)\s*---\n(.*?)(---END REVIEW---|(?=\n---(?:FILE|REVIEW):)|\Z)",
    re.DOTALL,
)
WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]")
INDEX_ENTRY_RE = re.compile(r"^\s*-\s*\[\[([^\]|]+)(?:\|[^\]]+)?\]\].*$")
INDEX_LINK_PREFIX_RE = re.compile(r"^(\s*-\s*\[\[[^\]]+\]\])")
# Fenced code blocks and inline code spans — stripped before scanning for
# wikilinks so a [[...]] shown as a documentation example isn't read as a
# real link.
CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)

# --- Large-source ingest (map-reduce) tuning -------------------------------
# Body-char counts act as a deterministic token proxy so the size gate needs no
# tokenizer dependency (the whole script is stdlib-only). CJK packs more tokens
# per char, so chars_per_token scales down with CJK density and a dense-CJK
# source tips into the two-phase path earlier. All overridable via CLI flags.
GATE_CHARS = 40000       # body chars at/above which ingest switches to map-reduce
CHUNK_CHARS = 12000      # target max body chars per MAP chunk
CHUNK_OVERLAP = 600      # char-window overlap, used ONLY on headingless fallback splits
# CJK + Japanese/Korean + fullwidth ranges for density estimation.
CJK_RE = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯＀-￯]"
)
# H1–H3 only (H4+ stays inside its parent block). Requires a space after the
# hashes, matching CommonMark, and tolerates an optional closing-hash run.
HEADING_RE = re.compile(r"^(#{1,3})[ \t]+(.+?)[ \t]*#*\s*$")
# A fence opens/closes a code block; a `#` inside one is never a heading.
FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


def extract_wikilinks(content: str) -> list[str]:
    """Body wikilink targets, with code examples and escaped-pipe artifacts removed.

    An aliased wikilink inside a Markdown table cell must escape the pipe
    (``[[slug\\|Display]]``) or it splits the cell; the alias-naive regex then
    captures the trailing backslash into the target. Strip it so the link
    resolves to ``slug`` (matching Obsidian and the app renderer).

    Heading/block anchors are resolved to their **page** target: ``[[page#H]]``
    is a link to ``page`` (drop the ``#H``), and a same-file anchor ``[[#H]]``
    (empty page part) is not a cross-page link at all, so it's dropped — both
    match Obsidian, and skipping them stops lint from false-flagging a valid
    in-page heading jump as a broken link.
    """
    scannable = CODE_SPAN_RE.sub("", content)
    targets = []
    for m in WIKILINK_RE.findall(scannable):
        page = m.rstrip("\\").split("#", 1)[0].strip()
        if page:
            targets.append(page)
    return targets


def die(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def normalize_rel(value: str) -> str:
    return value.replace("\\", "/").strip().lstrip("./")


def ensure_md_page(rel: str) -> str:
    """Every wiki page is Markdown. A model that drops the `.md` suffix in a
    `---FILE: wiki/…---` header (or a `--page` arg) must NOT cause an
    extensionless file to be written — Obsidian and the llm_wiki app only read
    `*.md`, so such a file is an invisible junk page (the very bug this guards).
    Repair any `wiki/` page path that doesn't already end in `.md` by appending
    it. Caller decides whether to warn. Non-`wiki/` paths are left untouched."""
    if rel.startswith("wiki/") and not rel.endswith(".md"):
        return rel + ".md"
    return rel


def today_text() -> str:
    return dt.date.today().isoformat()


def epoch_ms() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


def is_project_root(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "purpose.md").exists()
        and (path / "schema.md").exists()
        and (path / "wiki").is_dir()
    )


def resolve_root(path: Path) -> Path:
    path = path.resolve()
    current = path if path.is_dir() else path.parent

    parts = path.parts
    for i in range(len(parts) - 1):
        if parts[i] == "raw" and i + 1 < len(parts) and parts[i + 1] == "sources":
            candidate = Path(*parts[:i])
            if is_project_root(candidate):
                return candidate.resolve()
    for i, part in enumerate(parts):
        if part == "wiki":
            candidate = Path(*parts[:i])
            if is_project_root(candidate):
                return candidate.resolve()

    while True:
        if is_project_root(current):
            return current.resolve()
        if current.parent == current:
            break
        current = current.parent
    die(f"Could not resolve LLM Wiki project root from: {path}")


def _reanchor_suffix(root: Path, path: Path) -> Path | None:
    """A common weaker-model mistake is to keep the right `raw/…` or `wiki/…`
    suffix but drop or wrong-guess the project-root prefix — e.g.
    `/Users/me/raw/sources/video/X.md` instead of
    `/Users/me/<wiki>/raw/sources/video/X.md`. Return that suffix re-anchored
    under `root` (whether or not it exists on disk), or None if there is no
    `raw`/`wiki` segment to anchor on."""
    parts = path.resolve().parts
    for anchor in ("raw", "wiki"):
        if anchor in parts:
            return root.resolve() / Path(*parts[parts.index(anchor):])
    return None


def reanchor_existing(root: Path, path: Path) -> Path | None:
    """If `path` is outside `root` and missing, but the same raw/… or wiki/…
    suffix DOES exist under `root`, return that real path (for auto-repair).
    Returns None when `path` is already inside root, or no repair is possible."""
    root = root.resolve()
    rp = path.resolve()
    try:
        rp.relative_to(root)
        return None  # already inside root — nothing to repair
    except ValueError:
        pass
    cand = _reanchor_suffix(root, rp)
    return cand if (cand is not None and cand.exists()) else None


def rel_to_root(root: Path, path: Path) -> str:
    root = root.resolve()
    try:
        return normalize_rel(str(path.resolve().relative_to(root)))
    except ValueError:
        pass
    # Outside root: auto-repair when the re-anchored suffix exists, else point
    # at the corrected path instead of dumping a bare "outside project root".
    fixed = reanchor_existing(root, path)
    if fixed is not None:
        rel = normalize_rel(str(fixed.relative_to(root)))
        print(f"warning: re-anchored path under project root: {rel}", file=sys.stderr)
        return rel
    suggestion = _reanchor_suffix(root, path)
    hint = f"\n  → did you mean: {suggestion}" if suggestion else ""
    die(f"Path is outside project root: {path}{hint}")


def safe_project_path(root: Path, rel: str) -> Path:
    rel = normalize_rel(rel)
    if rel.startswith("/") or rel.startswith("../") or "/../" in f"/{rel}/":
        die(f"Unsafe relative path: {rel}")
    full = (root / rel).resolve()
    try:
        full.relative_to(root.resolve())
    except ValueError:
        die(f"Path escapes project root: {rel}")
    return full


def app_state_path(root: Path, rel: str) -> Path:
    return root / APP_STATE_DIR / rel


def agent_state_path(root: Path, rel: str) -> Path:
    return root / AGENT_STATE_DIR / rel


def state_path(root: Path, rel: str) -> Path:
    return agent_state_path(root, rel)


# Mirrors `my-llm-wiki/scripts/init_wiki.py`'s NEXT_STEPS. Two init paths exist
# (that skill's, for capture-side scaffolding, and this one) and a wiki created
# by either needs the same nudges — the first version of this guidance went
# into only one of them, so wikis scaffolded here got nothing and waited for
# `health` to notice at 12 sources. Keep the two texts saying the same thing.
#
# What it deliberately does not ask for is a tag vocabulary: someone creating a
# "政治" wiki knows the subject in a sentence and cannot enumerate its tags on
# day one. Tags grow from real content (`wiki_ops.py tags`) instead.
INIT_NEXT_STEPS = """
next steps (purpose.md and schema.md are the only per-wiki files every ingest reads):
  1. fill in purpose.md — Key Questions, In/Out of scope, Thesis. One line each is
     plenty; it is what lets ingest judge "worth its own page" vs "a mention".
  2. leave schema.md's domain table empty for now. Areas are easier to name after
     a dozen-odd sources than on day one.
  3. leave tags alone entirely — the vocabulary grows from what you capture.
     `wiki_ops.py tags <root> --q "<topic>"` shows it; ingest reads it before tagging.
  4. run `wiki_ops.py health <root>` occasionally — it tells you when this wiki has
     enough content to be worth filling the domain table and refreshing overview.md.
"""


def init_project(args: argparse.Namespace) -> None:
    root = Path(args.project_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    for rel in [
        "raw/sources",
        "raw/assets",
        "wiki/entities",
        "wiki/concepts",
        "wiki/sources",
        "wiki/queries",
        "wiki/synthesis",
        "wiki/comparisons",
        ".obsidian",
        APP_STATE_DIR,
        f"{AGENT_STATE_DIR}/research/runs",
        f"{AGENT_STATE_DIR}/page-history",
        f"{AGENT_STATE_DIR}/runs",
        f"{AGENT_STATE_DIR}/ingest-staging",
    ]:
        (root / rel).mkdir(parents=True, exist_ok=True)

    replacements = {"YYYY-MM-DD": today_text()}
    for name, dest in [
        ("purpose.md", "purpose.md"),
        ("schema.md", "schema.md"),
        ("index.md", "wiki/index.md"),
        ("log.md", "wiki/log.md"),
        ("overview.md", "wiki/overview.md"),
    ]:
        target = root / dest
        if target.exists() and not args.force:
            continue
        text = read_text(TEMPLATE_DIR / name)
        for old, new in replacements.items():
            text = text.replace(old, new)
        write_text(target, text)

    obsidian_files = {
        ".obsidian/app.json": {
            "attachmentFolderPath": "raw/assets",
            "userIgnoreFilters": [".cache", ".llm-wiki", ".superpowers"],
            "useMarkdownLinks": False,
            "newLinkFormat": "shortest",
            "showUnsupportedFiles": False,
        },
        ".obsidian/appearance.json": {
            "baseFontSize": 16,
            "theme": "obsidian",
        },
    }
    for rel, data in obsidian_files.items():
        target = root / rel
        if not target.exists() or args.force:
            write_text(target, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    for name, default in [
        ("review.json", []),
        ("lint.json", []),
    ]:
        path = app_state_path(root, name)
        if not path.exists() or args.force:
            write_text(path, json.dumps(default, indent=2, ensure_ascii=False) + "\n")

    for name, default in [
        ("ingest-cache.json", {"entries": {}}),
        ("research/queue.json", []),
    ]:
        path = agent_state_path(root, name)
        if not path.exists() or args.force:
            write_text(path, json.dumps(default, indent=2, ensure_ascii=False) + "\n")
    print(root)
    print(INIT_NEXT_STEPS, end="")


# A YAML frontmatter key at line start (`type:`, `title:`, `related:` …).
FRONTMATTER_KEY_RE = re.compile(r"^[A-Za-z_][\w-]*\s*:")


def repair_frontmatter_fence(text: str) -> tuple[str, bool]:
    """Recover frontmatter whose opening `---` was dropped.

    The `---FILE: <path>---` block header ends in `---`, and the page's own
    frontmatter opens with `---` on the very next line — two `---` lines
    back-to-back. Models routinely collapse them, emitting a page that starts
    straight at the first YAML key (`type: …`) and carries only a *closing*
    `---`. The app and Obsidian then can't parse the frontmatter at all. Detect
    that headless shape and restore the opening fence.

    Returns (text, repaired). Conservative: only fires when the head looks
    unmistakably like YAML frontmatter that's missing solely its opening fence.
    """
    if text.startswith("---"):
        return text, False
    lines = text.splitlines()
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    # First meaningful line must look like a frontmatter key, else it's just body.
    if idx >= len(lines) or not FRONTMATTER_KEY_RE.match(lines[idx]):
        return text, False
    # Walk the candidate frontmatter: keys or indented list items only, until a
    # lone `---` closing fence. A blank line, heading, or prose before the fence
    # means this isn't a headless frontmatter — leave it untouched.
    for line in lines[idx:]:
        if line.strip() == "---":
            return "---\n" + text, True
        if not line.strip():
            return text, False
        if FRONTMATTER_KEY_RE.match(line) or line.startswith((" ", "\t", "-")):
            continue
        return text, False
    return text, False


# A single-line double-quoted frontmatter scalar: `key: "…"`.
QUOTED_SCALAR_LINE_RE = re.compile(r'^([A-Za-z_][\w-]*):[ \t]+"(.*)"[ \t]*$')
# A `"` that isn't backslash-escaped — an unescaped interior quote breaks the
# surrounding double-quoted YAML scalar.
UNESCAPED_QUOTE_RE = re.compile(r'(?<!\\)"')


def _yaml_requote(inner: str) -> str:
    """Re-quote a literal string value so it's a valid YAML scalar.

    `inner` is the text between the outer double quotes (which may itself hold
    stray `"`). Decode any existing escapes to recover the literal, then prefer
    single quotes (no escaping needed unless the value contains `'`)."""
    literal = re.sub(r"\\(.)", r"\1", inner)  # \" -> ", \\ -> \
    if "'" not in literal:
        return "'" + literal + "'"
    esc = literal.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + esc + '"'


def repair_frontmatter_quoting(text: str) -> tuple[str, bool]:
    """Fix double-quoted frontmatter scalars with unescaped interior quotes.

    Models routinely wrap a title in `"…"` while the title itself contains raw
    `"` (e.g. `title: "老十三胤祥：从"康熙弃子"到"雍正副皇""`). YAML reads the
    string as ending at the first interior quote and then chokes — the whole
    page's frontmatter fails to parse. Re-quote any such scalar safely.

    Returns (text, repaired). Only rewrites lines that are genuinely broken;
    already-escaped or quote-free values are left untouched.
    """
    fm, body = strip_frontmatter(text)
    if not fm:
        return text, False
    changed = False
    out: list[str] = []
    for line in fm.splitlines():
        m = QUOTED_SCALAR_LINE_RE.match(line)
        if m and UNESCAPED_QUOTE_RE.search(m.group(2)):
            out.append(f"{m.group(1)}: {_yaml_requote(m.group(2))}")
            changed = True
        else:
            out.append(line)
    if not changed:
        return text, False
    return "\n".join(out) + "\n" + body, True


def frontmatter_defect(content: str) -> str | None:
    """One-line diagnosis of a frontmatter defect, or None if sound.

    Single entry point for the two failure classes `apply-blocks` self-heals —
    a missing opening `---` (headless frontmatter) and a double-quoted scalar
    with unescaped interior quotes — so `lint` surfaces both from one check,
    using the very same detectors as the repairs. Callers gate on page type
    (index.md/log.md have no frontmatter and are exempt)."""
    if not content.startswith("---"):
        _, headless = repair_frontmatter_fence(content)
        if headless:
            return ("Frontmatter is missing its opening '---' (headless "
                    "frontmatter). Re-apply the page or prepend '---'.")
        return "Missing YAML frontmatter."
    _, requoted = repair_frontmatter_quoting(content)
    if requoted:
        return ("Frontmatter has a double-quoted scalar with unescaped interior "
                "quotes (breaks YAML). Re-apply or single-quote it.")
    return None


# RAW capture frontmatter (schema.md's "RAW frontmatter" block) stamps
# `status: raw` + `tags: [inbox, ...]`, and `source_type` is one of these bare
# English platform words. None of that vocabulary has any business surviving
# into a wiki-layer page's `tags:` — the wiki Tag & Domain Policy calls for
# hygiene-checked topical words, and `inbox` specifically means "not yet
# processed into the wiki" (a claim that's false the moment a wiki page for it
# exists at all). Seen in practice: an ingest pass copied `source_type` +
# `tags: [inbox]` straight from a RAW file into the compiled wiki/sources page
# instead of generating real tags.
RAW_INBOX_TAG = "inbox"
RAW_SOURCE_TYPE_TAGS = {"video", "x", "wechat", "xiaohongshu", "web", "note", "bilibili"}


def extract_frontmatter_list(text: str, key: str) -> list[str]:
    """Read a frontmatter list field (`key: [a, b]` or a `- item` block list).

    Uses the same inline/block-list parsing `normalize_related` applies to
    `related`, generalized to an arbitrary top-level key so lint can inspect
    `tags` the same way.
    """
    fm, _ = strip_frontmatter(text)
    if not fm:
        return []
    lines = fm.strip().splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^\s*{re.escape(key)}\s*:", line):
            values, _consumed = _block_list_values_at(lines, i, key)
            return values
    return []


def raw_tag_leaks(content: str) -> tuple[list[str], list[str]]:
    """Wiki-layer `tags:` entries that are literal RAW-capture vocabulary.

    Returns `(inbox_hits, bare_source_type_hits)`, case-insensitive since the
    leak is copy-paste, not deliberate styling:

    - `inbox_hits`: always a defect. `inbox` means "not yet processed into the
      wiki" — false the moment the wiki page exists — so its presence is never
      tolerable regardless of what else is tagged alongside it.
    - `bare_source_type_hits`: a format word like `video`/`bilibili` is a
      harmless genre tag *when a page also carries real topical tags*
      (`[清初, video, bilibili, 康熙朝, 索额图, 太子废立]` is fine). It only
      means the page has no substantive tagging at all when it's the *entire*
      tag set — that's the unacceptable case, so this list is empty unless
      every tag on the page is RAW vocabulary.
    """
    tags = extract_frontmatter_list(content, "tags")
    lowered = [(t, t.strip().lower()) for t in tags]
    inbox_hits = list(dict.fromkeys(t for t, low in lowered if low == RAW_INBOX_TAG))
    source_type_hits = list(dict.fromkeys(t for t, low in lowered if low in RAW_SOURCE_TYPE_TAGS))
    genuine = [t for t, low in lowered if low != RAW_INBOX_TAG and low not in RAW_SOURCE_TYPE_TAGS]
    bare_source_type_hits = source_type_hits if (source_type_hits and not genuine) else []
    return inbox_hits, bare_source_type_hits


def missing_tags(content: str) -> bool:
    """A content page whose `tags:` is `[]`, blank, or absent altogether.

    This is `raw_tag_leaks`' blind spot. That check exists to catch the "no
    real tags were ever generated" failure mode, but it can only fire on tags
    that are *present* and RAW vocabulary — an empty set trips neither branch,
    so `tags: []` sails straight through the very rule written to catch it.
    Seen in practice: an ingest pass emitted the FILE-block template's literal
    `tags: []` placeholder while filling `related:` with real values, and both
    lint and apply-blocks reported clean.

    Callers exempt `index.md` / `log.md` / `overview.md` — overview's
    frontmatter is deliberately four fields with no `tags` key at all.
    """
    return not extract_frontmatter_list(content, "tags")


def strip_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        return "", text
    match = re.match(r"^(---\s*\n.*?\n---\s*\n?)(.*)$", text, re.DOTALL)
    if not match:
        return "", text
    return match.group(1), match.group(2)


def hash_text(text: str) -> str:
    """sha256 of a string (the in-memory parallel to hash_file)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cjk_ratio(text: str) -> float:
    """Fraction of non-space chars that are CJK/Japanese/Korean/fullwidth.

    Whitespace and markup are excluded from the denominator so they don't dilute
    the density estimate. ~1.0 for a Chinese report, ~0.0 for English prose.
    """
    dense = sum(1 for c in text if not c.isspace())
    if not dense:
        return 0.0
    cjk = sum(1 for _ in CJK_RE.finditer(text))
    return cjk / dense


def chars_per_token(ratio: float) -> float:
    """Chars-per-token estimate from CJK density. Latin ~4, dense CJK ~1.6.

    Deliberately conservative for CJK (under-estimates capacity) so dense sources
    tip into map-reduce earlier rather than risk a one-pass context overflow.
    """
    return 4.0 - 2.4 * max(0.0, min(1.0, ratio))


def _scan_segments(body: str) -> list[dict]:
    """Split body into fence-aware H1–H3 segments.

    Each segment is a heading line plus everything up to the next H1–H3 heading
    (or the leading text before the first heading, with level 0). Headings inside
    a fenced code block are ignored. Char offsets are relative to `body`.
    """
    lines = body.splitlines(keepends=True)
    segments: list[dict] = []
    cur_lines: list[str] = []
    cur_level = 0
    cur_title = ""
    seg_start = 0
    pos = 0
    in_fence = False
    fence_char = ""
    for line in lines:
        toggled = False
        fm = FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)[0]
            if not in_fence:
                in_fence, fence_char, toggled = True, marker, True
            elif marker == fence_char:
                in_fence, toggled = False, True
        is_heading = False
        if not toggled and not in_fence:
            hm = HEADING_RE.match(line)
            if hm:
                is_heading = True
        if is_heading:
            if cur_lines:
                segments.append({"level": cur_level, "title": cur_title,
                                 "text": "".join(cur_lines), "start": seg_start, "end": pos})
            hm = HEADING_RE.match(line)
            cur_level = len(hm.group(1))
            cur_title = hm.group(2).strip()
            cur_lines = [line]
            seg_start = pos
        else:
            if not cur_lines:
                seg_start = pos
            cur_lines.append(line)
        pos += len(line)
    if cur_lines:
        segments.append({"level": cur_level, "title": cur_title,
                         "text": "".join(cur_lines), "start": seg_start, "end": pos})
    return segments


def _window_segment(seg: dict, chunk_chars: int, overlap: int) -> list[dict]:
    """Char-window an oversized segment at safe (non-fence) line boundaries.

    A fenced code block longer than the budget is never split mid-fence; the
    window simply overruns the budget until the fence closes. Each window after
    the first is prefixed with up to `overlap` chars of the previous window's
    tail (snapped to a newline) so an entity mentioned right at a boundary still
    appears in both chunks for dedup.
    """
    base_start = seg["start"]
    text = seg["text"]
    lines = text.splitlines(keepends=True)
    raw: list[tuple[int, int]] = []
    cur_start = 0
    cur_chars = 0
    has_lines = False
    pos = 0
    in_fence = False
    fence_char = ""
    for line in lines:
        if has_lines and not in_fence and cur_chars + len(line) > chunk_chars:
            raw.append((cur_start, pos))
            cur_start, cur_chars, has_lines = pos, 0, False
        cur_chars += len(line)
        has_lines = True
        fm = FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)[0]
            if not in_fence:
                in_fence, fence_char = True, marker
            elif marker == fence_char:
                in_fence = False
        pos += len(line)
    if has_lines:
        raw.append((cur_start, pos))
    out: list[dict] = []
    for part_i, (s, e) in enumerate(raw):
        ov_start = s
        if part_i > 0 and overlap > 0:
            ov_start = max(0, s - overlap)
            # Snap to the FIRST line boundary at/after (s - overlap) so the prefix
            # spans ~overlap chars of real preceding content. (rfind here would
            # match the boundary newline itself and yield zero overlap.)
            nl = text.find("\n", ov_start, s - 1)
            if nl != -1:
                ov_start = nl + 1
        out.append({
            "breadcrumb": seg["breadcrumb"],
            "headingLevel": seg["level"],
            "charStart": base_start + ov_start,
            "charEnd": base_start + e,
            "text": text[ov_start:e],
            "part": part_i,
        })
    return out


def split_markdown(body: str, chunk_chars: int = CHUNK_CHARS,
                   overlap: int = CHUNK_OVERLAP) -> list[dict]:
    """Deterministically chunk a Markdown body for MAP-phase extraction.

    Pure function (no I/O): packs consecutive H1–H3 segments into chunks under
    `chunk_chars`, carrying an `H1 > H2 > H3` breadcrumb; an oversized single
    segment falls back to fence-safe char windows. Identical input yields
    identical chunks (stable for hashing). Returns chunk dicts with keys:
    index, breadcrumb, headingLevel, charStart, charEnd, chars, part, text.
    """
    segments = _scan_segments(body)
    stack: list[str] = []
    for seg in segments:
        lvl = seg["level"]
        if lvl > 0:
            stack = stack[: lvl - 1]
            while len(stack) < lvl - 1:
                stack.append("")
            stack.append(seg["title"])
            seg["breadcrumb"] = " > ".join(s for s in stack if s)
        else:
            seg["breadcrumb"] = ""

    chunks: list[dict] = []
    pack: list[dict] = []
    pack_chars = 0

    def flush_pack() -> None:
        nonlocal pack, pack_chars
        if not pack:
            return
        chunks.append({
            "breadcrumb": pack[0]["breadcrumb"],
            "headingLevel": pack[0]["level"],
            "charStart": pack[0]["start"],
            "charEnd": pack[-1]["end"],
            "text": "".join(s["text"] for s in pack),
            "part": None,
        })
        pack = []
        pack_chars = 0

    for seg in segments:
        seg_len = len(seg["text"])
        if seg_len > chunk_chars:
            flush_pack()
            chunks.extend(_window_segment(seg, chunk_chars, overlap))
            continue
        if pack and pack_chars + seg_len > chunk_chars:
            flush_pack()
        pack.append(seg)
        pack_chars += seg_len
    flush_pack()

    for i, c in enumerate(chunks):
        c["index"] = i
        c["chars"] = len(c["text"])
    return chunks


def staging_dir(root: Path, slug: str) -> Path:
    """Per-source map-reduce staging dir under the agent state tree."""
    return agent_state_path(root, f"ingest-staging/{slug}")


def frontmatter_value(text: str, key: str) -> str | None:
    fm, _ = strip_frontmatter(text)
    if not fm:
        return None
    m = re.search(rf"^{re.escape(key)}:\s*[\"']?(.+?)[\"']?\s*$", fm, re.MULTILINE)
    return m.group(1).strip() if m else None


def slug_key(target: str) -> str:
    target = target.strip().split("#", 1)[0]
    target = target[:-3] if target.endswith(".md") else target
    target = target.lower().replace("\\", "/")
    target = re.sub(r"^wiki/", "", target)
    target = re.sub(r"\s+", "-", target)
    return target


def parse_frontmatter_mapping(text: str) -> dict[str, str]:
    fm, _ = strip_frontmatter(text)
    if not fm:
        return {}
    body = fm
    if body.startswith("---"):
        body = re.sub(r"^---\s*\n", "", body)
        body = re.sub(r"\n---\s*$", "", body.strip())
    out: dict[str, str] = {}
    for line in body.splitlines():
        if ":" not in line or line.startswith(" "):
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip().strip("\"'")
    return out


def parse_inline_array(value: str | None) -> list[str]:
    if not value:
        return []
    raw = value.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [item.strip().strip("\"'") for item in raw.split(",") if item.strip()]


def set_frontmatter_scalar(text: str, key: str, value: str) -> str:
    fm, body = strip_frontmatter(text)
    if not fm:
        return text
    lines = fm.strip().splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}\s*:", line):
            lines[i] = f"{key}: {value}"
            replaced = True
            break
    if not replaced:
        lines.insert(max(1, len(lines) - 1), f"{key}: {value}")
    return "\n".join(lines).rstrip() + "\n" + body


def set_frontmatter_array(text: str, key: str, values: list[str]) -> str:
    fm, body = strip_frontmatter(text)
    if not fm:
        return text
    rendered = f"{key}: [{', '.join(values)}]"
    lines = fm.strip().splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}\s*:", line):
            lines[i] = rendered
            replaced = True
            break
    if not replaced:
        lines.insert(max(1, len(lines) - 1), rendered)
    return "\n".join(lines).rstrip() + "\n" + body


def basename_slug(target: str) -> str:
    """Reduce any link/path reference to a bare basename slug.

    The llm_wiki app's `resolveRelatedSlug` only resolves `related:` entries
    that are bare slugs (`野生小虎`), `slug.md`, or a full `wiki/.../slug.md`
    path. A path-style wikilink like `[[entities/野生小虎]]` unwraps to
    `entities/野生小虎`, hits the resolver's slash branch, and is looked up as
    `<project>/entities/野生小虎` (no `wiki/` prefix, no `.md`) → never found.
    So we collapse every shape down to the bare basename, which always
    resolves. Case is preserved because the app matches filenames exactly.
    """
    m = re.match(r"^\[\[([^\]|]+)(?:\|[^\]]*)?\]\]$", target.strip())
    inner = (m.group(1) if m else target).strip().strip("\"'")
    inner = inner.split("#", 1)[0].replace("\\", "/")
    if inner.endswith(".md"):
        inner = inner[:-3]
    return inner.rstrip("/").split("/")[-1].strip()


def _block_list_values_at(lines: list[str], idx: int, key: str) -> tuple[list[str], int]:
    """Read a `<key>:` value at lines[idx]. Handles inline `[a, b]` and the
    block list form. Returns (values, lines_consumed)."""
    inline = re.match(rf"^\s*{re.escape(key)}\s*:\s*(.*)$", lines[idx]).group(1).strip()
    if inline and inline != "[]":
        return parse_inline_array(inline), 1
    j = idx + 1
    block: list[str] = []
    while j < len(lines) and re.match(r"^\s*-\s+", lines[j]):
        block.append(re.sub(r"^\s*-\s+", "", lines[j]).strip().strip("\"'"))
        j += 1
    return block, (j - idx)


def normalize_related(text: str) -> str:
    """Rewrite a page's `related:` frontmatter into app-resolvable bare slugs.

    This is the deterministic guardrail: no matter how the model wrote the
    field (`[[entities/x]]`, `wiki/entities/x.md`, block list, …), the written
    page ends up with `related: [x, y]` so the app's relation panel resolves
    every chip. Other frontmatter and the body are left untouched.
    """
    fm, body = strip_frontmatter(text)
    if not fm:
        return text
    lines = fm.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if re.match(r"^\s*related\s*:", lines[i]):
            indent = re.match(r"^(\s*)", lines[i]).group(1)
            values, consumed = _block_list_values_at(lines, i, "related")
            slugs = list(dict.fromkeys(s for v in values if (s := basename_slug(v))))
            out.append(f"{indent}related: [{', '.join(slugs)}]")
            i += consumed
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out).rstrip() + "\n" + body


def deterministic_page_merge(existing: str, incoming: str) -> str:
    """Fallback merge: union arrays, lock identity fields, keep incoming body."""
    existing_fm = parse_frontmatter_mapping(existing)
    incoming_fm = parse_frontmatter_mapping(incoming)
    merged = incoming
    for key in ["sources", "tags", "related"]:
        values = [
            *parse_inline_array(existing_fm.get(key)),
            *parse_inline_array(incoming_fm.get(key)),
        ]
        if key == "related":
            values = [s for v in values if (s := basename_slug(v))]
        values = list(dict.fromkeys(values))
        merged = set_frontmatter_array(merged, key, values)
    for key in ["type", "title", "created"]:
        if existing_fm.get(key):
            merged = set_frontmatter_scalar(merged, key, existing_fm[key])
    merged = set_frontmatter_scalar(merged, "updated", today_text())
    return merged


def compact_index(index: str, no_desc: bool = False) -> str:
    lines: list[str] = []
    for line in index.splitlines():
        if line.startswith("#"):
            lines.append(line.rstrip())
            continue
        if INDEX_ENTRY_RE.match(line):
            if no_desc:
                # Drop the ` — description` tail, keep bullet + wikilink only.
                # Mirrors the app's compactIndexListing for the overview digest.
                m = INDEX_LINK_PREFIX_RE.match(line)
                lines.append((m.group(1) if m else line).rstrip())
            else:
                lines.append(line)
    return "\n".join(lines).strip() + "\n"


def parse_sections(text: str) -> tuple[str, list[tuple[str, list[str]]]]:
    fm, body = strip_frontmatter(text)
    sections: list[tuple[str, list[str]]] = []
    current_title = ""
    current_lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if current_title or current_lines:
                sections.append((current_title, current_lines))
            current_title = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_title or current_lines:
        sections.append((current_title, current_lines))
    return fm, sections


# Canonical index section titles are English; map localized headings back so a
# delta (or an already-corrupted index) that used e.g. 实体/概念 merges into the
# canonical section instead of spawning a parallel one.
INDEX_SECTION_ALIASES = {
    "实体": "Entities", "概念": "Concepts", "来源": "Sources", "查询": "Queries",
    "比较": "Comparisons", "综合": "Synthesis",
    "實體": "Entities", "來源": "Sources", "查詢": "Queries",
    "比較": "Comparisons", "綜合": "Synthesis",
}


def canonical_section_title(title: str) -> str:
    title = title.strip()
    return INDEX_SECTION_ALIASES.get(title, title)


def merge_index(existing: str, delta: str) -> str:
    existing_fm, existing_sections = parse_sections(existing)
    _delta_fm, delta_sections = parse_sections(delta)

    order = ["Entities", "Concepts", "Sources", "Queries", "Comparisons", "Synthesis"]

    def line_key(line: str) -> str | None:
        m = INDEX_ENTRY_RE.match(line)
        return slug_key(m.group(1)) if m else None

    section_map: dict[str, list[str]] = {}

    def apply_sections(sections: list[tuple[str, list[str]]]) -> None:
        # Fold sections into section_map keyed by canonical title. On a slug
        # collision the later line wins (replaces in place), so re-running this
        # over a file that has both an English and a localized block collapses
        # them into one and keeps the version that appears last in the file.
        for title, lines in sections:
            canon = canonical_section_title(title)
            if not canon:
                continue
            target_lines = section_map.setdefault(canon, [])
            key_to_idx = {key: i for i, line in enumerate(target_lines) if (key := line_key(line))}
            for line in lines:
                if not INDEX_ENTRY_RE.match(line):
                    continue
                key = line_key(line)
                if not key:
                    continue
                if key in key_to_idx:
                    target_lines[key_to_idx[key]] = line
                else:
                    target_lines.append(line)
                    key_to_idx[key] = len(target_lines) - 1

    apply_sections(existing_sections)
    if not section_map:
        section_map = {title: [] for title in order}
    apply_sections(delta_sections)

    titles = [t for t in order if t in section_map]
    titles.extend(t for t in section_map if t not in titles and t)
    body = "# Wiki Index\n\n"
    for title in titles:
        body += f"## {title}\n"
        entries = [line for line in section_map.get(title, []) if line.strip()]
        if entries:
            body += "\n".join(entries) + "\n"
        body += "\n"

    if existing_fm:
        return existing_fm.rstrip() + "\n\n" + body.rstrip() + "\n"
    return body.rstrip() + "\n"


def cmd_merge_index(args: argparse.Namespace) -> None:
    root = resolve_root(Path(args.project_root))
    existing_path = root / "wiki/index.md"
    existing = read_text(existing_path) if existing_path.exists() else ""
    delta = read_text(Path(args.delta_file)) if args.delta_file else sys.stdin.read()
    merged = merge_index(existing, delta)
    if args.write:
        write_text(existing_path, merged)
    else:
        sys.stdout.write(merged)


def parse_review_blocks(
    text: str,
    root: Path,
    source_path: str | None = None,
    warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    now = epoch_ms()
    for idx, match in enumerate(REVIEW_BLOCK_RE.finditer(text), start=1):
        kind = match.group(1).strip().lower()
        title = match.group(2).strip()
        body = match.group(3).strip()
        if warnings is not None and match.group(4) != "---END REVIEW---":
            warnings.append(
                f"REVIEW block {title!r} was not closed with ---END REVIEW--- "
                "(recovered at next block / EOF); check it parsed as intended."
            )
        options = ["Deep Research", "Create Page", "Skip"]
        options_match = re.search(r"^OPTIONS:\s*(.+)$", body, re.MULTILINE)
        if options_match:
            options = [x.strip() for x in options_match.group(1).split("|") if x.strip()]
        pages_match = re.search(r"^PAGES:\s*(.+)$", body, re.MULTILINE)
        search_match = re.search(r"^SEARCH:\s*(.+)$", body, re.MULTILINE)
        description = re.sub(r"^(OPTIONS|PAGES|SEARCH):.*$", "", body, flags=re.MULTILINE).strip()
        item_id = f"review-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}-{idx:04d}"
        items.append({
            "id": item_id,
            "type": kind if kind in {"contradiction", "duplicate", "missing-page", "confirm", "suggestion"} else "confirm",
            "title": title,
            "description": description,
            "sourcePath": source_path,
            "affectedPages": [x.strip() for x in re.split(r"[|,]", pages_match.group(1)) if x.strip()] if pages_match else [],
            "searchQueries": [x.strip() for x in search_match.group(1).split("|") if x.strip()] if search_match else [],
            "options": [{"label": x, "action": x.lower().replace(" ", "-")} for x in options],
            "resolved": False,
            "createdAt": now,
        })
    return items


def review_key(item: dict[str, Any]) -> str:
    title = re.sub(
        r"^(missing[\s-]?page[:：]\s*|duplicate[\s-]?page[:：]\s*|possible[\s-]?duplicate[:：]\s*|缺失页面[:：]\s*|缺少页面[:：]\s*|重复页面[:：]\s*|疑似重复[:：]\s*)",
        "",
        str(item.get("title", "")),
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\s+", " ", title).strip().lower()
    return f"{item.get('type')}::{title}"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(read_text(path))
    except Exception:
        return default


def is_review_resolved(item: dict[str, Any]) -> bool:
    if isinstance(item.get("resolved"), bool):
        return bool(item.get("resolved"))
    return item.get("status") == "resolved"


def review_status(item: dict[str, Any]) -> str:
    return "resolved" if is_review_resolved(item) else "open"


def add_reviews(root: Path, incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    path = app_state_path(root, "review.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    items = load_json(path, [])
    index = {review_key(item): i for i, item in enumerate(items) if not is_review_resolved(item)}
    for item in incoming:
        key = review_key(item)
        if key in index:
            old = items[index[key]]
            for field in ["affectedPages", "searchQueries"]:
                merged = list(dict.fromkeys([*(old.get(field) or []), *(item.get(field) or [])]))
                old[field] = merged
            if item.get("description"):
                old["description"] = item["description"]
            if item.get("sourcePath"):
                old["sourcePath"] = item["sourcePath"]
        else:
            items.append(item)
            index[key] = len(items) - 1
    write_text(path, json.dumps(items, indent=2, ensure_ascii=False) + "\n")
    return items


def cmd_review(args: argparse.Namespace) -> None:
    root = resolve_root(Path(args.project_root))
    path = app_state_path(root, "review.json")
    # An aliased subcommand (e.g. `add`, `queue`) lands here as its alias string;
    # canonicalize so the dispatch below stays keyed on the real names.
    review_cmd = _CMD_ALIASES.get(args.review_cmd, args.review_cmd)
    if review_cmd == "list":
        items = load_json(path, [])
        status = args.status
        for item in items:
            current = review_status(item)
            if status != "all" and current != status:
                continue
            print(f"{item.get('id')} [{item.get('type')}] {current} - {item.get('title')}")
    elif review_cmd == "add-blocks":
        text = read_text(Path(args.blocks_file)) if args.blocks_file else sys.stdin.read()
        source = normalize_rel(args.source) if args.source else None
        rb_warnings: list[str] = []
        items = add_reviews(root, parse_review_blocks(text, root, source, rb_warnings))
        print(json.dumps({"count": len(items), "warnings": rb_warnings}, ensure_ascii=False))
    elif review_cmd == "resolve":
        items = load_json(path, [])
        found = False
        for item in items:
            if item.get("id") == args.id:
                item["resolved"] = True
                item.pop("status", None)
                item["resolvedAction"] = args.action
                found = True
                break
        if not found:
            die(f"Review id not found: {args.id}")
        write_text(path, json.dumps(items, indent=2, ensure_ascii=False) + "\n")
    elif review_cmd == "get":
        # 单条取用：处理一个待办不需要把整个 review.json 读进上下文。
        items = load_json(path, [])
        for item in items:
            if item.get("id") == args.id:
                print(json.dumps(item, indent=2, ensure_ascii=False))
                return
        die(f"Review id not found: {args.id}")
    elif review_cmd == "find":
        # 过滤取用：按类型/状态/关键词圈出少量条目，JSON 输出完整字段。
        items = load_json(path, [])
        terms = [t.lower() for t in (args.q or "").split() if t.strip()]
        matches = []
        for item in items:
            if args.status != "all" and review_status(item) != args.status:
                continue
            if args.type and item.get("type") != args.type:
                continue
            if terms:
                haystack = " ".join(
                    str(v)
                    for v in (
                        item.get("title"),
                        item.get("description"),
                        item.get("sourcePath"),
                        " ".join(item.get("affectedPages") or []),
                        " ".join(item.get("searchQueries") or []),
                    )
                    if v
                ).lower()
                if not all(term in haystack for term in terms):
                    continue
            matches.append(item)
        limit = max(1, args.limit)
        print(
            json.dumps(
                {"total": len(matches), "items": matches[:limit]},
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        die(f"Unknown review command: {args.review_cmd}")


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_cache(args: argparse.Namespace) -> None:
    root = resolve_root(Path(args.project_root))
    source = Path(args.source).resolve()
    fixed = reanchor_existing(root, source)
    if fixed is not None:
        source = fixed
    rel = rel_to_root(root, source)
    cache_path = agent_state_path(root, "ingest-cache.json")
    cache = load_json(cache_path, {"entries": {}})
    entries = cache.setdefault("entries", {})
    cache_cmd = _CMD_ALIASES.get(args.cache_cmd, args.cache_cmd)
    if cache_cmd == "check":
        entry = entries.get(rel)
        current = hash_file(source)
        ok = bool(entry and entry.get("hash") == current)
        print(json.dumps({"hit": ok, "entry": entry, "source": rel}, indent=2, ensure_ascii=False))
    elif cache_cmd == "save":
        files = list(args.files)
        files_file = getattr(args, "files_file", None)
        if files_file:
            if files:
                die("Pass page paths as positional args or --files-file, not both")
            payload = Path(files_file).read_text(encoding="utf-8").strip()
            if payload.startswith("["):
                try:
                    decoded = json.loads(payload)
                except json.JSONDecodeError as exc:
                    die(f"--files-file contains invalid JSON: {exc.msg}")
                if not isinstance(decoded, list) or not all(isinstance(x, str) for x in decoded):
                    die("--files-file JSON must be an array of path strings")
                files = decoded
            else:
                files = [line.strip() for line in payload.splitlines() if line.strip()]
        normalized_files = [normalize_rel(x) for x in files]
        entries[rel] = {
            "hash": hash_file(source),
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "filesWritten": normalized_files,
        }
        write_text(cache_path, json.dumps(cache, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({
            "saved": True,
            "source": rel,
            "filesWritten": normalized_files,
        }, indent=2, ensure_ascii=False))
    else:
        die(f"Unknown cache command: {args.cache_cmd}")


def cmd_cache_pending(args: argparse.Namespace) -> None:
    """List raw sources under raw/sources/ not yet ingested (no cache entry) or
    changed since last ingest (hash mismatch). Read-only.

    This is the batch-discovery entry point a capture skill (e.g. my-llm-wiki)
    hands off to when the user wants to "process the inbox": ingest-cache.json is
    the authoritative "already synthesized" ledger, so pending = raw sources not
    in it (or stale). Wiki selection is NOT done here — the caller owns multi-wiki
    routing and always passes one explicit project_root; this only inspects that
    single root."""
    root = resolve_root(Path(args.project_root))
    cache_path = agent_state_path(root, "ingest-cache.json")
    cache = load_json(cache_path, {"entries": {}})
    entries = cache.get("entries", {})
    sources_dir = root / "raw" / "sources"
    pending: list[dict[str, str]] = []
    if sources_dir.is_dir():
        for md in sorted(sources_dir.rglob("*.md")):
            rel = rel_to_root(root, md)
            entry = entries.get(rel)
            if entry and entry.get("hash") == hash_file(md):
                continue  # already ingested, unchanged
            pending.append({"source": rel, "status": "changed" if entry else "new"})
    print(json.dumps(
        {"root": str(root), "count": len(pending), "pending": pending},
        indent=2, ensure_ascii=False,
    ))


def _resolve_raw(root: Path, raw: str) -> Path:
    p = Path(raw)
    p = p if p.is_absolute() else (root / raw)
    if not p.exists():
        fixed = reanchor_existing(root, p)
        if fixed is not None:
            print(
                f"warning: re-anchored raw path under project root: "
                f"{fixed.relative_to(root.resolve())}",
                file=sys.stderr,
            )
            return fixed
    return p


def cmd_probe_source(args: argparse.Namespace) -> None:
    """Deterministic size gate: classify a raw source as small (single-pass) or
    large (map-reduce) by BODY char count, the most deterministic signal. Token
    estimate is reported for observability only — the decision is on bodyChars."""
    root = resolve_root(Path(args.project_root))
    raw_path = _resolve_raw(root, args.raw)
    rel = rel_to_root(root, raw_path)
    _, body = strip_frontmatter(read_text(raw_path))
    body_chars = len(body)
    ratio = cjk_ratio(body)
    cpt = chars_per_token(ratio)
    est = round(body_chars / cpt) if cpt else 0
    path = "large" if body_chars >= args.threshold else "small"
    print(json.dumps({
        "source": rel,
        "bodyChars": body_chars,
        "cjkRatio": round(ratio, 3),
        "estTokens": est,
        "path": path,
        "threshold": args.threshold,
        "recommendedChunkChars": args.chunk_chars,
        "recommendedOverlapChars": args.overlap,
    }, indent=2, ensure_ascii=False))


def _manifest_chunk_entry(chunk: dict) -> dict:
    name = f"chunk-{chunk['index']:03d}"
    return {
        "index": chunk["index"],
        "file": f"chunks/{name}.md",
        "mapFile": f"map/{name}.json",
        "breadcrumb": chunk["breadcrumb"],
        "headingLevel": chunk["headingLevel"],
        "part": chunk["part"],
        "chars": chunk["chars"],
        "charStart": chunk["charStart"],
        "charEnd": chunk["charEnd"],
        "textHash": hash_text(chunk["text"]),
        "mapStatus": "pending",
        "mapHash": None,
    }


def cmd_split_source(args: argparse.Namespace) -> None:
    """Chunk a large raw source into staging for the MAP phase.

    Idempotent + resumable: if a manifest already exists and the raw file hash is
    unchanged, the existing staging (including MAP progress) is reused. A changed
    hash re-splits everything; --force wipes staging first. Without --write it is
    a dry run that only prints the manifest."""
    root = resolve_root(Path(args.project_root))
    raw_path = _resolve_raw(root, args.raw)
    rel = rel_to_root(root, raw_path)
    slug = compute_source_slug(args.url, rel)
    sdir = staging_dir(root, slug)
    manifest_path = sdir / "manifest.json"
    raw_hash = hash_file(raw_path)

    if manifest_path.exists() and not args.force:
        manifest = load_json(manifest_path, {})
        if manifest.get("rawHash") == raw_hash:
            manifest["status"] = "reused"
            manifest["stagingDir"] = rel_to_root(root, sdir)
            print(json.dumps(manifest, indent=2, ensure_ascii=False))
            return

    if args.force and sdir.exists():
        shutil.rmtree(sdir)

    _, body = strip_frontmatter(read_text(raw_path))
    chunks = split_markdown(body, args.chunk_chars, args.overlap)
    entries = [_manifest_chunk_entry(c) for c in chunks]
    manifest = {
        "source": rel,
        "sourceSlug": slug,
        "rawHash": raw_hash,
        "chunkChars": args.chunk_chars,
        "overlap": args.overlap,
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "chunkCount": len(entries),
        "chunks": entries,
    }

    if args.write:
        for chunk, entry in zip(chunks, entries):
            write_text(sdir / entry["file"], chunk["text"])
        (sdir / "map").mkdir(parents=True, exist_ok=True)
        write_text(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        manifest["status"] = "written"
        manifest["stagingDir"] = rel_to_root(root, sdir)
    else:
        manifest["status"] = "dryRun"

    print(json.dumps(manifest, indent=2, ensure_ascii=False))


def cmd_stage_status(args: argparse.Namespace) -> None:
    """Report MAP progress for a staged source, or mark one chunk's MAP done.

    Report mode is both the resumability driver (which chunks still need MAP) and
    the REDUCE gate (ready only when nothing is pending/stale). A chunk is `stale`
    if it was marked done but its map file is now missing or the chunk text hash
    changed (e.g. after a re-split). --mark-done refuses unless the map file
    exists and parses as JSON (conservative failure)."""
    root = resolve_root(Path(args.project_root))
    raw_path = _resolve_raw(root, args.source)
    rel = rel_to_root(root, raw_path)
    slug = compute_source_slug(None, rel)
    sdir = staging_dir(root, slug)
    manifest_path = sdir / "manifest.json"
    if not manifest_path.exists():
        die(f"No staging manifest for source: {rel} (run split-source --write first)")
    manifest = load_json(manifest_path, {})
    chunks = manifest.get("chunks", [])

    if args.mark_done is not None:
        entry = next((c for c in chunks if c["index"] == args.mark_done), None)
        if entry is None:
            die(f"No chunk index {args.mark_done} in manifest")
        map_path = sdir / entry["mapFile"]
        if not map_path.exists():
            die(f"Map file missing, cannot mark done: {entry['mapFile']}")
        try:
            json.loads(read_text(map_path))
        except Exception as exc:
            die(f"Map file is not valid JSON: {entry['mapFile']} ({exc})")
        entry["mapStatus"] = "done"
        entry["mapHash"] = hash_text(read_text(sdir / entry["file"]))
        write_text(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({"marked": args.mark_done, "mapStatus": "done"}, ensure_ascii=False))
        return

    done: list[int] = []
    pending: list[int] = []
    stale: list[int] = []
    for c in chunks:
        chunk_file = sdir / c["file"]
        map_path = sdir / c["mapFile"]
        cur_hash = hash_text(read_text(chunk_file)) if chunk_file.exists() else None
        if c.get("mapStatus") == "done" and map_path.exists() and c.get("mapHash") == cur_hash:
            done.append(c["index"])
        elif c.get("mapStatus") == "done":
            stale.append(c["index"])
        else:
            pending.append(c["index"])
    print(json.dumps({
        "source": rel,
        "sourceSlug": slug,
        "stagingDir": rel_to_root(root, sdir),
        "chunkCount": len(chunks),
        "done": len(done),
        "pending": len(pending),
        "stale": len(stale),
        "pendingIndices": pending,
        "staleIndices": stale,
        "ready": len(chunks) > 0 and not pending and not stale,
    }, indent=2, ensure_ascii=False))


def slugify(value: str) -> str:
    value = value.strip().lower()
    # keep latin word chars, digits, and CJK; everything else becomes a hyphen
    value = re.sub(r"[^\w一-鿿]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:80].strip("-")


def compute_source_slug(url: str | None, raw: str | None) -> str:
    """Deterministic, stable source slug from a raw source path or URL.

    Two goals: (1) stability — re-ingesting the same source must target the same
    `wiki/sources/<slug>.md` so we never mint a second page; (2) follow the
    source's own language, not a fixed one. The raw filename usually carries the
    source language (a Chinese article saved as `…野生小虎….md` → a Chinese slug),
    so it WINS; `slugify` keeps CJK. URL is the fallback when there is no raw
    file (its host/path are ASCII, an English-ish slug). Dedup does not rely on
    the slug shape — `cmd_source_page` matches by frontmatter sources/url.
    """
    if raw and raw.strip():
        stem = Path(normalize_rel(raw)).stem
        # Strip capture noise so the fallback slug isn't a wall of text:
        # a leading `YYYY-MM-DD-` date (the RAW naming convention) and an
        # optional `NN-` sequence number some importers prepend.
        stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem)
        stem = re.sub(r"^\d+-", "", stem)
        slug = slugify(stem)
        if slug:
            return slug
    if url and url.strip():
        u = re.sub(r"^[a-z]+://", "", url.strip().lower())
        u = u.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        parts = [p for p in u.split("/") if p]
        segs = parts[1:]
        if segs:
            picked = [segs[0]]
            if len(segs) > 1 and segs[-1] != segs[0]:
                picked.append(segs[-1])
            base = "-".join(picked)
        else:
            base = parts[0].split(".")[0] if parts else "source"
        return slugify(base) or "source"
    die("source-slug requires --url or --raw")


def cmd_source_page(args: argparse.Namespace) -> None:
    """Resolve the canonical wiki source page for a raw source.

    Dedup first: scan wiki/sources/ for a page whose frontmatter already points
    at the same raw file (by basename) or the same URL, and reuse it. Only when
    none exists do we hand back a fresh deterministic slug. The model should
    write the source summary to `page`, merging if `existing` is set.
    """
    root = resolve_root(Path(args.project_root))
    raw_base = Path(normalize_rel(args.raw)).name.lower() if args.raw else None
    url_norm = args.url.strip().rstrip("/").lower() if args.url else None

    existing: str | None = None
    sdir = root / "wiki/sources"
    if sdir.exists() and (raw_base or url_norm):
        for p in sorted(sdir.glob("*.md")):
            content = read_text(p)
            mapping = parse_frontmatter_mapping(content)
            srcs = parse_inline_array(mapping.get("sources"))
            page_url = (frontmatter_value(content, "url") or "").strip().rstrip("/").lower()
            hit = (
                (raw_base and any(Path(normalize_rel(s)).name.lower() == raw_base for s in srcs))
                or (url_norm and page_url and page_url == url_norm)
            )
            if hit:
                existing = rel_to_root(root, p)
                break

    slug = compute_source_slug(args.url, args.raw)
    print(json.dumps({
        "existing": existing,
        "slug": f"sources/{slug}",
        "page": existing or f"wiki/sources/{slug}.md",
    }, indent=2, ensure_ascii=False))


def cmd_apply_blocks(args: argparse.Namespace) -> None:
    root = resolve_root(Path(args.project_root))
    text = read_text(Path(args.blocks_file)) if args.blocks_file else sys.stdin.read()
    written: list[str] = []
    warnings: list[str] = []
    for match in FILE_BLOCK_RE.finditer(text):
        rel = normalize_rel(match.group(1))
        # Guardrail: a FILE header that dropped the .md suffix would otherwise be
        # written verbatim as an extensionless junk page the app/Obsidian ignore.
        # Repair it (before the index/log/overview routing below, which keys on
        # the .md name) and surface a warning so the slip is visible, not silent.
        fixed = ensure_md_page(rel)
        if fixed != rel:
            warnings.append(f"FILE path missing .md suffix; wrote as '{fixed}' (header said '{rel}')")
            rel = fixed
        content = match.group(2).rstrip() + "\n"
        # Self-heal the common failure where the model merges the block header's
        # trailing `---` with the frontmatter's opening `---`, dropping the latter
        # and leaving a headless frontmatter (starts at `type:`, only a closing
        # `---`). The app/Obsidian can't parse that; restore the opening fence.
        if rel not in {"wiki/index.md", "wiki/log.md"}:
            content, repaired = repair_frontmatter_fence(content)
            if repaired:
                warnings.append(
                    f"Restored missing opening '---' on frontmatter for {rel} "
                    "(header '---' was merged with the frontmatter open)."
                )
            content, requoted = repair_frontmatter_quoting(content)
            if requoted:
                warnings.append(
                    f"Re-quoted a frontmatter scalar with unescaped interior "
                    f"quotes for {rel} (would have broken YAML parsing)."
                )
        if match.group(3) != "---END FILE---":
            warnings.append(
                f"Block for {rel} was not closed with ---END FILE--- "
                "(recovered at next block / EOF); check the page for stray content."
            )
        if not rel.startswith("wiki/"):
            warnings.append(f"Skipped non-wiki path: {rel}")
            continue
        if rel == "wiki/overview.md":
            warnings.append("Skipped overview block during ingest; refresh overview explicitly.")
            continue
        if rel == "wiki/index.md":
            existing = read_text(root / rel) if (root / rel).exists() else ""
            content = merge_index(existing, content)
        elif rel == "wiki/log.md" and (root / rel).exists():
            existing = read_text(root / rel).rstrip()
            content = existing + "\n\n" + content.strip() + "\n"
        target = safe_project_path(root, rel)
        if target.exists() and rel not in {"wiki/index.md", "wiki/log.md"} and not args.overwrite:
            warnings.append(f"Skipped existing content page without --overwrite: {rel}")
            continue
        if target.exists() and rel not in {"wiki/index.md", "wiki/log.md"} and args.overwrite:
            backup = agent_state_path(root, f"page-history/{rel.replace('/', '__')}-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}.md")
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, backup)
            warnings.append(f"Existing page backed up before overwrite: {rel}")
        if rel not in {"wiki/index.md", "wiki/log.md"}:
            content = normalize_related(content)
            # Catch the unfilled `tags: []` placeholder at write time, not just
            # in lint. Ingest always calls apply-blocks, whereas lint is a
            # separate pass a session can skip (and has: a batch computed
            # lint-scope, then finished without running the checks). Warn
            # rather than reject — the page body is still worth persisting.
            if missing_tags(content):
                warnings.append(
                    f"{rel} has empty tags — the template's `tags: []` placeholder was "
                    "never filled. Add real topical tags per schema.md's Tag & Domain Policy."
                )
        write_text(target, content)
        written.append(rel)
    reviews = parse_review_blocks(text, root, args.source, warnings)
    if reviews:
        add_reviews(root, reviews)
    print(json.dumps({"written": written, "reviews": len(reviews), "warnings": warnings}, indent=2, ensure_ascii=False))


def wiki_files(root: Path) -> list[Path]:
    wiki = root / "wiki"
    if not wiki.exists():
        return []
    return sorted(p for p in wiki.rglob("*.md") if p.is_file())


# ── Dedup: duplicate entity/concept detection + merge ───────────────────────
# Mirrors the llm_wiki app's src/lib/dedup.ts + dedup-runner.ts + dedup-storage.ts.
# Deterministic stages live here; the LLM detect/merge prompts live in
# references/review-research.md and are run by the agent.

DEDUP_NOTDUP_FILE = "dedup-not-duplicates.json"  # App-compatible, shared with app


def truncate(s: str, max_len: int) -> str:
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def first_body_paragraph(body: str) -> str | None:
    """First non-empty body line that isn't a heading or table row."""
    for raw in body.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("|"):
            continue
        return line
    return None


def rewrite_cross_references(content: str, redirects: dict[str, str]) -> str:
    """Rewrite [[old]]/[[folder/old|alias]] body links and `related:` entries
    that point at a merged-away slug so they target the canonical slug."""
    out = content
    for old, new in redirects.items():
        pat = re.compile(r"\[\[((?:[^\]|]*/)?)" + re.escape(old) + r"(\|[^\]]+)?\]\]")
        out = pat.sub(lambda m: f"[[{m.group(1)}{new}{m.group(2) or ''}]]", out)

    rel_vals = parse_inline_array(parse_frontmatter_mapping(out).get("related"))
    if rel_vals:
        mapped = []
        seen: set[str] = set()
        for v in rel_vals:
            slug = basename_slug(v)
            slug = redirects.get(slug, slug)
            k = slug.lower()
            if slug and k not in seen:
                seen.add(k)
                mapped.append(slug)
        if mapped != [basename_slug(v) for v in rel_vals]:
            out = set_frontmatter_array(out, "related", mapped)
    return out


def _line_refers_to_slug(line: str, slugs: set[str]) -> bool:
    for slug in slugs:
        esc = re.escape(slug)
        if re.search(r"\[\[(?:[^\]|]*/)?" + esc + r"(\|[^\]]*)?\]\]", line):
            return True
        if re.search(r"\(([^)]*/)?" + esc + r"\.md\)", line):
            return True
        if re.search(r"\b" + esc + r"\.md\b", line):
            return True
    return False


def rewrite_index_md(content: str, removed: set[str]) -> str:
    """Drop whole index.md lines that reference a merged-away slug."""
    if not removed:
        return content
    return "\n".join(
        line for line in content.split("\n") if not _line_refers_to_slug(line, removed)
    )


def _union_page_arrays(target: str, source: str) -> str:
    """Fold a source page's sources/tags/related into target's frontmatter.
    `related` is collapsed to bare basename slugs (app-resolvable)."""
    sfm = parse_frontmatter_mapping(source)
    for key in ["sources", "tags", "related"]:
        tfm = parse_frontmatter_mapping(target)
        values = [*parse_inline_array(tfm.get(key)), *parse_inline_array(sfm.get(key))]
        if key == "related":
            values = [s for v in values if (s := basename_slug(v))]
        values = list(dict.fromkeys(values))
        target = set_frontmatter_array(target, key, values)
    return target


def _canonical_group_key(slugs: list[str]) -> str:
    return ",".join(sorted(s.lower() for s in slugs))


def load_not_duplicates(root: Path) -> list[list[str]]:
    data = load_json(app_state_path(root, DEDUP_NOTDUP_FILE), [])
    if not isinstance(data, list):
        return []
    return [g for g in data if isinstance(g, list) and all(isinstance(s, str) for s in g)]


def add_not_duplicate(root: Path, slugs: list[str]) -> bool:
    slugs = [s for s in slugs if s.strip()]
    if len(slugs) < 2:
        return False
    lst = load_not_duplicates(root)
    key = _canonical_group_key(slugs)
    if any(_canonical_group_key(g) == key for g in lst):
        return False
    lst.append(sorted(slugs))
    write_text(app_state_path(root, DEDUP_NOTDUP_FILE), json.dumps(lst, indent=2, ensure_ascii=False) + "\n")
    return True


def cmd_dedup_summaries(args: argparse.Namespace) -> None:
    root = resolve_root(Path(args.project_root))
    summaries: list[dict[str, Any]] = []
    for sub in ["entities", "concepts"]:
        d = root / "wiki" / sub
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            content = read_text(p)
            fm = parse_frontmatter_mapping(content)
            if not fm:
                continue
            _, body = strip_frontmatter(content)
            desc = fm.get("description") or first_body_paragraph(body)
            summaries.append({
                "slug": p.stem,
                "path": rel_to_root(root, p),
                "type": fm.get("type") or "unknown",
                "title": fm.get("title") or p.stem,
                "description": truncate(desc, 200) if desc else None,
                "tags": parse_inline_array(fm.get("tags")),
            })
    print(json.dumps(
        {"summaries": summaries, "notDuplicates": load_not_duplicates(root)},
        indent=2, ensure_ascii=False,
    ))


def cmd_dedup_merge(args: argparse.Namespace) -> None:
    root = resolve_root(Path(args.project_root))
    canonical = args.canonical.strip()
    slugs = [s.strip() for s in args.slugs.split(",") if s.strip()]
    if len(slugs) < 2:
        die("dedup-merge requires >=2 slugs")
    if canonical not in slugs:
        die(f"--canonical {canonical} must be one of --slugs {slugs}")

    path_by_slug: dict[str, Path] = {}
    for p in wiki_files(root):
        path_by_slug.setdefault(p.stem, p)
    for s in slugs:
        if s not in path_by_slug:
            die(f"Slug not found on disk: {s}")
    group_paths = {s: path_by_slug[s] for s in slugs}
    group_path_set = set(group_paths.values())

    # Canonical content = LLM-merged body, then deterministic frontmatter union.
    merged = read_text(Path(args.body_file))
    for s in slugs:
        merged = _union_page_arrays(merged, read_text(group_paths[s]))
    merged = normalize_related(merged)
    merged = set_frontmatter_scalar(merged, "updated", today_text())

    redirects = {s: canonical for s in slugs if s != canonical}
    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    backup_dir = agent_state_path(root, f"page-history/dedup-{stamp}")

    def backup(path: Path) -> None:
        dest = backup_dir / rel_to_root(root, path).replace("/", "__")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)

    for p in group_paths.values():
        backup(p)

    rewrites: list[str] = []
    for p in wiki_files(root):
        if p in group_path_set:
            continue
        rel = rel_to_root(root, p)
        if rel == "wiki/index.md":
            continue
        content = read_text(p)
        new = rewrite_cross_references(content, redirects)
        if new != content:
            new = normalize_related(new)  # collapse any path-style related the rewrite produced
            backup(p)
            write_text(p, new)
            rewrites.append(rel)

    write_text(group_paths[canonical], merged)

    deleted: list[str] = []
    for s in slugs:
        if s == canonical:
            continue
        try:
            group_paths[s].unlink()
            deleted.append(rel_to_root(root, group_paths[s]))
        except OSError as exc:
            print(f"WARN: failed to delete {group_paths[s]}: {exc}", file=sys.stderr)

    index_path = root / "wiki/index.md"
    index_rewritten = False
    if index_path.exists():
        ic = read_text(index_path)
        new_ic = rewrite_index_md(ic, set(redirects.keys()))
        if new_ic != ic:
            backup(index_path)
            write_text(index_path, new_ic.rstrip() + "\n")
            index_rewritten = True

    print(json.dumps({
        "canonical": rel_to_root(root, group_paths[canonical]),
        "deleted": deleted,
        "rewrites": rewrites,
        "indexRewritten": index_rewritten,
        "backupDir": f".llm-wiki/agent/page-history/dedup-{stamp}",
    }, indent=2, ensure_ascii=False))


def cmd_dedup_not_duplicate(args: argparse.Namespace) -> None:
    root = resolve_root(Path(args.project_root))
    slugs = [s.strip() for s in args.slugs.split(",") if s.strip()]
    added = add_not_duplicate(root, slugs)
    print(json.dumps({"added": added, "group": sorted(s.lower() for s in slugs)}, ensure_ascii=False))


def page_id(root: Path, path: Path) -> str:
    rel = rel_to_root(root / "wiki", path)
    return rel[:-3] if rel.endswith(".md") else rel


def lint_page(root: Path, path: Path) -> str:
    return rel_to_root(root / "wiki", path)


# ── Schema versioning + upgrade ─────────────────────────────────────────────
# `init_wiki.py` copies the bundled schema template into a new wiki once and
# never touches it again (`keep_existing=True`), so every wiki's conventions are
# frozen at whatever the template said on its creation day. That froze a real
# defect into six live wikis: a Tag & Domain Policy fix shipped to the template
# and no existing wiki ever saw it, because nothing compares the two.
#
# Version marker over content hash, because a wiki's schema.md is *meant* to be
# edited — the domain table is filled in per wiki — so any hash comparison would
# read every healthy wiki as "modified" and tell us nothing.

SCHEMA_VERSION_RE = re.compile(r"<!--\s*llm-wiki-schema-version:\s*(\d+)\s*-->")
# Every class here is `[ \t]`, never `\s`: `\s` matches newlines, so a greedy
# separator class spanning `[-\s|]` happily ate the separator row *and* the
# blank `|        |        |` data row beneath it, leaving the rows group empty
# and silently relocating a carried-over table outside the markdown table.
DOMAIN_TABLE_RE = re.compile(
    r"^\|[ \t]*domain[ \t]*\|[ \t]*covers[ \t]*\|[ \t]*\n"   # header row
    r"\|[-:| \t]+\|[ \t]*\n"                                  # separator row
    r"((?:\|[^\n]*\|[ \t]*\n)*)",                             # data rows
    re.MULTILINE)


def bundled_schema_path() -> Path:
    return SKILL_DIR / "assets" / "templates" / "schema.md"


def schema_version(text: str) -> int:
    """Version marker, or 1 for a pre-versioning schema (no marker)."""
    m = SCHEMA_VERSION_RE.search(text)
    return int(m.group(1)) if m else 1


def domain_table_rows(text: str) -> str:
    """The per-wiki rows of the `| domain | covers |` table, header excluded."""
    m = DOMAIN_TABLE_RE.search(text)
    return m.group(1) if m else ""


def domain_table_is_empty(text: str) -> bool:
    rows = domain_table_rows(text)
    return not any(c.strip() for c in rows.replace("|", " ").split())


# Above this many lines of local content the bundled template doesn't have,
# `--apply` stops and asks. The domain table is the only section this command
# knows how to carry across, and that was written assuming it is the only
# per-wiki section — false for any wiki whose owner actually edited schema.md.
# One real wiki had 44 such lines: domain-usage examples naming its own values,
# tag rules citing its own pages, naming conventions with its own filenames.
# The five untouched wikis alongside it had 3–10, all superseded template
# wording. The gap between those two populations is what the number splits;
# below it is ordinary drift, above it someone wrote something.
SCHEMA_LOCAL_EDIT_LIMIT = 15


def schema_local_only_lines(current: str, bundled: str) -> list[str]:
    """Non-trivial lines in a wiki's schema.md absent from the bundled template.

    Line-level and deliberately crude: this decides whether to *ask*, not what
    to keep. Blank lines and bare markdown punctuation are ignored so that
    reformatting alone doesn't trip it.
    """
    template = {line.strip() for line in bundled.splitlines() if line.strip()}
    orphans = []
    for line in current.splitlines():
        stripped = line.strip()
        if not stripped or stripped in template:
            continue
        if set(stripped) <= set("-|`#*_ "):  # rules, fences, table separators
            continue
        orphans.append(stripped)
    return orphans


def cmd_schema_upgrade(args: argparse.Namespace) -> None:
    root = resolve_root(Path(args.project_root))
    target = root / "schema.md"
    bundled = bundled_schema_path()
    if not bundled.exists():
        print(json.dumps({"status": "error",
                          "detail": f"bundled template missing at {bundled}"},
                         indent=2, ensure_ascii=False))
        sys.exit(1)
    current = read_text(target) if target.exists() else ""
    new_text = read_text(bundled)
    have, want = schema_version(current), schema_version(new_text)

    # Never write backwards. A wiki ahead of the bundled template means either a
    # hand-extended schema or an older skill install talking to a newer wiki;
    # overwriting either silently discards rules the wiki actively relies on.
    # The backup is not a defense — "upgraded" in the report is what makes it
    # dangerous, because nobody goes looking for a backup of a success.
    if have > want:
        print(json.dumps({
            "status": "refused",
            "wiki": str(root),
            "currentVersion": have,
            "bundledVersion": want,
            "detail": (f"this wiki's schema.md is v{have}, ahead of the bundled v{want}. "
                       "Refusing to overwrite it — that would be a downgrade, not an upgrade. "
                       "Update the skill install, or reconcile the two by hand."),
        }, indent=2, ensure_ascii=False))
        sys.exit(1)

    # Carry the one section that is genuinely per-wiki across the upgrade.
    # Verify the result rather than trusting the substitution: losing a
    # hand-built domain taxonomy to a silent regex miss is the worst outcome
    # this command has, and reporting it as carried would hide the loss.
    rows = domain_table_rows(current)
    carried = False
    if rows.strip() and not domain_table_is_empty(current):
        candidate = DOMAIN_TABLE_RE.sub(
            lambda m: m.group(0)[: m.start(1) - m.start(0)] + rows, new_text, count=1)
        carried = domain_table_rows(candidate).strip() == rows.strip()
        if carried:
            new_text = candidate
        else:
            report_warn = ("domain table could NOT be carried over — the bundled template's "
                           "table did not match; your rows are preserved in the backup only")
            print(json.dumps({"status": "aborted", "wiki": str(root), "detail": report_warn,
                              "rows": rows.strip().splitlines()}, indent=2, ensure_ascii=False))
            sys.exit(1)

    diff = list(difflib.unified_diff(
        current.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile=f"schema.md (v{have}, current)", tofile=f"schema.md (v{want}, bundled)"))
    orphans = schema_local_only_lines(current, new_text)
    report: dict[str, Any] = {
        "status": "up-to-date" if have >= want and not diff else ("behind" if have < want else "drifted"),
        "wiki": str(root),
        "currentVersion": have,
        "bundledVersion": want,
        "domainTableCarried": carried,
        "diffLines": len(diff),
        "localOnlyLines": len(orphans),
    }
    if not args.apply:
        report["hint"] = ("re-run with --apply to write it (a backup is kept under "
                          ".llm-wiki/agent/page-history/)")
        if orphans:
            report["localOnlySample"] = [truncate(o, 90) for o in orphans[:8]]
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if diff and args.diff:
            sys.stdout.write("".join(diff))
        return
    if not diff:
        report["status"] = "up-to-date"
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    # Only the domain table survives an upgrade. Everything else a wiki's owner
    # wrote into schema.md is replaced, and a report saying "upgraded" is how
    # that becomes invisible.
    if len(orphans) > SCHEMA_LOCAL_EDIT_LIMIT and not args.accept_local_loss:
        report["status"] = "refused"
        report["detail"] = (
            f"schema.md has {len(orphans)} lines of content the bundled template does not, "
            f"well past the {SCHEMA_LOCAL_EDIT_LIMIT} expected from ordinary template drift. "
            "This wiki's schema looks hand-written, and only the domain table is carried "
            "across — the rest would be replaced. Review with --diff, merge the new "
            "sections by hand, or pass --accept-local-loss if the backup is enough.")
        report["localOnlySample"] = [truncate(o, 90) for o in orphans[:8]]
        print(json.dumps(report, indent=2, ensure_ascii=False))
        sys.exit(1)
    if target.exists():
        backup = agent_state_path(
            root, f"page-history/schema.md-v{have}-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}.md")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, backup)
        report["backup"] = str(backup)
    write_text(target, new_text)
    report["status"] = "upgraded"
    print(json.dumps(report, indent=2, ensure_ascii=False))


# ── Tag vocabulary (the self-growing retrieval facet) ───────────────────────
# schema.md asks that every tag be "reusable across ≥2 pages", but an ingest
# agent generating tags for a new page has no way to honor that blind: it can't
# see what the corpus already uses, so each session coins fresh words and
# near-synonyms pile up (one wiki accumulated `大模型`, `LLM` and `大语言模型`
# from three separate ingests of the same subject). The vocabulary
# has to be readable *back* into ingest, not merely written out — that feedback
# loop is what makes the facet converge instead of sprawl, and it's why the
# vocabulary is derived from the corpus on every call rather than stored in a
# file that would drift from the pages it describes.
#
# Deliberately not a controlled vocabulary: `domain` already plays that role
# (closed, ≤2 values, defined by hand in schema.md's table). Tags stay free —
# the user who says "this wiki is for 政治" genuinely does not know the tag set
# on day one, and shouldn't have to. It emerges, then gets consolidated.

TAG_ESTABLISHED_MIN = 2  # schema.md's "reusable across ≥2 pages" threshold


def _tag_near_duplicate(a: str, b: str) -> str | None:
    """Cheap, deterministic near-synonym test for two tags.

    Returns a short reason string, or None. Tuned for a mixed CJK/ASCII corpus:

    - Case-folded equality catches `YouTube` / `youtube`.
    - CJK pairs need *two* tests, because Chinese near-synonyms fail in two
      different shapes. An expansion like `大模型` vs `大语言模型` is not a
      substring relation at all — the shared characters are not contiguous — so
      character-set Jaccard catches it at 0.60, while a merely adjacent pair
      like `大模型`/`模型压缩` stays at 0.40 and is left alone. Specialization
      (`检索` vs `检索增强`) *is* nesting, but Jaccard penalizes the length gap
      down to 0.50 and would miss it, so containment covers that half. Either
      test firing is enough.
    - ASCII pairs use containment with a length floor, so `SEO` doesn't swallow
      every tag that happens to contain those letters.

    A hint for a human or LLM to adjudicate — never an automatic merge.
    """
    la, lb = a.strip().lower(), b.strip().lower()
    if not la or not lb:
        return None
    if la == lb:
        return "case-variant"
    if cjk_ratio(la) > 0.5 and cjk_ratio(lb) > 0.5:
        if min(len(la), len(lb)) >= 2 and (la in lb or lb in la):
            return "containment"
        sa, sb = set(la), set(lb)
        overlap = len(sa & sb) / len(sa | sb)
        return f"char-overlap {overlap:.2f}" if overlap >= 0.6 else None
    if min(len(la), len(lb)) >= 3 and (la in lb or lb in la):
        return "containment"
    return None


# Word-ish runs, ASCII and CJK scored separately. Splitting on whitespace alone
# was actively harmful: a query written the way the docs show it —
# `"检索增强 / 向量数据库"` — made `/` its own term, and `/` occurs in every body
# wikilink (`[[entities/…]]`), so it matched the entire corpus. A nonsense query
# containing a slash returned 40 confidently "relevant" tags. The mirror-image
# failure was CJK punctuation: `检索增强，向量数据库` has no whitespace, so the whole
# string became one term and matched nothing. Both directions are worse than a
# crude match — one fabricates relevance, the other silently reports new ground.
TAG_QUERY_TOKEN_RE = re.compile(
    r"[0-9A-Za-z][0-9A-Za-z_+#.\-]*"                       # latin/alnum runs, hyphens kept
    r"|[一-鿿぀-ヿ가-힯]+")        # CJK runs


def tag_query_terms(query: str) -> list[str]:
    """Search terms from a free-text query, punctuation and noise removed.

    Hyphens and dots stay *inside* tokens (`agent-skills`, `claude-code` and
    `v2.1` are real tags); everything else that isn't alphanumeric or CJK is a
    separator. Single characters are dropped — a lone `党` or `a` matches
    almost everything and only blurs the ranking.

    Trailing `+` and `#` are **kept**, unlike `.`/`-`/`_` which are usually
    sentence punctuation. Stripping them turned `C++` and `C#` into a bare `c`,
    which then failed the length filter, so two ordinary subjects for a
    technical wiki were rejected outright as "no usable search terms".
    """
    terms: list[str] = []
    for raw in TAG_QUERY_TOKEN_RE.findall(query or ""):
        token = raw.rstrip("-._").lower()  # regex already guarantees an alnum start
        if len(token) >= 2:
            terms.append(token)
    return list(dict.fromkeys(terms))


def _tag_query_scope(root: Path, query: str, top: int) -> set[str]:
    """Pages most relevant to a free-text query — the same bounded keyword
    scoring the local retrieval tier uses, reduced to just page paths."""
    terms = tag_query_terms(query)
    if not terms:
        return set()
    scored: list[tuple[float, str]] = []
    for path in wiki_files(root):
        if path.name in {"index.md", "log.md", "overview.md"}:
            continue
        try:
            text = read_text(path)[:6000]
        except OSError:
            continue
        title = (frontmatter_value(text, "title") or path.stem).lower()
        _, body = strip_frontmatter(text)
        body_lower = body.lower()
        score = 0.0
        for term in terms:
            if term in title:
                score += 5.0
            score += float(min(body_lower.count(term), 5))
        if score > 0:
            scored.append((score, lint_page(root, path)))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return {rel for _, rel in scored[:top]}


def tag_vocabulary(root: Path, *, query: str | None = None, scope_top: int = 12,
                   scope_paths: list[str] | None = None,
                   with_duplicates: bool = True) -> dict[str, Any]:
    """Derive the live tag vocabulary from the wiki layer.

    Returns established tags (used on ≥2 pages), singletons (used once — either
    a tag that hasn't caught on yet or a near-synonym of an established one),
    untagged pages, and consolidation hints. Index/log/overview are excluded:
    overview carries no `tags` key by contract, the other two aren't content.

    `query` scopes the result to tags appearing on the pages most relevant to
    it, which is how ingest should read this. Counts stay corpus-wide so the
    caller still sees how established each tag really is; only the *selection*
    narrows. This is also what keeps new tags reachable — see `relevant` below.

    `with_duplicates=False` skips the O(tags²) pair scan. On a 1500-tag wiki
    that scan is 1.1M comparisons and ~2s, pure waste for callers that only
    want the backbone.
    """
    counts: dict[str, int] = {}
    pages_for: dict[str, list[str]] = {}
    untagged: list[str] = []
    total = 0
    for path in wiki_files(root):
        if path.name in {"index.md", "log.md", "overview.md"}:
            continue
        total += 1
        rel = lint_page(root, path)
        tags = [t.strip() for t in extract_frontmatter_list(read_text(path), "tags") if t.strip()]
        if not tags:
            untagged.append(rel)
        for tag in dict.fromkeys(tags):  # a page counts once per tag
            counts[tag] = counts.get(tag, 0) + 1
            pages_for.setdefault(tag, []).append(rel)

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    established = [{"tag": t, "count": c} for t, c in ranked if c >= TAG_ESTABLISHED_MIN]
    singletons = [t for t, c in ranked if c < TAG_ESTABLISHED_MIN]

    # Tags on the pages nearest this source, singletons included and marked.
    # Without this the facet cannot actually grow: a tag coined on one page is
    # a singleton, the backbone view only lists established (≥2) tags, so the
    # next ingest never sees the new word, never reuses it, and it can never
    # reach 2. Every new tag would die at count 1 and the "self-growing"
    # vocabulary would only ever recycle whatever was already popular.
    relevant: list[dict[str, Any]] = []
    if scope_paths is not None or query:
        if scope_paths is not None:
            # Exact pages from the caller — ingest already picked these in step
            # 5, so reusing them beats re-deriving relevance from a text blob.
            scope = {normalize_rel(p).removeprefix("wiki/") for p in scope_paths}
        else:
            scope = _tag_query_scope(root, query or "", scope_top)
        in_scope = {t for t, pages in pages_for.items() if scope.intersection(pages)}
        relevant = [{"tag": t, "count": counts[t], "new": counts[t] < TAG_ESTABLISHED_MIN}
                    for t, _ in ranked if t in in_scope]

    # Rank by how much merging the pair would actually buy, because on a large
    # wiki this list runs to hundreds of entries and an unordered dump is one
    # nobody reads. Case-variants lead: they need no judgement at all (`AI`/`ai`
    # split 58 pages in one real wiki), so they are pure wins. Everything else
    # follows by total pages affected.
    hints: list[dict[str, Any]] = []
    if with_duplicates:
        all_tags = [t for t, _ in ranked]
        for i, a in enumerate(all_tags):
            for b in all_tags[i + 1:]:
                reason = _tag_near_duplicate(a, b)
                if reason:
                    hints.append({"a": a, "b": b, "reason": reason,
                                  "counts": f"{counts[a]}/{counts[b]}",
                                  "pagesAffected": counts[a] + counts[b]})
        hints.sort(key=lambda h: (h["reason"] != "case-variant", -h["pagesAffected"],
                                  h["a"], h["b"]))
    return {
        "contentPages": total,
        "taggedPages": total - len(untagged),
        "distinctTags": len(counts),
        "established": established,
        "singletons": singletons,
        "relevant": relevant,
        "untaggedPages": untagged,
        "nearDuplicates": hints,
        "duplicatesComputed": with_duplicates,
        "pagesFor": pages_for,
    }


TAG_SINGLETON_TAIL = 25  # unscoped promotion window — see cmd_tags


def cmd_tags(args: argparse.Namespace) -> None:
    root = resolve_root(Path(args.project_root))
    # `or None` here silently turned an explicitly empty --paths-file into an
    # unscoped call, which then printed the global singleton tail — an agent
    # whose retrieval found nothing would read a list of unrelated tags as "the
    # tags relevant to this source". Absent scope and empty scope are different
    # questions and must not collapse into the same answer. Same reason missing
    # pages are an error rather than "0 tags on 1 given pages": that phrasing
    # reads as "new ground for the wiki", which is a claim about the corpus,
    # not about a typo'd path.
    scope_paths: list[str] | None = None
    if args.paths or args.paths_file:
        scope_paths = _read_pages_paths(args)
        if not scope_paths:
            die("--paths/--paths-file was given but resolved to zero paths. If the "
                "retrieval working set is genuinely empty, omit the flag (or use --q) "
                "rather than passing an empty scope.")
        missing = [p for p in scope_paths
                   if not (root / "wiki" / normalize_rel(p).removeprefix("wiki/")).is_file()]
        if len(missing) == len(scope_paths):
            die("none of the --paths/--paths-file entries exist under wiki/: "
                + ", ".join(missing[:5]) + ("…" if len(missing) > 5 else ""))
        if missing:
            print(f"# warning: {len(missing)} of {len(scope_paths)} scope paths do not exist "
                  f"under wiki/ and were ignored: " + ", ".join(missing[:5])
                  + ("…" if len(missing) > 5 else ""), file=sys.stderr)
            # Drop them, or the header would claim more pages than it read.
            scope_paths = [p for p in scope_paths if p not in set(missing)]
    elif args.q and not tag_query_terms(args.q):
        die("--q has no usable search terms (only punctuation / single characters). "
            "Pass topic words or entity names, e.g. --q \"检索增强 向量数据库 嵌入\".")
    vocab = tag_vocabulary(root, query=args.q, scope_top=args.scope_top,
                           scope_paths=scope_paths,
                           with_duplicates=args.audit or bool(args.json))
    scoped = scope_paths is not None or bool(args.q)
    if args.json:
        if not args.verbose:
            vocab.pop("pagesFor", None)
        print(json.dumps(vocab, indent=2, ensure_ascii=False))
        return
    # Two audiences, and conflating them was a real cost: ingest reads this
    # before tagging *every* page, while singletons / duplicate pairs /
    # untagged pages are cleanup material nobody needs mid-ingest. Emitting all
    # of it by default put 16KB (~6.4k tokens) of governance data into the
    # ingest path of a 911-page wiki — an O(wiki) load in a SOP whose first
    # rule is "retrieval is O(top-k), never O(wiki)". Default is now the
    # backbone only: bounded by --limit, so the cost is flat no matter how
    # large the corpus grows. `--audit` (and `health`) serve the other reader.
    est = vocab["established"]
    shown = est if args.limit <= 0 else est[: args.limit]
    print(f"# tag vocabulary — {vocab['taggedPages']}/{vocab['contentPages']} pages tagged, "
          f"{vocab['distinctTags']} distinct")
    if scoped:
        rel = vocab["relevant"]
        rel_shown = rel if args.limit <= 0 else rel[: args.limit]
        where = (f"{len(scope_paths)} given pages" if scope_paths is not None
                 else f"the {args.scope_top} nearest pages")
        print(f"\n## relevant to this source ({len(rel)} tags on {where})")
        print(", ".join(f"{r['tag']}({r['count']}){'*' if r['new'] else ''}" for r in rel_shown)
              if rel_shown else "(no related pages yet — this is new ground for the wiki)")
        if args.limit > 0 and len(rel) > args.limit:
            print(f"… +{len(rel) - args.limit} more (--limit 0 for all)")
        print("* = used on only one page so far; reusing it here is what promotes it")
    print("\n## established (reuse these when they fit)")
    print(", ".join(f"{e['tag']}({e['count']})" for e in shown) if shown
          else "(none yet — this wiki is still building its vocabulary)")
    if args.limit > 0 and len(est) > args.limit:
        print(f"… +{len(est) - args.limit} more established tags (--limit 0 for all)")
    # Unscoped callers still need a path by which a brand-new tag can be seen
    # and reused, or nothing ever climbs from 1 to 2. Show the tail bounded —
    # the whole list is 899 entries on a large wiki, which is what made the
    # first version of this command expensive.
    if not scoped and not args.audit and vocab["singletons"]:
        tail = vocab["singletons"][:TAG_SINGLETON_TAIL]
        print(f"\n## used once so far ({len(vocab['singletons'])} total, showing {len(tail)} — "
              "reuse one instead of coining a near-synonym)")
        print(", ".join(tail))
        print("(`--q \"<source topic>\"` for the ones actually related to what you're tagging)")
    if not args.audit:
        print(f"\n({len(vocab['singletons'])} tags used once, {len(vocab['untaggedPages'])} untagged "
              "pages — `--audit` or `wiki_ops.py health` for the full cleanup view)")
        return
    if vocab["singletons"]:
        print("\n## singletons (used once — prefer reusing one over coining a near-synonym)")
        print(", ".join(vocab["singletons"]))
    dups = vocab["nearDuplicates"]
    if dups:
        shown_d = dups if args.limit <= 0 else dups[: args.limit]
        print(f"\n## possible duplicates ({len(dups)}, highest-impact first — judge, don't auto-merge)")
        for h in shown_d:
            print(f"  {h['a']} ↔ {h['b']}  [{h['counts']}] {h['reason']}")
        if args.limit > 0 and len(dups) > args.limit:
            print(f"  … +{len(dups) - args.limit} more (--limit 0 for all)")
    if vocab["untaggedPages"]:
        print(f"\n## untagged pages ({len(vocab['untaggedPages'])})")
        for p in vocab["untaggedPages"]:
            print(f"  {p}")


# ── Wiki health (project-level, where lint is page-level) ───────────────────
# lint answers "is this page well-formed". Nothing answered "is this *wiki*
# still configured for the corpus it has grown into" — so the setup work a wiki
# needs once it has real content (fill the domain table, replace the purpose.md
# stubs, refresh the placeholder overview, consolidate a sprawling tag facet)
# had no trigger at all and simply never happened. One wiki ran a month and 22
# pages with every init placeholder still in place.
#
# Everything here is a nudge with a threshold, not an error: a three-page wiki
# genuinely should not have a domain taxonomy yet.

HEALTH_SOURCE_THRESHOLD = 12  # schema.md's own "often after a dozen-plus sources"
# Two init paths write two different placeholder wordings — `my-llm-wiki`'s
# `init_wiki.py` inline templates and this skill's `assets/templates/`. Matching
# only the first meant a wiki scaffolded by the maintainer's own Initialize flow
# reported a clean bill of health with every placeholder still in place. Any new
# init template has to add its markers here too.
PURPOSE_STUB_MARKERS = (
    "<!-- List the primary questions", "<!-- What is in scope?",
    "> TBD", "<!-- Your current working hypothesis",          # init_wiki.py
    "Describe what this wiki is trying to understand",        # assets/templates
    "## Working Thesis\n\nTBD.",
)
OVERVIEW_STUB_MARKERS = (
    "<!-- Provide a high-level summary",                      # init_wiki.py
    "<!-- `refresh overview` regenerates this",               # assets/templates
)


def cmd_health(args: argparse.Namespace) -> None:
    root = resolve_root(Path(args.project_root))
    findings: list[dict[str, str]] = []

    def note(area: str, severity: str, detail: str, fix: str) -> None:
        findings.append({"area": area, "severity": severity, "detail": detail, "fix": fix})

    raw_dir = root / "raw" / "sources"
    source_count = len(list(raw_dir.rglob("*.md"))) if raw_dir.exists() else 0
    mature = source_count >= HEALTH_SOURCE_THRESHOLD

    schema_text = read_text(root / "schema.md") if (root / "schema.md").exists() else ""
    bundled = bundled_schema_path()
    if schema_text and bundled.exists():
        have, want = schema_version(schema_text), schema_version(read_text(bundled))
        if have < want:
            note("schema", "warning",
                 f"schema.md is v{have}; the bundled template is v{want}. This wiki never "
                 "received template fixes made after it was created.",
                 "wiki_ops.py schema-upgrade <root> --diff, then --apply")
    if schema_text and domain_table_is_empty(schema_text) and mature:
        note("domain", "info",
             f"the domain table in schema.md is still empty at {source_count} sources; "
             "schema.md says to fill it once a few natural areas are clear.",
             "edit the `| domain | covers |` table in schema.md, then apply going forward")

    purpose = read_text(root / "purpose.md") if (root / "purpose.md").exists() else ""
    stubs = [m for m in PURPOSE_STUB_MARKERS if m in purpose]
    if purpose and stubs and mature:
        note("purpose", "info",
             f"purpose.md still has {len(stubs)} init placeholder(s) at {source_count} sources "
             "(Key Questions / Scope / Thesis). Ingest reads this file on every run, so blank "
             "sections cost relevance judgement on every page it writes.",
             "fill in the sections — this is one of only two per-wiki files ingest always reads")

    overview = root / "wiki" / "overview.md"
    if (overview.exists() and source_count > 0
            and any(m in read_text(overview) for m in OVERVIEW_STUB_MARKERS)):
        note("overview", "info",
             "wiki/overview.md is still the init placeholder.",
             "refresh it per the Refresh Overview flow (manual, never during ingest)")

    vocab = tag_vocabulary(root)
    if vocab["untaggedPages"]:
        note("tags", "warning",
             f"{len(vocab['untaggedPages'])} content page(s) have no tags at all.",
             "wiki_ops.py lint <root> lists them as missing-tags")
    singles, distinct = len(vocab["singletons"]), vocab["distinctTags"]
    if distinct >= 10 and singles > distinct / 2:
        note("tags", "info",
             f"{singles} of {distinct} tags are used on exactly one page — the facet is "
             "sprawling rather than converging.",
             "wiki_ops.py tags <root> and fold singletons into established tags")
    if vocab["nearDuplicates"]:
        pairs = ", ".join(f"{h['a']}↔{h['b']}" for h in vocab["nearDuplicates"][:5])
        note("tags", "info",
             f"{len(vocab['nearDuplicates'])} possible duplicate tag pair(s): {pairs}"
             + ("…" if len(vocab["nearDuplicates"]) > 5 else ""),
             "wiki_ops.py tags <root> shows all pairs; merge by editing the pages")

    result = {
        "wiki": str(root),
        "sources": source_count,
        "contentPages": vocab["contentPages"],
        "mature": mature,
        "findings": findings,
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"# wiki health — {root}")
    print(f"{source_count} raw sources, {vocab['contentPages']} content pages"
          + ("" if mature else f" (below the {HEALTH_SOURCE_THRESHOLD}-source maturity "
                               "threshold — setup nudges stay quiet)"))
    if not findings:
        print("\nno findings.")
        return
    for f in findings:
        print(f"\n[{f['severity']}] {f['area']}: {f['detail']}")
        print(f"  → {f['fix']}")


REVIEW_STARVATION_THRESHOLD = 5


def make_lint_item(seq: int, item_type: str, severity: str, page: str, detail: str, affected_pages: list[str] | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": f"lint-{seq}",
        "type": item_type,
        "severity": severity,
        "page": page,
        "detail": detail,
        "createdAt": epoch_ms(),
    }
    if affected_pages:
        item["affectedPages"] = affected_pages
    return item


def cmd_lint(args: argparse.Namespace) -> None:
    root = resolve_root(Path(args.project_root))
    files = wiki_files(root)
    id_to_path: dict[str, Path] = {}
    for path in files:
        pid = page_id(root, path)
        id_to_path[slug_key(pid)] = path
        id_to_path[slug_key(path.stem)] = path

    inbound: dict[str, int] = {slug_key(page_id(root, p)): 0 for p in files}
    issues: list[dict[str, Any]] = []
    outlinks: dict[Path, list[str]] = {}
    seq = 0
    raw_dir = root / "raw"
    raw_basenames = {p.name for p in raw_dir.rglob("*.md")} if raw_dir.exists() else set()

    def issue(item_type: str, severity: str, page: str, detail: str, affected_pages: list[str] | None = None) -> None:
        nonlocal seq
        seq += 1
        issues.append(make_lint_item(seq, item_type, severity, page, detail, affected_pages))

    for path in files:
        rel = lint_page(root, path)
        content = read_text(path)
        if path.name not in {"index.md", "log.md"}:
            defect = frontmatter_defect(content)
            if defect:
                issue("semantic", "warning", rel, defect)
        if path.name not in {"index.md", "log.md", "overview.md"}:
            if missing_tags(content):
                issue("missing-tags", "warning", rel,
                      "tags is empty — this page never got topical tags at all. The "
                      "FILE-block template ships `tags: []` as a placeholder to fill, "
                      "not a value to keep; an empty set is the same defect as tagging "
                      "a page 'video' and nothing else. Add real subject words per "
                      "schema.md's Tag & Domain Policy.")
            inbox_leak, bare_source_type_leak = raw_tag_leaks(content)
            if inbox_leak:
                issue("raw-tag-leak", "warning", rel,
                      f"tags contain '{inbox_leak[0]}' — that means \"not yet processed\" "
                      "and belongs only to RAW capture frontmatter (schema.md), never a "
                      "compiled wiki page. Replace with real topical tags.")
            if bare_source_type_leak:
                issue("raw-tag-leak", "warning", rel,
                      f"tags are ONLY RAW source_type word(s) {bare_source_type_leak} — no "
                      "real topical tag at all. A format tag like 'video'/'bilibili' is fine "
                      "alongside genuine tags, but as the entire tag set it means this page "
                      "never got real tags generated. Add topical tags per the Tag & Domain "
                      "Policy.")
        links = extract_wikilinks(content)
        outlinks[path] = links
        for link in links:
            key = slug_key(link)
            if key in id_to_path:
                inbound[slug_key(page_id(root, id_to_path[key]))] = inbound.get(slug_key(page_id(root, id_to_path[key])), 0) + 1
            else:
                issue("broken-link", "warning", rel, f"Broken link: [[{link}]]")
        # Traceability: a sources[] entry naming a raw .md that doesn't exist means
        # the page's claims can never be re-checked against the original (seen with
        # research pages whose evidence bundle was never persisted, and after raw
        # renames). Compare by basename — entries may be full paths or bare names.
        if path.name not in {"index.md", "log.md", "overview.md"}:
            for src_entry in extract_frontmatter_list(content, "sources"):
                if not src_entry.endswith(".md") or "://" in src_entry:
                    continue
                if Path(src_entry).name not in raw_basenames:
                    issue("missing-raw", "info", rel,
                          f"sources entry has no matching raw file: {src_entry}")

    for path in files:
        if path.name in {"index.md", "log.md", "overview.md"}:
            continue
        rel = lint_page(root, path)
        pid = slug_key(page_id(root, path))
        if inbound.get(pid, 0) == 0:
            issue("orphan", "info", rel, "No inbound wikilinks.")
        if not outlinks.get(path):
            issue("no-outlinks", "info", rel, "No outbound wikilinks.")

    # Review starvation (advisory). N+ ingests with zero review output means the
    # deep-research loop is silently starving — the failure mode where a condensed
    # workflow doc or a weaker model drops REVIEW generation and nobody notices
    # until the queue has been flat for weeks. Ingest times come from the cache
    # ledger; "since the last review item" (or ever, if there is none) is the window.
    cache = load_json(agent_state_path(root, "ingest-cache.json"), {})
    entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
    ingest_times = []
    for entry in entries.values():
        ts = entry.get("timestamp") if isinstance(entry, dict) else None
        if isinstance(ts, str):
            try:
                ingest_times.append(dt.datetime.fromisoformat(ts))
            except ValueError:
                pass
    reviews = load_json(app_state_path(root, "review.json"), [])
    latest_review_ms = max(
        (item.get("createdAt", 0) for item in reviews if isinstance(item, dict)), default=0
    )
    latest_review = dt.datetime.fromtimestamp(latest_review_ms / 1000, dt.timezone.utc)
    starved = sum(1 for ts in ingest_times if ts > latest_review)
    if starved >= REVIEW_STARVATION_THRESHOLD:
        since = latest_review.date().isoformat() if latest_review_ms else "ever"
        issue(
            "review-starvation", "info", ".llm-wiki/review.json",
            f"{starved} ingests since the last review item ({since}) produced zero review "
            "output — the deep-research queue is starving. REVIEW blocks are part of the "
            "ingest deliverable (0–3 suggestions per source, decided not skipped); see "
            "references/ingest-update.md step 7.",
        )

    write_text(app_state_path(root, "lint.json"), json.dumps(issues, indent=2, ensure_ascii=False) + "\n")

    # Severity ranking so a CI gate can fail on "warning and above" while still
    # tolerating "info" (orphans / no-outlinks are advisory, not breakage).
    rank = {"info": 0, "warning": 1, "error": 2}
    threshold = rank.get(getattr(args, "fail_on", "warning"), 1)
    failing = sum(1 for it in issues if rank.get(it.get("severity"), 1) >= threshold)
    print(json.dumps(
        {"count": len(issues), "failing": failing, "fail_on": getattr(args, "fail_on", "warning"), "issues": issues},
        indent=2, ensure_ascii=False,
    ))
    # Default behavior unchanged (exit 0). Only --exit-code turns lint into a
    # cron/CI gate: non-zero when any issue at/above the threshold exists.
    if getattr(args, "exit_code", False) and failing:
        return 1
    return 0


GRAPH_SKIP_NAMES = {"index.md", "log.md", "overview.md"}


def _scan_page_links(path: Path) -> dict[str, Any]:
    """Link surface of one page: wikilink targets, related: slugs, sources[]."""
    content = read_text(path)
    return {
        "links": {slug_key(t) for t in extract_wikilinks(content)},
        "related": {slug_key(t) for t in extract_frontmatter_list(content, "related")},
        # Compare sources by basename: entries may be full raw paths or bare names.
        "sources": {Path(s).name for s in extract_frontmatter_list(content, "sources") if s},
    }


def _wiki_graph(root: Path, files: list[Path]) -> tuple[dict[str, Path], dict[Path, dict[str, Any]]]:
    """slug→path resolution map + per-page link scan, content pages only."""
    id_to_path: dict[str, Path] = {}
    info: dict[Path, dict[str, Any]] = {}
    for path in files:
        if path.name in GRAPH_SKIP_NAMES:
            continue
        id_to_path[slug_key(page_id(root, path))] = path
        id_to_path[slug_key(path.stem)] = path
        info[path] = _scan_page_links(path)
    return id_to_path, info


def _one_hop_neighbors(
    root: Path,
    target: Path,
    id_to_path: dict[str, Path],
    info: dict[Path, dict[str, Any]],
) -> dict[str, set[str]]:
    """rel-path → relation kinds for pages one hop from an existing target page."""
    neighbors: dict[str, set[str]] = {}

    def add(path: Path, via: str) -> None:
        neighbors.setdefault(rel_to_root(root, path), set()).add(via)

    t = info.get(target) or _scan_page_links(target)
    target_keys = {slug_key(page_id(root, target)), slug_key(target.stem)}
    for key in t["links"]:
        hit = id_to_path.get(key)
        if hit and hit != target:
            add(hit, "outbound")
    for key in t["related"]:
        hit = id_to_path.get(key)
        if hit and hit != target:
            add(hit, "related")
    for path, data in info.items():
        if path == target:
            continue
        if data["links"] & target_keys or data["related"] & target_keys:
            add(path, "inbound")
        if t["sources"] and data["sources"] & t["sources"]:
            add(path, "shared-source")
    return neighbors


def cmd_neighbors(args: argparse.Namespace) -> None:
    """One-hop neighborhood of a wiki page — the conflict sentinel's input set.

    Deterministic graph walk: given an existing page (--page) and/or the link
    targets of a not-yet-written page (--slugs), return the pages one hop away
    (outbound wikilinks, inbound wikilinks, related:, shared sources[]) so the
    ingest flow can check new content against them for direct contradictions
    BEFORE apply-blocks. See references/ingest-update.md → Conflict Sentinel.
    """
    if not args.page and not args.slugs:
        die("neighbors: provide --page and/or --slugs")
    root = resolve_root(Path(args.project_root))
    files = wiki_files(root)
    id_to_path, info = _wiki_graph(root, files)

    neighbors: dict[str, set[str]] = {}
    target: Path | None = None
    target_rel: str | None = None
    if args.page:
        page = ensure_md_page(normalize_rel(args.page))
        target = safe_project_path(root, page)
        if not target.exists():
            die(f"neighbors: page not found: {page}")
        target_rel = rel_to_root(root, target)
        neighbors = _one_hop_neighbors(root, target, id_to_path, info)
    if args.slugs:

        def add(path: Path, via: str) -> None:
            neighbors.setdefault(rel_to_root(root, path), set()).add(via)

        for raw_slug in args.slugs.split(","):
            key = slug_key(raw_slug.strip())
            hit = id_to_path.get(key) if key else None
            if hit and hit != target:
                add(hit, "outbound")

    # Closest first: more distinct relations = tighter neighbor. Cap the set so
    # the sentinel prompt stays small (see the token budget in the plan doc).
    ranked = sorted(neighbors.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    capped = ranked[: max(1, args.max_neighbors)]
    print(json.dumps(
        {
            "page": target_rel,
            "count": len(neighbors),
            "returned": len(capped),
            "neighbors": [{"file": rel, "via": sorted(via)} for rel, via in capped],
        },
        indent=2, ensure_ascii=False,
    ))


LINT_STATE_FILE = "lint-state.json"  # Skill-only (.llm-wiki/agent/): lint.json is an
# App-compatible ARRAY of issues, so the incremental bookkeeping cannot live there.


def cmd_lint_scope(args: argparse.Namespace) -> None:
    """Input set for an incremental SEMANTIC lint pass.

    Full-wiki semantic lint (contradictions/stale claims/duplicates) costs LLM
    tokens proportional to wiki size, so it silently stops being run. This
    command scopes the pass to what actually moved: pages changed since the
    last `--mark` plus their one-hop neighbors (where a contradiction with the
    changed content can live). Structural lint stays full-repo — it's script-cheap.

    Flow: `lint-scope <root>` → run semantic checks on `scope[]` only →
    `lint-scope <root> --mark` once the pass completed.
    """
    root = resolve_root(Path(args.project_root))
    state_file = agent_state_path(root, LINT_STATE_FILE)
    files = wiki_files(root)
    current: dict[str, str] = {}
    paths_by_rel: dict[str, Path] = {}
    for path in files:
        if path.name in GRAPH_SKIP_NAMES:
            continue
        rel = rel_to_root(root, path)
        current[rel] = hash_text(read_text(path))
        paths_by_rel[rel] = path

    if args.mark:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_text(
            state_file,
            json.dumps({"lastLintAt": stamp, "pages": current}, indent=2, ensure_ascii=False) + "\n",
        )
        print(json.dumps({"marked": True, "lastLintAt": stamp, "pages": len(current)}, ensure_ascii=False))
        return

    state = load_json(state_file, {})
    prev = state.get("pages", {}) if isinstance(state, dict) else {}
    first_run = not prev
    changed = sorted(rel for rel, digest in current.items() if prev.get(rel) != digest)
    deleted = sorted(rel for rel in prev if rel not in current)

    neighbor_rels: set[str] = set()
    changed_set = set(changed)
    # First run: everything is "changed", neighbors add nothing — skip the graph walk.
    if changed and not first_run:
        id_to_path, info = _wiki_graph(root, files)
        for rel in changed:
            neighbor_rels.update(_one_hop_neighbors(root, paths_by_rel[rel], id_to_path, info))
        neighbor_rels -= changed_set
    print(json.dumps(
        {
            "firstRun": first_run,
            "lastLintAt": state.get("lastLintAt") if isinstance(state, dict) else None,
            "changed": changed,
            "deleted": deleted,
            "neighbors": sorted(neighbor_rels),
            "scope": sorted(changed_set | neighbor_rels),
        },
        indent=2, ensure_ascii=False,
    ))


def cmd_merge_page(args: argparse.Namespace) -> None:
    root = resolve_root(Path(args.project_root))
    # Same guardrail as apply-blocks: a `--page` missing its .md suffix would both
    # miss the existing page (real one ends in .md) and write an extensionless
    # junk file. Repair once and use the fixed path for target + backup alike.
    page = ensure_md_page(normalize_rel(args.page))
    if page != normalize_rel(args.page):
        sys.stderr.write(f"warning: --page missing .md suffix; using '{page}'\n")
    target = safe_project_path(root, page)
    incoming = read_text(Path(args.incoming_file)) if args.incoming_file else sys.stdin.read()
    existing = read_text(target) if target.exists() else ""
    merged = deterministic_page_merge(existing, incoming) if existing else incoming
    merged = normalize_related(merged)
    if args.write:
        if target.exists():
            backup = agent_state_path(root, f"page-history/{page.replace('/', '__')}-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}.md")
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, backup)
        write_text(target, merged)
    else:
        sys.stdout.write(merged)


def build_page_index(root: Path) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    titles: set[str] = set()
    for path in wiki_files(root):
        ids.add(slug_key(path.stem))
        content = read_text(path)
        title = frontmatter_value(content, "title")
        if title:
            titles.add(title.strip().lower())
    return ids, titles


def cmd_sweep_reviews(args: argparse.Namespace) -> None:
    root = resolve_root(Path(args.project_root))
    path = app_state_path(root, "review.json")
    items = load_json(path, [])
    ids, titles = build_page_index(root)
    resolved = 0
    for item in items:
        if is_review_resolved(item):
            continue
        kind = item.get("type")
        if kind == "missing-page":
            candidates = [str(item.get("title", ""))]
            candidates.extend(str(p).split("/")[-1].replace(".md", "") for p in item.get("affectedPages", []) or [])
            if any(slug_key(c) in ids or str(c).strip().lower() in titles for c in candidates if c):
                item["resolved"] = True
                item.pop("status", None)
                item["resolvedAction"] = "auto-resolved"
                resolved += 1
        elif kind == "duplicate":
            pages = item.get("affectedPages", []) or []
            if pages and any(not safe_project_path(root, normalize_rel(p)).exists() for p in pages):
                item["resolved"] = True
                item.pop("status", None)
                item["resolvedAction"] = "auto-resolved"
                resolved += 1
    write_text(path, json.dumps(items, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"resolvedByRules": resolved}, indent=2, ensure_ascii=False))


def cmd_trace(args: argparse.Namespace) -> None:
    root = resolve_root(Path(args.project_root))
    record = {
        "scope": args.scope,
        "projectRoot": str(root),
        "source": args.source,
        "inputChars": args.input_chars,
        "outputChars": args.output_chars,
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    # Retrieval/token metrology — the runtime-agnostic counters that make
    # cross-host before/after comparison possible (host-side token accounting
    # like Hermes' state.db only measures that one host).
    for key, value in (
        ("backend", args.backend),
        ("candidates", args.candidates),
        ("pagesRead", args.pages_read),
        ("contextChars", args.context_chars),
        ("promptTokens", args.prompt_tokens),
        ("cacheReadTokens", args.cache_read_tokens),
    ):
        if value is not None:
            record[key] = value
    extra = json.loads(args.extra) if args.extra else {}
    record.update(extra)
    path = agent_state_path(root, "token-trace.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, indent=2, ensure_ascii=False))


def cmd_resolve_root(args: argparse.Namespace) -> None:
    print(resolve_root(Path(args.path)))


def _read_state_file(name: str) -> str | None:
    path = Path.home() / ".my-llm-wiki" / "connector" / name
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _valid_port(value: str | None) -> int | None:
    if not value:
        return None
    try:
        port = int(value.strip())
    except ValueError:
        return None
    return port if 1024 <= port <= 65535 else None


def _browser_base_url(args: argparse.Namespace) -> str:
    explicit = (
        args.base_url
        or os.environ.get("LLM_WIKI_BROWSER_URL")
        or os.environ.get("LLM_WIKI_WEB_URL")
    )
    if explicit:
        return explicit.rstrip("/")
    port = (
        _valid_port(_read_state_file("server-port"))
        or _valid_port(os.environ.get("PORT"))
        or 8800
    )
    return f"http://127.0.0.1:{port}"


def _browser_token(args: argparse.Namespace) -> str | None:
    if args.token is not None:
        return args.token.strip() or None
    env = os.environ.get("LLM_WIKI_WEB_TOKEN")
    if env and env.strip():
        return env.strip()
    return _read_state_file("token")


def _browser_share_result(**kwargs: Any) -> dict[str, Any]:
    result = {
        "available": False,
        "relayConnected": False,
        "onlineUrl": None,
        "pageUrl": None,
        "linkUrl": None,
        "markdownLink": None,
        "reason": None,
        "endpoint": None,
    }
    result.update(kwargs)
    return result


def _fetch_browser_json(base_url: str, path: str, headers: dict[str, str], timeout: float) -> Any:
    request = urllib.request.Request(f"{base_url}{path}", headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _optional_root(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    try:
        return resolve_root(path)
    except SystemExit:
        return None


def _normalize_browser_page_path(page: str, root: Path | None = None) -> str | None:
    value = page.strip()
    if not value:
        return None
    value = value.replace("\\", "/")
    path = Path(value).expanduser()
    if path.is_absolute():
        if root is None:
            return None
        try:
            value = normalize_rel(str(path.resolve().relative_to(root.resolve())))
        except ValueError:
            return None
    else:
        parts = [part for part in value.split("/") if part and part != "."]
        if any(part == ".." for part in parts):
            return None
        value = "/".join(parts)
    if value.startswith("wiki/"):
        value = value[len("wiki/"):]
    if value.startswith("page/"):
        value = value[len("page/"):]
    if value.endswith(".md"):
        value = value[:-3]
    return value or None


def _online_page_url(online_url: str, wiki_key: str, page_path: str) -> str:
    parts = urllib.parse.urlsplit(online_url)
    base_path = parts.path.rstrip("/")
    route = (
        f"/w/{urllib.parse.quote(wiki_key, safe='')}"
        f"/page/{urllib.parse.quote(page_path, safe='/')}"
    )
    path = f"{base_path}{route}" if base_path else route
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _markdown_link(url: str | None, label: str) -> str | None:
    return f"[{label}]({url})" if url else None


def _resolve_browser_wiki_key(
    base_url: str,
    headers: dict[str, str],
    timeout: float,
    root: Path | None,
    explicit_wiki: str | None,
) -> str | None:
    if explicit_wiki:
        return explicit_wiki.strip() or None
    if root is None:
        return None
    payload = _fetch_browser_json(base_url, "/api/v1/config/wikis", headers, timeout)
    if not isinstance(payload, list):
        return None
    resolved_root = root.resolve()
    for item in payload:
        if not isinstance(item, dict):
            continue
        root_dir = item.get("root_dir")
        key = item.get("key")
        if not root_dir or not key:
            continue
        try:
            candidate = Path(str(root_dir)).expanduser().resolve()
        except OSError:
            continue
        if candidate == resolved_root:
            return str(key)
    return None


def cmd_browser_share(args: argparse.Namespace) -> None:
    """Probe the optional desktop browser for a tokenized online wiki URL.

    The browser is optional for the maintainer workflow. This command therefore
    reports unavailable states as JSON and exits successfully instead of failing
    the ingest/update/save operation that just completed.
    """
    base_url = _browser_base_url(args)
    endpoint = f"{base_url}/api/v1/config/share"
    headers = {"Accept": "application/json"}
    token = _browser_token(args)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        payload = _fetch_browser_json(base_url, "/api/v1/config/share", headers, args.timeout)
    except urllib.error.HTTPError as err:
        reason = "unauthorized" if err.code == 401 else f"http-{err.code}"
        result = _browser_share_result(reason=reason, endpoint=endpoint)
        print(json.dumps(result, ensure_ascii=False))
        return
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        result = _browser_share_result(
            reason="browser-unavailable",
            endpoint=endpoint,
            error=str(err),
        )
        print(json.dumps(result, ensure_ascii=False))
        return
    except json.JSONDecodeError as err:
        result = _browser_share_result(
            reason="invalid-response",
            endpoint=endpoint,
            error=str(err),
        )
        print(json.dumps(result, ensure_ascii=False))
        return

    if not isinstance(payload, dict):
        result = _browser_share_result(reason="invalid-response", endpoint=endpoint)
        print(json.dumps(result, ensure_ascii=False))
        return

    online_url = payload.get("online_url")
    relay_connected = bool(payload.get("relay_connected") or online_url)
    root = _optional_root(args.project_root)
    page_path = _normalize_browser_page_path(args.page, root) if args.page else None
    page_url = None
    page_reason = None
    if online_url and page_path:
        try:
            wiki_key = _resolve_browser_wiki_key(base_url, headers, args.timeout, root, args.wiki)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            wiki_key = None
        if wiki_key:
            page_url = _online_page_url(online_url, wiki_key, page_path)
        else:
            page_reason = "wiki-key-unresolved"
    link_url = page_url or online_url
    result = _browser_share_result(
        available=bool(online_url),
        relayConnected=relay_connected,
        onlineUrl=online_url,
        pageUrl=page_url,
        linkUrl=link_url,
        markdownLink=_markdown_link(link_url, args.label),
        pagePath=page_path,
        reason=None if online_url else "relay-not-connected",
        pageReason=page_reason,
        endpoint=endpoint,
    )
    print(json.dumps(result, ensure_ascii=False))


def _browser_search_result(args: argparse.Namespace) -> dict[str, Any]:
    """Full-text search through the optional desktop browser's index.

    First-tier retrieval for the Query SOP: ask the browser's search index for
    candidate pages instead of hand-scanning wiki/ (which costs tokens
    proportional to wiki size). Like browser-share, an absent or unreachable
    browser is a normal state — report it as JSON and exit 0 so the caller
    falls back to the keyword scan.
    """
    base_url = _browser_base_url(args)
    headers = {"Accept": "application/json"}
    token = _browser_token(args)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    root = _optional_root(args.project_root)

    def fail(reason: str, **extra: Any) -> dict[str, Any]:
        return {"available": False, "reason": reason, "hits": [], **extra}

    try:
        wiki_key = _resolve_browser_wiki_key(base_url, headers, args.timeout, root, args.wiki)
        if not wiki_key:
            return fail("wiki-key-unresolved")
        params = {"q": args.q, "limit": str(max(1, min(args.top, 50)))}
        if args.page_type:
            params["type"] = args.page_type
        if args.tag:
            params["tag"] = args.tag
        search_path = (
            f"/api/v1/wikis/{urllib.parse.quote(wiki_key, safe='')}"
            f"/search?{urllib.parse.urlencode(params)}"
        )
        payload = _fetch_browser_json(base_url, search_path, headers, args.timeout)
    except urllib.error.HTTPError as err:
        return fail("unauthorized" if err.code == 401 else f"http-{err.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        return fail("browser-unavailable", error=str(err))
    except json.JSONDecodeError as err:
        return fail("invalid-response", error=str(err))

    if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
        return fail("invalid-response")

    hits = []
    for hit in payload["hits"]:
        if not isinstance(hit, dict) or not hit.get("path"):
            continue
        hits.append(
            {
                # The browser indexes wiki-dir-relative paths without .md;
                # emit the project-relative file so the caller can read it.
                "file": f"wiki/{hit['path']}.md",
                "title": hit.get("title"),
                "type": hit.get("type"),
                "snippet": hit.get("snippet"),
                "score": hit.get("score"),
            }
        )
    return {
        "available": True,
        "backend": "browser",
        "wiki": wiki_key,
        "query": payload.get("query", args.q),
        "total": payload.get("total", len(hits)),
        "hits": hits,
    }


def cmd_browser_search(args: argparse.Namespace) -> None:
    """Search only the optional Browser index; report unavailable without fallback."""
    print(json.dumps(_browser_search_result(args), ensure_ascii=False))


NON_CONTENT_BASENAMES = {"index.md", "log.md", "overview.md"}


def _local_search_result(args: argparse.Namespace) -> dict[str, Any]:
    """Bounded keyword retrieval over wiki/ — the LAST tier of the retrieval
    chain (Browser MCP → browser-search → this). No Browser required: it must
    keep every SOP fully usable, just a bit more expensive. Bounds: per-file
    scan chars, top-k hits, snippet-sized output — never whole files, never the
    full index.
    """
    root = resolve_root(Path(args.project_root))
    terms = [t.lower() for t in args.q.split() if t.strip()]
    if not terms:
        die("query must not be empty")
    query_lower = args.q.strip().lower()
    per_file = max(500, args.max_file_chars)
    scored: list[tuple[float, dict[str, Any]]] = []
    for path in wiki_files(root):
        if path.name in NON_CONTENT_BASENAMES:
            continue
        try:
            text = read_text(path)[:per_file]
        except OSError:
            continue
        title = frontmatter_value(text, "title") or path.stem
        _, body = strip_frontmatter(text)
        title_lower = title.lower()
        body_lower = body.lower()
        score = 0.0
        first_hit = -1
        for term in terms:
            in_title = term in title_lower
            occurrences = body_lower.count(term)
            if not in_title and not occurrences:
                continue
            score += (5.0 if in_title else 0.0) + float(min(occurrences, 5))
            if occurrences:
                pos = body_lower.find(term)
                if first_hit < 0 or pos < first_hit:
                    first_hit = pos
        if score <= 0:
            continue
        if title_lower == query_lower:
            score += 20.0
        elif title_lower.startswith(query_lower):
            score += 10.0
        if first_hit < 0:
            snippet = truncate(first_body_paragraph(body) or "", 120)
        else:
            start = max(0, first_hit - 60)
            snippet = ("…" if start else "") + body[start:first_hit + 120].strip().replace("\n", " ")
            snippet = truncate(snippet, 180)
        scored.append(
            (
                score,
                {
                    "file": rel_to_root(root, path),
                    "title": title,
                    "type": frontmatter_value(text, "type"),
                    "snippet": snippet,
                    "score": score,
                },
            )
        )
    scored.sort(key=lambda pair: -pair[0])
    top = max(1, min(args.top, 50))
    return {
        "available": True,
        "backend": "local",
        "query": args.q,
        "total": len(scored),
        "hits": [hit for _, hit in scored[:top]],
    }


def cmd_local_search(args: argparse.Namespace) -> None:
    """Search only the bounded local fallback."""
    print(json.dumps(_local_search_result(args), ensure_ascii=False))


def _retrieval_search_result(args: argparse.Namespace) -> dict[str, Any]:
    """Deterministic Browser-first retrieval with a bounded local fallback.

    Keeping the backend choice here prevents an agent from treating the two
    documented commands as peers and skipping the cheaper Browser path. MCP is
    still preferable when a host actually exposes its tools to the turn; this
    command is the runtime-neutral CLI path used otherwise.
    """
    browser = _browser_search_result(args)
    if browser.get("available"):
        return browser
    local = _local_search_result(args)
    local["fallbackFrom"] = "browser"
    local["fallbackReason"] = browser.get("reason", "unavailable")
    return local


def cmd_retrieval_search(args: argparse.Namespace) -> None:
    print(json.dumps(_retrieval_search_result(args), ensure_ascii=False))


def _read_pages_paths(args: argparse.Namespace) -> list[str]:
    values: list[str] = []
    if args.paths:
        values.extend(part.strip() for part in args.paths.split(","))
    if args.paths_file:
        text = read_text(Path(args.paths_file))
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = text.splitlines()
        if not isinstance(data, list):
            die("--paths-file must be a JSON array or one path per line")
        values.extend(str(item).strip() for item in data)
    return [v for v in values if v]


def cmd_read_pages(args: argparse.Namespace) -> None:
    """Budgeted batch page read from disk — the local mirror of the Browser
    MCP `read_pages` tool: at most --max-pages pages, each truncated to
    --max-chars-per-page, the whole result capped at --max-total-chars.
    Use it to pack search candidates into a bounded context instead of
    cat-ing whole files.
    """
    root = resolve_root(Path(args.project_root))
    requested = _read_pages_paths(args)
    if not requested:
        die("no page paths given (use --paths or --paths-file)")
    max_pages = max(1, min(args.max_pages, 20))
    per_page = max(200, min(args.max_chars_per_page, 20000))
    total_budget = max(min(per_page, 1000), min(args.max_total_chars, 100000))

    pages: list[dict[str, Any]] = []
    missing: list[str] = []
    omitted: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    for raw_rel in requested:
        rel = ensure_md_page(normalize_rel(raw_rel))
        if not rel.startswith("wiki/"):
            rel = f"wiki/{rel}"
        if rel in seen:
            continue
        seen.add(rel)
        path = safe_project_path(root, rel)
        if not path.is_file():
            missing.append(rel)
            continue
        if len(pages) >= max_pages or total_budget - total_chars < 200:
            omitted.append(rel)
            continue
        text = read_text(path)
        _, body = strip_frontmatter(text)
        budget = min(per_page, total_budget - total_chars)
        truncated = len(body) > budget
        clipped = body[:budget] + ("…" if truncated else "")
        total_chars += min(len(body), budget)
        pages.append(
            {
                "file": rel,
                "title": frontmatter_value(text, "title") or path.stem,
                "type": frontmatter_value(text, "type"),
                "sources": extract_frontmatter_list(text, "sources"),
                "chars": min(len(body), budget),
                "truncated": truncated,
                "body": clipped,
            }
        )
    print(
        json.dumps(
            {
                "budget": {
                    "maxPages": max_pages,
                    "maxCharsPerPage": per_page,
                    "maxTotalChars": total_budget,
                },
                "totalChars": total_chars,
                "pages": pages,
                "missing": missing,
                "omitted": omitted,
            },
            ensure_ascii=False,
        )
    )


# High-frequency wrong command names weaker models guess, mapped to the real
# subcommand. The real names are also registered as argparse `aliases=` below so
# these exact strings just work (0 round-trips); this map additionally lets the
# "did you mean" hint resolve fuzzy typos of them (e.g. `addd` → `add-blocks`).
_CMD_ALIASES = {
    "add": "add-blocks", "add-block": "add-blocks", "addblocks": "add-blocks",
    "add-review": "add-blocks", "queue": "add-blocks",
    "update": "save", "set": "save", "write": "save",
}


def _did_you_mean(message: str) -> str | None:
    """Build a 'did you mean: X' hint from argparse's invalid-choice error."""
    m = re.search(r"invalid choice: '([^']*)' \(choose from ([^)]*)\)", message)
    if not m:
        return None
    bad = m.group(1)
    choices = re.findall(r"'([^']*)'", m.group(2))
    target = _CMD_ALIASES.get(bad)
    if target and target in choices:
        return f"  → did you mean: {target}"
    # Fuzzy-match the bad token against real choices *and* known alias keys,
    # resolving an alias hit back to its canonical command.
    pool = list(choices) + [k for k, v in _CMD_ALIASES.items() if v in choices]
    hit = difflib.get_close_matches(bad, pool, n=1, cutoff=0.5)
    if hit:
        return f"  → did you mean: {_CMD_ALIASES.get(hit[0], hit[0])}"
    return None


class SmartParser(argparse.ArgumentParser):
    """ArgumentParser that appends a 'did you mean' hint on an unknown
    subcommand, so a model that guesses a wrong command name is pointed at the
    right one instead of just getting a bare usage dump. Propagates to every
    subparser (argparse builds them from the parent's class)."""

    def error(self, message: str):  # noqa: D102
        hint = _did_you_mean(message)
        if hint:
            message = f"{message}\n{hint}"
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = SmartParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("resolve-root")
    p.add_argument("path")
    p.set_defaults(func=cmd_resolve_root)

    p = sub.add_parser("init")
    p.add_argument("project_root")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=init_project)

    p = sub.add_parser("compact-index")
    p.add_argument("project_root")
    p.add_argument("--no-desc", action="store_true", help="description-free listing for the overview digest")
    p.set_defaults(func=lambda a: sys.stdout.write(compact_index(read_text(resolve_root(Path(a.project_root)) / "wiki/index.md"), no_desc=a.no_desc)))

    p = sub.add_parser("merge-index")
    p.add_argument("project_root")
    p.add_argument("--delta-file")
    p.add_argument("--write", action="store_true")
    p.set_defaults(func=cmd_merge_index)

    p = sub.add_parser("merge-page")
    p.add_argument("project_root")
    p.add_argument("page")
    p.add_argument("--incoming-file")
    p.add_argument("--write", action="store_true")
    p.set_defaults(func=cmd_merge_page)

    p = sub.add_parser("apply-blocks")
    p.add_argument("project_root")
    p.add_argument("--blocks-file", "--items", "--file", dest="blocks_file")
    p.add_argument("--source")
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(func=cmd_apply_blocks)

    p = sub.add_parser("source-page")
    p.add_argument("project_root")
    p.add_argument("--url")
    p.add_argument("--raw", help="raw source path (relative to project root or absolute)")
    p.set_defaults(func=cmd_source_page)

    p = sub.add_parser("probe-source", help="size-gate a raw source: small (single-pass) vs large (map-reduce)")
    p.add_argument("project_root")
    p.add_argument("--raw", required=True, help="raw source path (relative to project root or absolute)")
    p.add_argument("--threshold", type=int, default=GATE_CHARS, help=f"body-char gate (default {GATE_CHARS})")
    p.add_argument("--chunk-chars", type=int, default=CHUNK_CHARS)
    p.add_argument("--overlap", type=int, default=CHUNK_OVERLAP)
    p.set_defaults(func=cmd_probe_source)

    p = sub.add_parser("split-source", help="chunk a large source into staging for the MAP phase")
    p.add_argument("project_root")
    p.add_argument("--raw", required=True, help="raw source path (relative to project root or absolute)")
    p.add_argument("--url", help="source URL (slug fallback; raw filename wins)")
    p.add_argument("--chunk-chars", type=int, default=CHUNK_CHARS)
    p.add_argument("--overlap", type=int, default=CHUNK_OVERLAP)
    p.add_argument("--write", action="store_true", help="write chunks + manifest (else dry-run)")
    p.add_argument("--force", action="store_true", help="wipe existing staging and re-split")
    p.set_defaults(func=cmd_split_source)

    p = sub.add_parser("stage-status", help="report MAP progress / REDUCE-readiness, or mark a chunk done")
    p.add_argument("project_root")
    p.add_argument("--source", required=True, help="raw source path (relative to project root or absolute)")
    p.add_argument("--mark-done", type=int, metavar="INDEX", help="mark chunk INDEX's MAP result done")
    p.set_defaults(func=cmd_stage_status)

    p = sub.add_parser("dedup-summaries")
    p.add_argument("project_root")
    p.set_defaults(func=cmd_dedup_summaries)

    p = sub.add_parser("dedup-merge")
    p.add_argument("project_root")
    p.add_argument("--canonical", required=True, help="slug to keep")
    p.add_argument("--slugs", required=True, help="comma-separated slugs in the group (incl. canonical)")
    p.add_argument("--body-file", required=True, help="LLM-merged canonical page content")
    p.set_defaults(func=cmd_dedup_merge)

    p = sub.add_parser("dedup-not-duplicate")
    p.add_argument("project_root")
    p.add_argument("--slugs", required=True, help="comma-separated slugs to whitelist as NOT duplicates")
    p.set_defaults(func=cmd_dedup_not_duplicate)

    p = sub.add_parser("lint")
    p.add_argument("project_root")
    p.add_argument("--exit-code", action="store_true",
                   help="exit non-zero when issues at/above --fail-on exist (for cron/CI gates)")
    p.add_argument("--fail-on", choices=["info", "warning", "error"], default="warning",
                   help="minimum severity that counts as failing under --exit-code (default: warning)")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser(
        "tags",
        help="the wiki's live tag vocabulary — read this BEFORE generating tags for a new page")
    p.add_argument("project_root")
    p.add_argument("--q", help="scope to tags on the pages nearest this text — what ingest should "
                               "pass (the source's topic/entities, space-separated). Surfaces "
                               "relevant new tags that the global backbone view hides.")
    p.add_argument("--paths", help="comma-separated wiki page paths to scope to, instead of --q "
                                   "(most precise: pass the working set step 5 already selected)")
    p.add_argument("--paths-file", help="JSON array or one-per-line file of wiki page paths to scope to")
    p.add_argument("--scope-top", type=int, default=12,
                   help="pages the --q scope considers (default 12)")
    p.add_argument("--limit", type=int, default=40,
                   help="max established tags in text output (0 = all; default 40)")
    p.add_argument("--audit", action="store_true",
                   help="also list singletons, duplicate pairs and untagged pages (cleanup view — "
                        "not needed when tagging a page)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--verbose", action="store_true",
                   help="with --json, also emit pagesFor (tag → pages using it)")
    p.set_defaults(func=cmd_tags)

    p = sub.add_parser(
        "health",
        help="project-level setup drift: schema version, empty domain table, purpose.md stubs, tag sprawl")
    p.add_argument("project_root")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser(
        "schema-upgrade",
        help="compare this wiki's schema.md against the bundled template; --apply to update it")
    p.add_argument("project_root")
    p.add_argument("--diff", action="store_true", help="print the unified diff")
    p.add_argument("--apply", action="store_true",
                   help="write the new template (backs up the old, carries the domain table over)")
    p.add_argument("--accept-local-loss", action="store_true",
                   help="apply even though schema.md carries substantial hand-written content "
                        "the upgrade cannot preserve (only the domain table is carried)")
    p.set_defaults(func=cmd_schema_upgrade)

    p = sub.add_parser("sweep-reviews")
    p.add_argument("project_root")
    p.set_defaults(func=cmd_sweep_reviews)

    p = sub.add_parser("trace")
    p.add_argument("project_root")
    p.add_argument("scope")
    p.add_argument("--source")
    p.add_argument("--input-chars", type=int)
    p.add_argument("--output-chars", type=int)
    p.add_argument("--backend", choices=["mcp", "browser", "local"],
                   help="retrieval backend that served this step")
    p.add_argument("--candidates", type=int, help="search hits considered")
    p.add_argument("--pages-read", type=int, help="pages actually read into context")
    p.add_argument("--context-chars", type=int, help="chars packed into the prompt context")
    p.add_argument("--prompt-tokens", type=int, help="prompt tokens if the host reports them")
    p.add_argument("--cache-read-tokens", type=int, help="cache-read tokens if the host reports them")
    p.add_argument("--extra")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("browser-share", help="probe optional My LLM Wiki Browser share URL")
    p.add_argument("project_root", nargs="?", help="wiki root used to resolve the browser wiki key")
    p.add_argument("--page", help="wiki page to deep-link, e.g. wiki/sources/example.md")
    p.add_argument("--wiki", help="browser wiki key override")
    p.add_argument("--label", default="点击查看总结", help="Markdown link label")
    p.add_argument("--base-url", help="override local browser base URL")
    p.add_argument("--token", help="override browser auth token")
    p.add_argument("--timeout", type=float, default=1.5)
    p.set_defaults(func=cmd_browser_share)

    p = sub.add_parser(
        "lint-scope",
        help="pages changed since last marked semantic lint + one-hop neighbors",
    )
    p.add_argument("project_root")
    p.add_argument(
        "--mark", action="store_true",
        help="record current page hashes as semantically linted (run AFTER the pass)",
    )
    p.set_defaults(func=cmd_lint_scope)

    p = sub.add_parser(
        "neighbors", help="one-hop neighborhood of a wiki page (conflict-sentinel input)"
    )
    p.add_argument("project_root")
    p.add_argument("--page", help="existing wiki page, e.g. wiki/concepts/x.md")
    p.add_argument("--slugs", help="comma-separated wikilink targets of a not-yet-written page")
    p.add_argument(
        "--max", type=int, default=12, dest="max_neighbors",
        help="cap on returned neighbors (closest first)",
    )
    p.set_defaults(func=cmd_neighbors)

    p = sub.add_parser(
        "browser-search", help="full-text search via optional My LLM Wiki Browser index"
    )
    p.add_argument("project_root", nargs="?", help="wiki root used to resolve the browser wiki key")
    p.add_argument("--q", required=True, help="search query")
    p.add_argument("--top", type=int, default=8, help="max hits to return (clamped to 50)")
    p.add_argument("--type", dest="page_type", help="filter by page type (entity/concept/source/…)")
    p.add_argument("--tag", help="filter by tag")
    p.add_argument("--wiki", help="browser wiki key override")
    p.add_argument("--base-url", help="override local browser base URL")
    p.add_argument("--token", help="override browser auth token")
    p.add_argument("--timeout", type=float, default=3.0)
    p.set_defaults(func=cmd_browser_search)

    p = sub.add_parser(
        "retrieval-search",
        help="deterministic Browser-first search with bounded local fallback",
    )
    p.add_argument("project_root", help="wiki root used for Browser resolution and local fallback")
    p.add_argument("--q", required=True, help="search query")
    p.add_argument("--top", type=int, default=8, help="max hits to return (clamped to 50)")
    p.add_argument("--wiki", help="browser wiki key override")
    p.add_argument("--base-url", help="override local browser base URL")
    p.add_argument("--token", help="override browser auth token")
    p.add_argument("--timeout", type=float, default=3.0)
    p.add_argument(
        "--max-file-chars", type=int, default=8000,
        help="per-file scan budget used only by the local fallback",
    )
    p.set_defaults(page_type=None, tag=None, func=cmd_retrieval_search)

    p = sub.add_parser(
        "local-search",
        help="bounded keyword retrieval over wiki/ — last-tier fallback when the Browser is absent",
    )
    p.add_argument("project_root")
    p.add_argument("--q", required=True, help="search query (space-separated terms, all scored)")
    p.add_argument("--top", type=int, default=8, help="max hits to return (clamped to 50)")
    p.add_argument(
        "--max-file-chars", type=int, default=8000,
        help="per-file scan budget in chars (default 8000)",
    )
    p.set_defaults(func=cmd_local_search)

    p = sub.add_parser(
        "read-pages",
        help="budgeted batch page read (local mirror of the Browser MCP read_pages tool)",
    )
    p.add_argument("project_root")
    p.add_argument("--paths", help="comma-separated wiki page paths, e.g. wiki/concepts/a.md,wiki/entities/b.md")
    p.add_argument("--paths-file", help="JSON array or one path per line (for CJK-heavy lists)")
    p.add_argument("--max-pages", type=int, default=5, help="max pages to read (clamped to 20)")
    p.add_argument("--max-chars-per-page", type=int, default=6000)
    p.add_argument("--max-total-chars", type=int, default=24000)
    p.set_defaults(func=cmd_read_pages)

    p = sub.add_parser("review")
    review_sub = p.add_subparsers(dest="review_cmd", required=True)
    r = review_sub.add_parser("list")
    r.add_argument("project_root")
    r.add_argument("--status", choices=["open", "resolved", "all"], default="open")
    r.set_defaults(func=cmd_review)
    r = review_sub.add_parser(
        "add-blocks", aliases=["add", "add-block", "addblocks", "add-review", "queue"]
    )
    r.add_argument("project_root")
    r.add_argument("--blocks-file", "--items", "--file", dest="blocks_file")
    r.add_argument("--source")
    r.set_defaults(func=cmd_review)
    r = review_sub.add_parser("resolve")
    r.add_argument("project_root")
    r.add_argument("id")
    r.add_argument("--action", default="resolved")
    r.set_defaults(func=cmd_review)
    r = review_sub.add_parser(
        "get", help="print ONE review item as JSON (avoid reading all of review.json)"
    )
    r.add_argument("project_root")
    r.add_argument("id")
    r.set_defaults(func=cmd_review)
    r = review_sub.add_parser(
        "find", aliases=["search", "filter"],
        help="filter review items by type/status/keywords; JSON output",
    )
    r.add_argument("project_root")
    r.add_argument("--q", help="space-separated keywords, all must match title/description/pages")
    r.add_argument("--type", help="filter by review type (suggestion/contradiction/…)")
    r.add_argument("--status", choices=["open", "resolved", "all"], default="open")
    r.add_argument("--limit", type=int, default=10)
    r.set_defaults(func=cmd_review)

    p = sub.add_parser("cache")
    cache_sub = p.add_subparsers(dest="cache_cmd", required=True)
    c = cache_sub.add_parser("check")
    c.add_argument("project_root")
    c.add_argument("source")
    c.set_defaults(func=cmd_cache)
    c = cache_sub.add_parser("save", aliases=["update", "set", "write"])
    c.add_argument("project_root")
    c.add_argument("source")
    c.add_argument("files", nargs="*")
    c.add_argument(
        "--files-file",
        help="newline-delimited paths or a JSON array; avoids long/dynamic shell argv",
    )
    c.set_defaults(func=cmd_cache)
    c = cache_sub.add_parser("pending")
    c.add_argument("project_root")
    c.set_defaults(func=cmd_cache_pending)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    rc = args.func(args)
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
