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


def cmd_browser_search(args: argparse.Namespace) -> None:
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

    def fail(reason: str, **extra: Any) -> None:
        result = {"available": False, "reason": reason, "hits": [], **extra}
        print(json.dumps(result, ensure_ascii=False))

    try:
        wiki_key = _resolve_browser_wiki_key(base_url, headers, args.timeout, root, args.wiki)
        if not wiki_key:
            fail("wiki-key-unresolved")
            return
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
        fail("unauthorized" if err.code == 401 else f"http-{err.code}")
        return
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        fail("browser-unavailable", error=str(err))
        return
    except json.JSONDecodeError as err:
        fail("invalid-response", error=str(err))
        return

    if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
        fail("invalid-response")
        return

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
    result = {
        "available": True,
        "wiki": wiki_key,
        "query": payload.get("query", args.q),
        "total": payload.get("total", len(hits)),
        "hits": hits,
    }
    print(json.dumps(result, ensure_ascii=False))


NON_CONTENT_BASENAMES = {"index.md", "log.md", "overview.md"}


def cmd_local_search(args: argparse.Namespace) -> None:
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
    print(
        json.dumps(
            {
                "available": True,
                "backend": "local",
                "query": args.q,
                "total": len(scored),
                "hits": [hit for _, hit in scored[:top]],
            },
            ensure_ascii=False,
        )
    )


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
