#!/usr/bin/env python3
"""Deterministic caption fetcher — disk-first, context-cheap (video SOP §2 Path A).

Fetches a video's caption track with opencli (drives the logged-in browser,
sidesteps yt-dlp's bot wall) or yt-dlp, lands the raw payload and a normalized
`subs.srt` in the temp dir, and prints only a compact JSON summary. The full
payload never enters the conversation: a measured ad-hoc `opencli youtube
transcript … | head -200` put ~11KB of caption JSON into an agent context and
re-billed it on every later call in that session.

The emitted SRT (or the selected yt-dlp VTT) feeds `srt_to_anchors.py`
unchanged. Remote bytes are data, never executable code: subprocesses run with
argv lists, JSON is parsed in-process.

Usage:
  caption_fetch.py --url <https video URL> --out <temp-dir>
                   [--tool auto|opencli|yt-dlp] [--lang-pref zh-Hans,zh,en]
                   [--cookies-from-browser BROWSER] [--timeout 180]
                   [--from-json <saved opencli cue JSON>]

Exit 0 with status:"ok" (captions on disk), exit 2 with status:"no-captions"
(clean branch to the ASR fallback), exit 1 with status:"error".
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

CORE_SCRIPTS = Path(__file__).resolve().parents[2] / "my-llm-wiki" / "scripts"
sys.path.insert(0, str(CORE_SCRIPTS))
from tool_runtime import ToolRuntimeError, resolve_command_argv  # noqa: E402

_BV = re.compile(r"(BV[0-9A-Za-z]{6,})")
_TS = re.compile(r"^\d{1,2}(:\d{1,2}){1,2}(\.\d+)?$")
_ARROW = "-->"


def die(msg: str, **extra) -> None:
    print(json.dumps({"status": "error", "error": msg, **extra}, ensure_ascii=False))
    sys.exit(1)


def parse_ts(value) -> float | None:
    """Seconds from a cue-start value: number, '12.5', 'M:SS', 'H:MM:SS'."""
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    if not _TS.match(value):
        return None
    seconds = 0.0
    for part in value.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def extract_cues(payload) -> list[tuple[float, str]]:
    """(start_seconds, text) from tolerant JSON shapes.

    Accepts the opencli youtube grouped list ([{timestamp, speaker, text}]),
    Bilibili-style bodies ({body: [{from, to, content}]}), and close cousins
    ({data|transcript|cues|subtitles|items: [...]}).
    """
    if isinstance(payload, dict):
        for key in ("body", "data", "transcript", "cues", "subtitles", "items", "list"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        return []
    cues: list[tuple[float, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("content") or ""
        start = None
        for key in ("timestamp", "start", "from", "begin", "time"):
            if key in item:
                start = parse_ts(item[key])
                if start is not None:
                    break
        if start is not None and isinstance(text, str) and text.strip():
            cues.append((start, " ".join(text.split())))
    return cues


def srt_stamp(seconds: float) -> str:
    ms = round(seconds * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[tuple[float, str]], dest: Path) -> None:
    cues = sorted(cues, key=lambda c: c[0])
    blocks = []
    for i, (start, text) in enumerate(cues):
        end = cues[i + 1][0] if i + 1 < len(cues) and cues[i + 1][0] > start else start + 30.0
        blocks.append(f"{i + 1}\n{srt_stamp(start)} {_ARROW} {srt_stamp(end)}\n{text}\n")
    dest.write_text("\n".join(blocks), encoding="utf-8")


def subs_stats(path: Path) -> dict:
    """Cue count / text chars / first+last starts for a cue file already on disk."""
    starts: list[float] = []
    chars = 0
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").lstrip("﻿").strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        idx = next((j for j, line in enumerate(lines) if _ARROW in line), None)
        if idx is None:
            continue
        start = parse_ts(lines[idx].split(_ARROW)[0].strip().split()[0].replace(",", "."))
        body = re.sub(r"<[^>]+>", "", " ".join(lines[idx + 1:])).strip()
        if start is not None and body:
            starts.append(start)
            chars += len(body)

    def label(seconds: float) -> str:
        total = int(seconds)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    return {
        "cues": len(starts),
        "text_chars": chars,
        "first_anchor": label(min(starts)) if starts else None,
        "last_anchor": label(max(starts)) if starts else None,
    }


def run_argv(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def try_opencli(url: str, host: str, out: Path, timeout: int, warnings: list[str],
                errors: list[str]) -> Path | None:
    """subs path, or None. Tool breakage goes to `errors`, a clean empty result
    ("this video has no captions") only to `warnings` — the caller uses the
    distinction to pick exit 2 (branch to ASR) vs exit 1 (fix the tool/auth)."""
    try:
        prefix = resolve_command_argv("opencli")
    except ToolRuntimeError as exc:
        warnings.append(str(exc))
        return None
    if host == "youtube":
        argv = [*prefix, "youtube", "transcript", url, "--mode", "grouped", "-f", "json"]
    else:
        bvid = _BV.search(url)
        if not bvid:
            warnings.append("no BV id in Bilibili URL; leaving captions to yt-dlp")
            return None
        argv = [*prefix, "bilibili", "subtitle", bvid.group(1), "-f", "json"]
    result = run_argv(argv, timeout)
    raw = result.stdout or ""
    # opencli prints adapter warnings to stderr; stdout should be the JSON payload.
    start = min((i for i in (raw.find("["), raw.find("{")) if i >= 0), default=-1)
    if result.returncode or start < 0:
        tail = "\n".join((result.stderr or raw).strip().splitlines()[-3:])
        errors.append(f"opencli captions failed ({result.returncode}): {tail}")
        return None
    (out / "captions.json").write_text(raw[start:], encoding="utf-8")
    try:
        cues = extract_cues(json.loads(raw[start:]))
    except ValueError:
        errors.append("opencli output is not valid JSON")
        return None
    if not cues:
        warnings.append("opencli returned no cues")
        return None
    dest = out / "subs.srt"
    write_srt(cues, dest)
    return dest


def pick_caption_file(files: list[Path], prefs: list[str]) -> Path:
    for pref in prefs:
        for path in files:
            middle = path.name.split(".")[1:-1]  # subs.<lang>.vtt -> [<lang>]
            if any(token.lower().startswith(pref.lower()) for token in middle):
                return path
    return max(files, key=lambda p: p.stat().st_size)


def try_ytdlp(url: str, out: Path, browser: str | None, timeout: int,
              warnings: list[str], errors: list[str], prefs: list[str]) -> Path | None:
    """Same error-vs-empty contract as try_opencli."""
    try:
        prefix = resolve_command_argv("yt-dlp")
    except ToolRuntimeError as exc:
        warnings.append(str(exc))
        return None
    argv = [*prefix, "--skip-download", "--write-subs", "--write-auto-subs",
            "--sub-langs", "all", "--no-warnings",
            "-o", str(out / "subs.%(ext)s")]
    if browser:
        argv += ["--cookies-from-browser", browser]
    argv.append(url)
    result = run_argv(argv, timeout)
    files = sorted(p for p in out.glob("subs.*")
                   if p.suffix in (".vtt", ".srt") and p.name != "subs.srt")
    if result.returncode and not files:
        tail = "\n".join((result.stderr or result.stdout or "").strip().splitlines()[-3:])
        errors.append(f"yt-dlp captions failed ({result.returncode}): {tail}")
        return None
    if not files:
        warnings.append("yt-dlp found no caption tracks")
        return None
    return pick_caption_file(files, prefs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="video URL to fetch captions for (absolute HTTPS)")
    source.add_argument("--from-json", type=Path,
                        help="convert an already-saved opencli cue JSON instead of fetching")
    parser.add_argument("--out", required=True, type=Path,
                        help="temp dir for the payload and subs (created if missing)")
    parser.add_argument("--tool", choices=("auto", "opencli", "yt-dlp"), default="auto")
    parser.add_argument("--lang-pref", default="zh-Hans,zh-CN,zh,zh-Hant,en",
                        help="comma-separated language preference for multi-track results")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER",
                        help="explicit login-wall fallback, forwarded to yt-dlp")
    parser.add_argument("--timeout", type=int, default=180)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    prefs = [p.strip() for p in args.lang_pref.split(",") if p.strip()]

    if args.from_json:
        try:
            cues = extract_cues(json.loads(args.from_json.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            die(f"cannot parse {args.from_json}: {exc}")
        if not cues:
            print(json.dumps({"status": "no-captions", "tool": "from-json",
                              "warnings": ["no cues in saved JSON"]}, ensure_ascii=False))
            return 2
        subs = out / "subs.srt"
        write_srt(cues, subs)
        print(json.dumps({"status": "ok", "tool": "from-json", "subs": str(subs),
                          **subs_stats(subs), "warnings": warnings}, ensure_ascii=False))
        return 0

    parsed = urlparse(args.url)
    if parsed.scheme != "https" or not parsed.netloc:
        die("video URL must be an absolute HTTPS URL")
    netloc = parsed.netloc.lower()
    host = ("youtube" if any(h in netloc for h in ("youtube.com", "youtu.be"))
            else "bilibili" if "bilibili.com" in netloc
            else "other")

    subs: Path | None = None
    errors: list[str] = []
    tool = None
    attempted = False
    if args.tool in ("auto", "opencli") and host != "other":
        subs = try_opencli(args.url, host, out, args.timeout, warnings, errors)
        attempted = True
        tool = "opencli" if subs else None
    elif args.tool == "opencli":
        warnings.append("opencli caption adapters cover YouTube/Bilibili only")
    if subs is None and args.tool in ("auto", "yt-dlp"):
        subs = try_ytdlp(args.url, out, args.cookies_from_browser,
                         args.timeout, warnings, errors, prefs)
        attempted = True
        tool = "yt-dlp" if subs else None

    if subs is None:
        clean_empty = attempted and any("no caption" in w or "no cues" in w
                                        for w in warnings)
        if clean_empty and not errors:
            # A tool ran fine and found nothing: genuinely captionless video.
            print(json.dumps({"status": "no-captions", "tool": None,
                              "warnings": warnings}, ensure_ascii=False))
            return 2
        # Tools missing or broken (bot wall, auth, bad JSON): captions unknown —
        # fix the tool/auth (e.g. --cookies-from-browser) before falling to ASR.
        die("caption fetch failed before proving the video has no captions "
            "(install yt-dlp / opencli, or retry behind an auth wall with "
            "--cookies-from-browser)", warnings=warnings, errors=errors)

    print(json.dumps({"status": "ok", "tool": tool, "subs": str(subs),
                      **subs_stats(subs), "warnings": warnings}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
