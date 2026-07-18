#!/usr/bin/env python3
"""Probe online-video metadata without piping remote output to an interpreter.

The wrapper invokes yt-dlp with an argv list, parses its JSON in-process, and
emits a small stable record. Remote bytes are data, never executable code.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

CORE_SCRIPTS = Path(__file__).resolve().parents[2] / "my-llm-wiki" / "scripts"
sys.path.insert(0, str(CORE_SCRIPTS))
from tool_runtime import ToolRuntimeError, resolve_command_argv  # noqa: E402


def normalize_metadata(data: Any) -> dict[str, Any]:
    """Return the fields the capture SOP needs from a yt-dlp record."""
    if isinstance(data, list):
        if len(data) != 1 or not isinstance(data[0], dict):
            raise ValueError("expected one video metadata object")
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("yt-dlp returned a non-object metadata payload")

    def languages(value: Any) -> list[str]:
        return sorted(str(key) for key in value) if isinstance(value, dict) else []

    return {
        "id": data.get("id") or "",
        "title": data.get("title") or "",
        "uploader": data.get("uploader") or data.get("channel") or "",
        "channel": data.get("channel") or data.get("uploader") or "",
        "duration": data.get("duration"),
        "upload_date": data.get("upload_date") or "",
        "description": data.get("description") or "",
        "webpage_url": data.get("webpage_url") or data.get("original_url") or "",
        "thumbnail": data.get("thumbnail") or "",
        "subtitle_languages": languages(data.get("subtitles")),
        "automatic_caption_languages": languages(data.get("automatic_captions")),
    }


def load_metadata(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def run_ytdlp(url: str, browser: str | None, timeout: int) -> Any:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("video URL must be an absolute HTTPS URL")
    try:
        prefix = resolve_command_argv("yt-dlp")
    except ToolRuntimeError as exc:
        raise RuntimeError(str(exc)) from exc
    command = [
        *prefix,
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
    ]
    if browser:
        command.extend(["--cookies-from-browser", browser])
    command.append(url)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        tail = "\n".join(detail[-8:])
        raise RuntimeError(f"yt-dlp metadata probe failed ({result.returncode}):\n{tail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"yt-dlp returned invalid JSON: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="online video URL to probe with yt-dlp")
    source.add_argument("--from-json", type=Path, help="parse an existing yt-dlp JSON file")
    parser.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="explicit login-wall fallback; reads that browser's cookies",
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--output", type=Path, help="write normalized JSON to this file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cookies_from_browser and not args.url:
        raise SystemExit("--cookies-from-browser requires --url")
    try:
        raw = (load_metadata(args.from_json) if args.from_json
               else run_ytdlp(args.url, args.cookies_from_browser, args.timeout))
        metadata = normalize_metadata(raw)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"video_probe: {exc}", file=sys.stderr)
        return 1

    rendered = json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
