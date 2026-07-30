#!/usr/bin/env python3
"""Atomically assemble the accepted video transcript shape from anchored cues."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

CORE_SCRIPTS = Path(__file__).resolve().parents[2] / "my-llm-wiki" / "scripts"
sys.path.insert(0, str(CORE_SCRIPTS))
from path_compat import native_path  # noqa: E402

from capture_state import (  # noqa: E402
    CaptureStateError,
    anchors,
    atomic_write_text,
    parse_flat_yaml,
    validate_cover,
    validate_transcript,
)


GENERIC_TITLES = {"transcript", "anchored", "audio", "video", "untitled"}


def _one_line(value: Any, fallback: str = "-") -> str:
    rendered = re.sub(r"\s+", " ", str(value or "")).strip()
    return rendered or fallback


def _duration(value: Any) -> str:
    try:
        seconds = max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return "时长未知"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (
        f"{hours}:{minutes:02d}:{seconds:02d}"
        if hours
        else f"{minutes}:{seconds:02d}"
    )


def _metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureStateError(f"cannot read metadata JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CaptureStateError(f"metadata JSON must contain one object: {path}")
    return value


def build_transcript(
    *,
    workdir: Path,
    anchored_path: Path,
    output: Path,
    metadata_path: Path,
    title: str = "",
    author: str = "",
    publish_time: str = "",
    source_url: str = "",
    transcript_source: str = "",
    description_file: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    workdir = workdir.resolve()
    validate_cover(workdir)
    metadata = _metadata(metadata_path)
    status = parse_flat_yaml(workdir / "status.yaml")
    if status and status.get("status") == "error":
        raise CaptureStateError(f"ASR failed: {status.get('error') or 'unknown error'}")

    resolved_title = _one_line(title or metadata.get("title"), "")
    if not resolved_title or resolved_title.lower() in GENERIC_TITLES:
        raise CaptureStateError("a meaningful video title is required")
    resolved_url = _one_line(
        source_url or metadata.get("webpage_url") or status.get("source_url"), ""
    )
    parsed_url = urlparse(resolved_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise CaptureStateError("source URL must be an absolute HTTPS URL")
    if status.get("source_url") and status["source_url"] != resolved_url:
        raise CaptureStateError("metadata/source URL does not match the ASR checkpoint")

    if output.exists() and not force:
        existing = validate_transcript(output, source_url=resolved_url)
        return {"status": "reused", "output": str(output), **existing}

    if not anchored_path.is_file():
        raise CaptureStateError(f"anchored transcript not found: {anchored_path}")
    anchored = anchored_path.read_text(encoding="utf-8").strip()
    found_anchors = anchors(anchored)
    if not found_anchors:
        raise CaptureStateError("anchored transcript contains no timestamp anchors")
    if any(not link.startswith(resolved_url) for _, link in found_anchors):
        raise CaptureStateError("an anchor points to a different source video")

    if description_file:
        description = description_file.read_text(encoding="utf-8").strip()
    else:
        description = str(metadata.get("description") or "").strip()
    description = description or "（原视频未提供简介）"
    source_label = _one_line(
        transcript_source or status.get("transcript_source"), "未知"
    )
    rendered = (
        f"# {resolved_title}\n\n"
        f"> 作者: {_one_line(author or metadata.get('uploader') or metadata.get('channel'))}\n"
        f"> 发布时间: {_one_line(publish_time or metadata.get('upload_date'))}\n"
        f"> 原文链接: {resolved_url}\n\n"
        "![封面](images/cover.jpg)\n\n"
        f"*时长 {_duration(metadata.get('duration'))} · 转写来源：{source_label} · "
        "含可跳转时间戳*\n\n"
        "## 简介\n\n"
        f"{description}\n\n"
        "## 文字转写\n\n"
        f"{anchored}\n"
    )
    atomic_write_text(output, rendered)
    verified = validate_transcript(output, source_url=resolved_url)
    return {"status": "assembled", "output": str(output), **verified}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=native_path, required=True)
    parser.add_argument("--anchored", type=native_path)
    parser.add_argument("--output", type=native_path)
    parser.add_argument("--metadata", type=native_path)
    parser.add_argument("--title", default="")
    parser.add_argument("--author", default="")
    parser.add_argument("--publish-time", default="")
    parser.add_argument("--source-url", default="")
    parser.add_argument("--transcript-source", default="")
    parser.add_argument("--description-file", type=native_path)
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing transcript.md (default is to validate and reuse it)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workdir = args.workdir.resolve()
    try:
        result = build_transcript(
            workdir=workdir,
            anchored_path=(args.anchored or workdir / "anchored.md").resolve(),
            output=(args.output or workdir / "transcript.md").resolve(),
            metadata_path=(args.metadata or workdir / "metadata.json").resolve(),
            title=args.title,
            author=args.author,
            publish_time=args.publish_time,
            source_url=args.source_url,
            transcript_source=args.transcript_source,
            description_file=args.description_file,
            force=args.force,
        )
    except (CaptureStateError, OSError) as exc:
        print(f"assemble_transcript: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
