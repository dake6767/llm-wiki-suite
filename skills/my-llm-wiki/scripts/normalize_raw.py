#!/usr/bin/env python3
"""Normalize an opencli capture into an immutable RAW source file, compatible
with the open-source `llm_wiki` app and Obsidian.

The hard, fiddly, deterministic part of ingestion lives here so the agent
doesn't reinvent it on every call: pick the target wiki, parse opencli's
`>`-style header into real YAML frontmatter, relocate media into the shared
`raw/assets/` folder, rewrite the markdown links, flag un-localizable video,
and refuse to clobber anything that already exists (RAW is immutable).

Output layout (matches the `llm_wiki` app / an Obsidian vault):

  <wiki>/raw/sources/<source_type>/<YYYY-MM-DD>-<slug>.md   one file per item
  <wiki>/raw/assets/<YYYY-MM-DD>-<slug>--<name>             shared media folder

Images are referenced with relative links (`../../assets/<file>`) so they
render in the app, in Obsidian, and in any plain-markdown viewer.

Two input shapes are supported:

  1. --from <dir>   An opencli output folder, e.g. what `weixin download` or
                    `web read` produce: a folder containing one `*.md` plus an
                    `images/` subfolder with relative links.

  2. --md <file> [--assets <dir>]
                    A markdown file you assembled yourself (e.g. one X tweet
                    composed from web read text + `twitter download` media).

Wiki resolution (which repo to write into), highest priority first:
  --wiki <path>  >  nearest ancestor of CWD that is a wiki  >  $LLM_WIKI_DEFAULT
  >  registry default (wikis.py)  >  the sole registered wiki
A directory is a wiki when it has `schema.md` + a `wiki/` dir (the app's own
rule) or a `.llm-wiki/project.json`. The agent normally passes --wiki explicitly
after classifying by topic (SKILL.md §1); the registry fallbacks are a safety
net so a single-wiki / default setup just works.

Prints a YAML summary (dest path, asset count, video flags) to stdout.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clean_md import clean_markdown  # noqa: E402  (sibling module)
try:
    import wikis  # noqa: E402  (sibling module — registry fallback)
except ImportError:  # pragma: no cover — resolution still works without it
    wikis = None  # type: ignore

RAW_SOURCES = "raw/sources"
RAW_ASSETS = "raw/assets"
DEFAULT_TAGS = ["inbox"]

# Host → platform bucket. Safety net for the routing rule in SKILL.md: a platform
# note (小红书/X/公众号) is often captured via the generic `web read` fallback, and
# the agent then passes --source-type web by reflex, leaking it into raw/sources/web/.
# When the URL's host names a known platform, we correct a generic `web`/empty type
# so the bucket + frontmatter always match the source. A deliberate non-generic type
# (note, video, an already-correct xiaohongshu) is never touched.
_HOST_SOURCE_TYPE = (
    ("xiaohongshu.com", "xiaohongshu"),
    ("xhslink.com", "xiaohongshu"),
    ("weixin.qq.com", "wechat"),
    ("x.com", "x"),
    ("twitter.com", "x"),
)


def source_type_from_host(url: str) -> str:
    """Return the platform bucket for a URL's host, or '' if not a known platform."""
    host = urlparse(url).netloc.lower()
    for needle, source_type in _HOST_SOURCE_TYPE:
        if host == needle or host.endswith("." + needle):
            return source_type
    return ""


# ---------------------------------------------------------------------------
# Tiny YAML emitter (avoid a hard dependency on PyYAML for the values we write).
# ---------------------------------------------------------------------------

# YAML indicator characters: a plain (unquoted) scalar may not START with any of
# these. The classic offender for us is an X handle author like `@AomyYing` — left
# unquoted it makes the WHOLE frontmatter invalid YAML, so Obsidian shows no
# properties at all. Quote any value that begins with one of these.
_YAML_LEAD = set("@`!&*?|>%#,-:[]{}\"'~ ")


def _yaml_dump(d: dict) -> str:
    """Emit deterministic, frontmatter-grade YAML for the values we produce."""
    def fmt_scalar(v):
        if v is None:
            return '""'
        if isinstance(v, bool):
            return "true" if v else "false"
        s = str(v)
        if s == "":
            return '""'
        if re.search(r'[:#\[\]{}",\n]', s) or s.strip() != s or s[0] in _YAML_LEAD:
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return s

    lines = []
    for k, v in d.items():
        if isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
            else:
                lines.append(f"{k}:")
                for item in v:
                    lines.append(f"  - {fmt_scalar(item)}")
        else:
            lines.append(f"{k}: {fmt_scalar(v)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Wiki resolution
# ---------------------------------------------------------------------------

def _is_wiki_root(d: Path) -> bool:
    """The `llm_wiki` app validates a project by `schema.md` + `wiki/`; the
    identity file appears lazily. Accept either signal."""
    if (d / "schema.md").exists() and (d / "wiki").is_dir():
        return True
    if (d / ".llm-wiki" / "project.json").exists():
        return True
    return False


def resolve_wiki(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.exists():
            sys.exit(f"error: --wiki path does not exist: {p}")
        return p
    cur = Path.cwd().resolve()
    for cand in [cur, *cur.parents]:
        if _is_wiki_root(cand):
            return cand
    env = os.environ.get("LLM_WIKI_DEFAULT")
    if env:
        return Path(env).expanduser().resolve()
    # Registry fallbacks (a safety net — the agent usually passes --wiki after
    # classifying). Only ever target a wiki that actually exists on disk.
    if wikis is not None:
        dp = wikis.default_path()
        if dp:
            return dp.resolve()
        sole = wikis.registered_paths(existing_only=True)
        if len(sole) == 1:
            return sole[0].resolve()
    sys.exit(
        "error: could not resolve a target wiki. Pass --wiki <path>, run inside "
        "a wiki (a dir with schema.md + wiki/), set $LLM_WIKI_DEFAULT, or register "
        "one with scripts/wikis.py. To make a new wiki, run scripts/init_wiki.py."
    )


# ---------------------------------------------------------------------------
# opencli header parsing
# ---------------------------------------------------------------------------

OPENCLI_HEADER = {
    "author": [r"公众号[:：]\s*(.+)", r"作者[:：]\s*(.+)", r"[Aa]uthor[:：]\s*(.+)"],
    "publish_time": [r"发布时间[:：]\s*(.+)", r"[Pp]ublish(?:ed)?[ _]?time[:：]\s*(.+)"],
    "source_url": [r"原文链接[:：]\s*(.+)", r"[Ss]ource[:：]\s*(.+)", r"[Uu]rl[:：]\s*(.+)"],
}


def parse_capture(md_text: str) -> tuple[dict, str]:
    """Split an opencli markdown capture into (metadata, body).

    opencli emits:  `# Title` then `> 公众号: ...` / `> 发布时间: ...` /
    `> 原文链接: ...` lines, a `---` rule, then the body. We lift those into
    metadata and return the body with that preamble stripped. Files that are
    already plain markdown just come back with title = first H1 (if any)."""
    meta: dict = {}
    lines = md_text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines):
        m = re.match(r"^#\s+(.+)$", lines[i].strip())
        if m:
            meta["title"] = m.group(1).strip()
            i += 1
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith(">"):
            content = stripped.lstrip(">").strip()
            for field, pats in OPENCLI_HEADER.items():
                for pat in pats:
                    mm = re.match(pat, content)
                    if mm and field not in meta:
                        meta[field] = mm.group(1).strip()
            i += 1
            continue
        break
    j = i
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j < len(lines) and re.match(r"^-{3,}$", lines[j].strip()):
        i = j + 1
    body = "\n".join(lines[i:]).strip()
    return meta, body


# ---------------------------------------------------------------------------
# slug / date
# ---------------------------------------------------------------------------

def slugify(title: str) -> str:
    if not title:
        return "untitled"
    s = title.strip()
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"[\s/\\]+", "-", s)
    s = re.sub(r"[^\w㐀-鿿぀-ヿ-]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if len(s) > 60:
        s = s[:60].rstrip("-")
    return s or "untitled"


CHROME_MARKERS = re.compile(r"(微信扫一扫|赞赏作者|名称已清空|最低赞赏|喜欢作者|其它金额)")
UNLOCALIZED_IMG = re.compile(r"(<img\b|data-src=|!\[[^\]]*\]\(https?://[^)]*\.(?:png|jpe?g|gif|webp))", re.I)

# Working-file names a pipeline uses for its intermediates. When the title
# would fall back to one of these (no --title, no H1), the capture step was
# skipped — refuse instead of minting a RAW file titled "anchored"/"transcript"
# that then needs frontmatter patching + a file rename (a recurring live
# incident with video captures fed the bare srt_to_anchors.py output).
GENERIC_STEMS = {
    "transcript", "anchored", "anchors", "subs", "subtitles", "srt",
    "output", "out", "result", "final", "temp", "tmp", "test",
    "page", "capture", "untitled", "index", "content", "text",
    "doc", "document", "article", "note", "notes", "audio", "video",
}


def assess_capture(body: str, asset_count: int, is_note: bool = False,
                   has_source_file: bool = False, is_video: bool = False) -> list[str]:
    """Sanity-check a capture so failures and page-structure changes surface
    loudly instead of producing a silently degraded RAW. The site HTML (and the
    opencli adapter that parses it) will drift over time; these tripwires are how
    we find out a capture went wrong without eyeballing every file. Tuned for low
    false-positives — a warning means 'a human should glance at this', not 'broken'.

    `is_note` relaxes the checks for first-party notes: a two-line thought is a
    valid note, not a failed fetch, so the 'almost no text' tripwire (aimed at
    broken web captures) is skipped for notes. `has_source_file` does the same for
    documents whose original is archived: sparse text extraction (e.g. a scanned
    PDF) isn't a lost capture when the faithful original is kept alongside."""
    warnings: list[str] = []
    text_len = len(re.sub(r"\s+", "", body))
    if not is_note and not has_source_file and text_len < 200 and asset_count == 0:
        warnings.append(
            f"capture has almost no text ({text_len} chars) and no media — likely "
            f"failed or the page structure changed"
        )
    leftover = UNLOCALIZED_IMG.findall(body)
    if leftover:
        warnings.append(
            f"{len(leftover)} image reference(s) left un-localized (remote URL or raw "
            f"<img>) — the source's image markup may have changed"
        )
    if len(CHROME_MARKERS.findall(body)) >= 2 and text_len < 800:
        warnings.append(
            "capture looks dominated by page chrome (打赏/UI boilerplate) — main "
            "content extraction may be incomplete"
        )
    if is_video and asset_count == 0:
        warnings.append(
            "video capture localized no media — the acceptance contract expects a "
            "cover (images/cover.jpg referenced from transcript.md)"
        )
    return warnings


def existing_original_id(md_file: Path) -> str:
    """Read original_id from an already-written RAW file's frontmatter, so we can
    tell a genuine re-capture (same id) from a mere slug collision between two
    different sources (different ids)."""
    if not md_file.exists():
        return ""
    try:
        text = md_file.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return ""
    for line in m.group(1).splitlines():
        mm = re.match(r"^original_id:\s*(.*)$", line.strip())
        if mm:
            return mm.group(1).strip().strip("\"'")
    return ""


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul",
     "aug", "sep", "oct", "nov", "dec"], 1)}


def normalize_date(raw: str | None, captured: _dt.date) -> str:
    """Best-effort YYYY-MM-DD from messy publish_time strings."""
    if raw:
        # Month-name formats first — X/Twitter's native stamp
        # ("Fri Jun 05 14:26:41 +0000 2026"; also "May 4, 2026"). The numeric
        # parser below would otherwise misread "+0000"/the time as the date.
        mm = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b",
                       raw, re.I)
        years = [int(y) for y in re.findall(r"\d{4}", raw) if 1900 <= int(y) <= 2200]
        if mm and years:
            try:
                return _dt.date(years[0], _MONTHS[mm.group(1).lower()], int(mm.group(2))).isoformat()
            except ValueError:
                pass
        # Numeric formats: 2026年5月4日 / 2026-05-04 / 2026/5/4 / 2026-06-07T05:23Z.
        m = re.search(r"(\d{4})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})", raw)
        if m:
            y, mo, d = (int(x) for x in m.groups())
            try:
                return _dt.date(y, mo, d).isoformat()
            except ValueError:
                pass
    return captured.isoformat()


# ---------------------------------------------------------------------------
# media handling
# ---------------------------------------------------------------------------

IMG_LINK = re.compile(r"(!\[[^\]]*\]\()([^)]+)(\))")
VIDEO_URL = re.compile(r"(v\.qq\.com|\.mp4|\.m3u8|youtube\.com|youtu\.be|bilibili\.com)", re.I)
VIDEO_TAG = re.compile(r"<video[\s/>]|blob:|腾讯视频|v\.qq\.com|<iframe", re.I)


def sniff_ext(path: Path) -> str | None:
    """Return the true file extension (with dot) from magic bytes, or None if
    unrecognized. CDNs (X media especially) routinely serve a JPEG under a
    `?format=png` name; opencli saves it by that name, and a `.png` that is
    really a JPEG looks broken in some readers and lies in the archive. We sniff
    the bytes and correct the suffix so RAW is honest about what's on disk."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
    except OSError:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:2] == b"BM":
        return ".bmp"
    if head[:4] == b"%PDF":
        return ".pdf"
    if head[:4] == b"\x1aE\xdf\xa3":  # EBML / Matroska
        return ".webm"
    if head[4:8] == b"ftyp":  # ISO base media (mp4 / mov / m4v ...)
        return ".mov" if head[8:10] == b"qt" else ".mp4"
    stripped = head.lstrip()
    if stripped[:5].lower() == b"<?xml" or stripped[:4].lower() == b"<svg":
        return ".svg"
    return None


def _ext_equiv(a: str, b: str) -> bool:
    """Treat the jpeg spellings as one so a correct .jpeg isn't renamed to .jpg."""
    def norm(e: str) -> str:
        e = e.lower()
        return ".jpg" if e in (".jpg", ".jpeg", ".jpe") else e
    return norm(a) == norm(b)


VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


def collect_and_rewrite(
    body: str, src_media: Path | None, assets_dir: Path, md_dir: Path, prefix: str
) -> tuple[str, int, list[str], bool, bool]:
    """Copy referenced local media into the shared `raw/assets/` folder (names
    prefixed with this capture's slug to avoid collisions across sources),
    rewrite links to a path relative to the source .md, and surface video links
    (kept as-is, never broken). Also reports whether a video was localized so the
    caller doesn't simultaneously flag the same video as un-downloadable."""
    asset_count = 0
    videos: list[str] = []
    copied: dict[str, str] = {}
    local_video = False

    def repl(m: re.Match) -> str:
        nonlocal asset_count, local_video
        pre, url, post = m.group(1), m.group(2).strip(), m.group(3)
        if re.match(r"^https?://", url):
            return m.group(0)  # remote image left untouched (rare; opencli localizes)
        if url in copied:
            val = copied[url]
            # Video is stored as a ready-made Obsidian embed; images as a rel path
            # to re-wrap with the current alt text.
            return val if val.startswith("![[") else f"{pre}{val}{post}"
        candidate = None
        if src_media is not None:
            candidate = (src_media / Path(url)).resolve()
            if not candidate.exists():
                candidate = (src_media / Path(url).name)
        if candidate and candidate.exists():
            assets_dir.mkdir(parents=True, exist_ok=True)
            name = candidate.name
            sniffed = sniff_ext(candidate)
            if sniffed and not _ext_equiv(candidate.suffix, sniffed):
                name = candidate.stem + sniffed  # correct a mislabeled extension
            asset_name = f"{prefix}--{name}"
            target = assets_dir / asset_name
            if not target.exists():
                shutil.copy2(candidate, target)
            asset_count += 1
            if Path(asset_name).suffix.lower() in VIDEO_EXTS:
                # Obsidian plays video only via wikilink embeds (`![](file.mp4)`
                # won't), and resolves them by basename across the vault.
                local_video = True
                copied[url] = f"![[{asset_name}]]"
                return copied[url]
            rel = os.path.relpath(target, md_dir).replace(os.sep, "/")
            copied[url] = rel
            return f"{pre}{rel}{post}"
        return m.group(0)

    body = IMG_LINK.sub(repl, body)

    has_video = bool(VIDEO_TAG.search(body))
    for m in re.finditer(r'https?://[^\s"\'<>)]+', body):
        u = m.group(0)
        if VIDEO_URL.search(u) and "thumb" not in u.lower():
            videos.append(u)
    videos = sorted(set(videos))
    if videos:
        has_video = True
    # A localized video shouldn't also be recorded/announced as un-downloadable:
    # drop the remote video URLs (they're the source of the file we just saved).
    if local_video:
        has_video = True
        videos = []
    return body, asset_count, videos, has_video, local_video


def archive_source_file(path: Path, assets_dir: Path, md_dir: Path, prefix: str) -> str:
    """Copy an original source file (e.g. the PDF a `doc` was converted from) into
    the shared `raw/assets/` folder so the faithful original is kept next to its
    text extraction. Returns the link relative to the RAW .md (for `source_file:`).

    Reuses the media naming convention (`<prefix>--<name>`, slug-prefixed to avoid
    collisions) and the magic-byte extension check. Office formats (docx/pptx/xlsx/
    epub) are zips that `sniff_ext` doesn't recognize → it returns None and we keep
    the original extension. Copy is idempotent (skip if the target already exists)."""
    assets_dir.mkdir(parents=True, exist_ok=True)
    name = path.name
    sniffed = sniff_ext(path)
    if sniffed and not _ext_equiv(path.suffix, sniffed):
        name = path.stem + sniffed
    target = assets_dir / f"{prefix}--{name}"
    if not target.exists():
        shutil.copy2(path, target)
    return os.path.relpath(target, md_dir).replace(os.sep, "/")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Normalize an opencli capture into a RAW source file.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--from", dest="from_dir", help="opencli output folder (md + images/)")
    g.add_argument("--md", help="path to a markdown file you assembled")
    ap.add_argument("--assets", help="media folder for the --md case")
    ap.add_argument("--wiki", help="target wiki root (else resolved from CWD / $LLM_WIKI_DEFAULT)")
    ap.add_argument("--source-type", required=True,
                    help="wechat | x | xiaohongshu | web | ... (becomes the raw/sources/<type>/ bucket)")
    ap.add_argument("--source-url", default="", help="canonical original URL")
    ap.add_argument("--original-id", default="", help="platform id (tweet id, mp article id, ...)")
    ap.add_argument("--title", default="", help="override title (else parsed from capture)")
    ap.add_argument("--author", default="", help="override author (else parsed; useful when a 图文 web-read fallback lost the author that weixin download had)")
    ap.add_argument("--publish-time", default="", help="override publish_time string (else parsed)")
    ap.add_argument("--source-file", default="",
                    help="original file to archive alongside the text (e.g. the PDF "
                         "a doc was converted from) — copied into raw/assets/ and "
                         "recorded as `source_file:` so the faithful original is kept")
    ap.add_argument("--tags", default="", help="comma-separated extra tags")
    ap.add_argument("--related", default="",
                    help="comma-separated refs this note responds to (a captured "
                         "source slug, an [[wikilink]], a URL, or a topic) — only "
                         "meaningful for --source-type note")
    ap.add_argument("--captured-at", default="",
                    help="ISO datetime override (the agent should pass `date -u +%Y-%m-%dT%H:%M:%SZ`)")
    ap.add_argument("--on-exists", choices=["version", "skip", "fail"], default="version",
                    help="what to do if the RAW item already exists (default: version)")
    args = ap.parse_args()

    wiki = resolve_wiki(args.wiki)

    # Locate source markdown + its media root.
    if args.from_dir:
        src = Path(args.from_dir).expanduser().resolve()
        if not src.is_dir():
            sys.exit(f"error: --from is not a directory: {src}")
        mds = sorted(src.glob("*.md"))
        if not mds:
            sys.exit(f"error: no .md found in {src}")
        if len(mds) == 1:
            md_file = mds[0]
        else:
            # Multiple .md files: the alphabetically-first one is NOT a safe
            # pick — a video temp dir holds both anchored.md (unpolished
            # srt_to_anchors.py intermediate) and transcript.md (the assembled
            # deliverable), and "anchored" sorts first (live incident: the raw
            # intermediate got normalized while the polished file sat beside
            # it). Prefer the contract name, then drop known intermediates;
            # if still ambiguous, refuse rather than guess.
            intermediates = {"anchored", "anchors", "subs", "subtitles", "srt", "cues"}
            named = [m for m in mds if m.stem.lower() == "transcript"]
            candidates = named or [m for m in mds if m.stem.lower() not in intermediates]
            if len(candidates) == 1:
                md_file = candidates[0]
                skipped = [m.name for m in mds if m != md_file]
                print(f"note: {len(mds)} .md files in {src} — using {md_file.name}, "
                      f"ignoring intermediate(s): {', '.join(skipped)}", file=sys.stderr)
            else:
                sys.exit(
                    f"error: {len(mds)} .md files in {src} and no unambiguous "
                    f"deliverable ({', '.join(m.name for m in mds)}) — pass the "
                    f"right one via --md, or remove the extras from the folder."
                )
        media_root = src
    else:
        md_file = Path(args.md).expanduser().resolve()
        if not md_file.exists():
            sys.exit(f"error: --md not found: {md_file}")
        media_root = Path(args.assets).expanduser().resolve() if args.assets else md_file.parent

    md_text = md_file.read_text(encoding="utf-8")
    parsed, body = parse_capture(md_text)

    title = args.title or parsed.get("title") or ""
    if not title:
        stem = md_file.stem
        if stem.lower() in GENERIC_STEMS:
            sys.exit(
                f"error: no --title and no H1 in the markdown — the title would "
                f"default to the working filename '{stem}'. Pass --title \"<real "
                f"title>\" or put a `# <title>` H1 in the file. For a video "
                f"capture, assemble the full transcript.md (H1 + `>` header + "
                f"cover) per my-llm-wiki-video's video-capture-sop.md §1 — don't normalize the bare "
                f"srt_to_anchors.py output."
            )
        title = stem
    source_url = args.source_url or parsed.get("source_url", "")

    # Correct a generic bucket when the host names a known platform (see above).
    if args.source_type in ("web", ""):
        inferred = source_type_from_host(source_url)
        if inferred and inferred != args.source_type:
            print(
                f"note: source_type '{args.source_type or '(empty)'}' → '{inferred}' "
                f"(inferred from host {urlparse(source_url).netloc})",
                file=sys.stderr,
            )
            args.source_type = inferred
    publish_time = args.publish_time or parsed.get("publish_time", "")
    author = args.author or parsed.get("author", "")
    if author in ("", "-"):
        mu = re.search(r"(?:x\.com|twitter\.com)/([^/]+)/(?:status|article)/", source_url)
        author = ("@" + mu.group(1)) if mu and mu.group(1) not in ("i", "home") else ""

    captured_at = args.captured_at or _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    captured_date = _dt.date.fromisoformat(captured_at[:10])
    date_str = normalize_date(publish_time, captured_date)
    slug = slugify(title)

    dest_parent = wiki / RAW_SOURCES / args.source_type
    base = f"{date_str}-{slug}"
    dest = dest_parent / f"{base}.md"

    if dest.exists():
        # Identity is the source's original_id, not the slug. Same id → genuine
        # re-capture (apply the on-exists policy); different ids → two distinct
        # sources that slugified the same (e.g. two tweets from one author on one
        # day) — those must each land, never be skipped.
        existing_id = existing_original_id(dest)
        same_source = bool(args.original_id) and existing_id == args.original_id

        if same_source:
            if args.on_exists == "fail":
                sys.exit(f"error: RAW item already exists (immutable): {dest}")
            if args.on_exists == "skip":
                print(_yaml_dump({"status": "skipped_exists", "dest": str(dest)}))
                return
            n = 2
            while (dest_parent / f"{base}-v{n}.md").exists():
                n += 1
            base = f"{base}-v{n}"
            dest = dest_parent / f"{base}.md"
        else:
            suffix = args.original_id[-8:] if args.original_id else ""
            if suffix and not (dest_parent / f"{base}-{suffix}.md").exists():
                base = f"{base}-{suffix}"
            else:
                n = 2
                while (dest_parent / f"{base}-{n}.md").exists():
                    n += 1
                base = f"{base}-{n}"
            dest = dest_parent / f"{base}.md"

    # A `note` is the wiki owner's own first-party writing, not a web capture —
    # it has no HTML→Markdown conversion damage, so the structural "repairs"
    # (un-escaping, heading merges) would only risk altering intentional
    # formatting. Skip cleanup for notes; run it for every captured source.
    is_note = args.source_type == "note"
    if not is_note:
        # Repair the converter's structural damage (exploded links, split headings,
        # over-escaping, social chrome) FIRST — so dropped chrome (e.g. the X avatar)
        # never gets downloaded into raw/assets/ as an orphan. collect_and_rewrite then
        # localizes only the media still referenced (cover + inline images).
        body = clean_markdown(body, args.source_type, title, author)

    assets_dir = wiki / RAW_ASSETS
    new_body, asset_count, videos, has_video, has_local_video = collect_and_rewrite(
        body, media_root, assets_dir, dest_parent, base
    )

    # Archive the original file (e.g. the PDF behind a markitdown `doc`) so the
    # faithful original lives next to its lossy text extraction.
    source_file_rel = ""
    if args.source_file:
        sf = Path(args.source_file).expanduser().resolve()
        if not sf.is_file():
            sys.exit(f"error: --source-file not found: {sf}")
        source_file_rel = archive_source_file(sf, assets_dir, dest_parent, base)

    extra_tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    related = [r.strip() for r in args.related.split(",") if r.strip()]
    if is_note:
        # First-party note: drop the external-source fields (source_url /
        # original_id / publish_time) that don't apply, and default-tag it `note`
        # so it's filterable and the synthesis layer can weight it as the owner's
        # own stance (see schema.md). `related` links it to what it responds to.
        fm = {
            "title": title,
            "source_type": "note",
            "captured_at": captured_at,
            "status": "raw",
            "tags": list(dict.fromkeys([*DEFAULT_TAGS, "note", *extra_tags])),
        }
        if author:
            fm["author"] = author
        if related:
            fm["related"] = related
    else:
        fm = {
            "title": title,
            "source_type": args.source_type,
            "source_url": source_url,
            "original_id": args.original_id,
            "author": author,
            "publish_time": publish_time,
            "captured_at": captured_at,
            "status": "raw",
            "tags": list(dict.fromkeys([*DEFAULT_TAGS, *extra_tags])),
        }
    if source_file_rel:
        fm["source_file"] = source_file_rel
    # For a `video` capture the source URL *is* a video and its content was
    # already captured as a transcript — so a remote video URL appearing in the
    # body (e.g. a youtu.be share link in the description) is expected, not a
    # missing-media gap. Suppress the has_video / video_links / "can't download"
    # machinery for this type; the transcript + source_url already tell the story.
    # (A genuinely localized video asset, has_local_video, is unaffected.)
    is_video = args.source_type == "video"
    if has_video and not (is_video and not has_local_video):
        fm["has_video"] = True
        if videos:
            fm["video_links"] = videos

    warnings = assess_capture(
        new_body, asset_count, is_note=is_note,
        has_source_file=bool(source_file_rel), is_video=is_video,
    )
    if warnings:
        fm["capture_health"] = "warn"

    parts = ["---", _yaml_dump(fm), "---", ""]
    # Only annotate a video when it was NOT localized. If a local video asset is
    # present, the `![video](assets/…)` link in the body already says it all — a
    # "can't download / kept as link" callout there would contradict the file.
    # `video` captures skip this entirely: not downloading the video is the whole
    # point (the transcript is the capture), so a "can't download" note is wrong.
    if has_video and not has_local_video and not is_video:
        if videos:
            parts.append(
                "> [!note] 视频未本地化，以原链接保留（平台限制无法下载）：\n"
                + "\n".join(f"> - {v}" for v in videos)
                + "\n"
            )
        else:
            parts.append(
                "> [!note] 此条含视频，但无法直接下载（如 X 的 blob 源 / 微信腾讯视频 iframe）。"
                "如需本地化，对 X 用 `opencli twitter download --tweet-url <url>` 取 mp4 后引用。\n"
            )
    parts.append(new_body.rstrip() + "\n")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(parts), encoding="utf-8")

    # Persist any health warnings to a per-wiki log so flagged captures are
    # reviewable later (e.g. to decide the skill/adapter needs an update). Lives
    # under .llm-wiki/ which the app's Obsidian config ignores.
    if warnings:
        log_dir = wiki / ".llm-wiki"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "capture-issues.log", "a", encoding="utf-8") as lf:
            lf.write(f"{captured_at}\t{args.source_type}\t{source_url}\t{dest}\t{' | '.join(warnings)}\n")

    summary = {
        "status": "ingested",
        "wiki": str(wiki),
        "dest": str(dest),
        "source_type": args.source_type,
        "title": title,
        "date": date_str,
        "assets": asset_count,
    }
    # For a `video` capture the body's youtube/bilibili links are timestamp deep
    # links (`…&t=NNNs`) into the one source video, not separate un-downloadable
    # videos — reporting them as "kept as link" would be misleading, so omit.
    if not is_video:
        summary["videos_kept_as_link"] = len(videos)
    summary["capture_health"] = "warn" if warnings else "ok"
    if source_file_rel:
        summary["source_file"] = source_file_rel
    if warnings:
        summary["warnings"] = warnings
    print(_yaml_dump(summary))


if __name__ == "__main__":
    main()
