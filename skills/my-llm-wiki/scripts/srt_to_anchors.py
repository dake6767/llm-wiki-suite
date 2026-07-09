#!/usr/bin/env python3
"""Deterministic SRT/VTT → anchored-transcript assembly (video SOP §3).

Every video capture — whatever produced the cues (official captions via
opencli/yt-dlp, or local ASR via SenseVoice/whisper) — ends with the same
deterministic text transform: parse per-cue timestamps, merge fine-grained
cues into ~30 s chunks, and render each chunk as a bold clickable deep link:

    **[M:SS](<video-url>&t=<sec>s)** …that chunk's speech…

This is the anchor layer that lets RAW answer "where, in which video, was
this said?" (`references/raw-contract.md` → Online video). The rules:

  1. Take each cue's START time; strip inline `<tags>`.
  2. Drop empty cues and consecutive exact duplicates (rolling auto-captions
     repeat lines).
  3. Merge cues into chunks of >= --chunk seconds (default 30) — one anchor
     per caption line is noise.
  4. CJK join rule: joining with spaces corrupts Chinese text — a
     Chinese-dominant chunk is joined with "", everything else with " ".
  5. Label M:SS, or H:MM:SS past the hour. Deep-link format by host:
     YouTube `&t=<sec>s` · Bilibili `?t=<sec>` · anything else `?t=<sec>s`
     best effort (`&` when the URL already has a query).

Pure stdlib, no network, no ASR stack — same input, same output. Usage:

    python3 srt_to_anchors.py subs.srt --url 'https://www.youtube.com/watch?v=ID' > anchored.md
    python3 srt_to_anchors.py subs.vtt --url 'https://www.bilibili.com/video/BV…/' --chunk 30
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

_TAG = re.compile(r"<[^>]+>")            # inline <i>/<c.color>/SenseVoice tags
_CJK = re.compile(r"[一-鿿]")
_LATIN = re.compile(r"[A-Za-z]")
# WEBVTT allows MM:SS.mmm (no hours) and cue settings after the arrow;
# SRT uses HH:MM:SS,mmm. Both: take the arrow line's left side, first token.
_ARROW = "-->"


def _secs(ts: str) -> float:
    parts = [float(x) for x in ts.replace(",", ".").split(":")]
    s = 0.0
    for p in parts:
        s = s * 60 + p
    return s


def _label(t: float) -> str:
    t = int(t)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _deeplink(url: str, sec: float) -> str:
    sep = "&" if "?" in url else "?"
    host = urlparse(url).netloc.lower()
    if "bilibili.com" in host:
        return f"{url}{sep}t={int(sec)}"
    return f"{url}{sep}t={int(sec)}s"    # YouTube form; best effort elsewhere


def parse_cues(text: str) -> list[tuple[float, str]]:
    """(start_seconds, text) per non-empty cue, SRT or VTT."""
    text = text.lstrip("﻿")
    cues: list[tuple[float, str]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        i = next((j for j, l in enumerate(lines) if _ARROW in l), None)
        if i is None:                    # WEBVTT header / NOTE / STYLE blocks
            continue
        start = lines[i].split(_ARROW)[0].strip().split()[0]
        body = _TAG.sub("", " ".join(lines[i + 1:])).strip()
        if body:
            cues.append((_secs(start), body))
    return cues


def _join(parts: list[str]) -> str:
    blob = "".join(parts)
    cjk_dominant = len(_CJK.findall(blob)) > len(_LATIN.findall(" ".join(parts))) * 0.3
    return blob if cjk_dominant else " ".join(parts)


def assemble(cues: list[tuple[float, str]], url: str, chunk_s: float) -> str:
    out: list[str] = []
    cur: list[str] = []
    start: float | None = None
    last: str | None = None

    def flush() -> None:
        if cur and start is not None:
            out.append(f"**[{_label(start)}]({_deeplink(url, start)})** {_join(cur)}")

    for sec, text in cues:
        if text == last:                 # rolling auto-captions repeat lines
            continue
        last = text
        if start is not None and sec - start >= chunk_s:
            flush()
            cur, start = [], None
        if start is None:
            start = sec
        cur.append(text)
    flush()
    return "\n\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("subs", help="input .srt or .vtt file")
    ap.add_argument("--url", required=True, help="canonical video URL for deep links")
    ap.add_argument("--chunk", type=float, default=30.0, help="min chunk length in seconds (default 30)")
    args = ap.parse_args()

    cues = parse_cues(Path(args.subs).read_text(encoding="utf-8"))
    if not cues:
        sys.exit(f"srt_to_anchors: no cues found in {args.subs}")
    print(assemble(cues, args.url, args.chunk))


if __name__ == "__main__":
    main()
