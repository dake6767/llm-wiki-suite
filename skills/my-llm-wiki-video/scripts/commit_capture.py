#!/usr/bin/env python3
"""Normalize one verified video capture, verify RAW, then clean staged media."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

CORE_SCRIPTS = Path(__file__).resolve().parents[2] / "my-llm-wiki" / "scripts"
DEFAULT_NORMALIZER = CORE_SCRIPTS / "normalize_raw.py"
sys.path.insert(0, str(CORE_SCRIPTS))
from path_compat import native_path  # noqa: E402

from capture_state import (  # noqa: E402
    CaptureStateError,
    atomic_write_json,
    media_cleanup_candidates,
    parse_flat_yaml,
    parse_summary,
    validate_cover,
    validate_raw,
    validate_status_identity,
    validate_transcript,
)


def _load_marker(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CaptureStateError(f"invalid commit checkpoint {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CaptureStateError(f"invalid commit checkpoint {path}")
    return value


def _cleanup(workdir: Path) -> list[str]:
    candidates = media_cleanup_candidates(workdir)
    for path in candidates:
        if not (path.is_file() or path.is_symlink()):
            raise CaptureStateError(f"refusing to clean non-file path: {path}")
        if path.parent.resolve() != workdir.resolve():
            raise CaptureStateError(f"refusing to clean outside the workspace: {path}")
    cleaned: list[str] = []
    for path in candidates:
        path.unlink()
        cleaned.append(path.name)
    return cleaned


def _finish_cleanup(
    *, workdir: Path, marker_path: Path, checkpoint: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    try:
        cleaned = _cleanup(workdir)
        committed = {
            **checkpoint,
            "status": "committed",
            "cleaned": cleaned,
            "committed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        atomic_write_json(marker_path, committed)
    except (CaptureStateError, OSError) as exc:
        failed = {**checkpoint, "status": "cleanup-error", "error": str(exc)}
        try:
            atomic_write_json(marker_path, failed)
        except (CaptureStateError, OSError):
            pass
        return 4, failed
    return 0, committed


def commit(
    *,
    workdir: Path,
    wiki: str,
    title: str,
    source_url: str,
    original_id: str,
    author: str = "",
    publish_time: str = "",
    captured_at: str = "",
    on_exists: str = "skip",
    require_timestamps: bool = True,
    normalizer: Path = DEFAULT_NORMALIZER,
) -> tuple[int, dict[str, Any]]:
    workdir = workdir.resolve()
    transcript = workdir / "transcript.md"
    marker_path = workdir / ".capture-commit.json"
    marker = _load_marker(marker_path)
    if marker:
        if marker.get("source_url") != source_url or marker.get("original_id") != original_id:
            raise CaptureStateError("commit checkpoint belongs to a different video")
        raw_dest = Path(str(marker.get("raw_dest") or ""))
        if raw_dest.is_file():
            raw_details = validate_raw(
                raw_dest, source_url=source_url, require_timestamps=require_timestamps
            )
            if raw_details["capture_health"] != "ok":
                return 3, {
                    **marker,
                    "status": "normalized-warning",
                    "cleanup": "retained",
                }
            if marker.get("status") == "committed":
                return 0, {**marker, "status": "reused"}
            return _finish_cleanup(
                workdir=workdir, marker_path=marker_path, checkpoint=marker
            )

    status = parse_flat_yaml(workdir / "status.yaml")
    if status:
        validate_status_identity(
            status, source_url=source_url, original_id=original_id
        )
    staged = validate_transcript(
        transcript,
        require_timestamps=require_timestamps,
        source_url=source_url,
    )
    validate_cover(workdir)
    if title and staged["title"] != title:
        raise CaptureStateError(
            f"transcript title mismatch: expected {title!r}, got {staged['title']!r}"
        )
    if not normalizer.is_file():
        raise CaptureStateError(f"normalize_raw.py not found: {normalizer}")
    command = [
        sys.executable,
        str(normalizer),
        "--from",
        str(workdir),
        "--wiki",
        wiki,
        "--source-type",
        "video",
        "--title",
        title,
        "--source-url",
        source_url,
        "--original-id",
        original_id,
        "--on-exists",
        on_exists,
    ]
    if author:
        command.extend(["--author", author])
    if publish_time:
        command.extend(["--publish-time", publish_time])
    if captured_at:
        command.extend(["--captured-at", captured_at])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise CaptureStateError(
            f"normalize_raw.py failed ({result.returncode}); staged media retained:\n"
            f"{detail}"
        )
    summary = parse_summary(result.stdout)
    if summary.get("status") not in {"ingested", "skipped_exists"}:
        raise CaptureStateError(
            "normalize_raw.py returned success without an accepted status; "
            "staged media retained"
        )
    raw_dest = Path(str(summary.get("dest") or "")).resolve()
    raw_details = validate_raw(
        raw_dest, source_url=source_url, require_timestamps=require_timestamps
    )
    if raw_details["capture_health"] != "ok":
        return 3, {
            "status": "normalized-warning",
            "raw_dest": str(raw_dest),
            "capture_health": raw_details["capture_health"],
            "cleanup": "retained",
        }
    checkpoint = {
        "status": "normalized",
        "source_url": source_url,
        "original_id": original_id,
        "raw_dest": str(raw_dest),
        "normalizer_status": summary["status"],
        "capture_health": "ok",
        "normalized_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    atomic_write_json(marker_path, checkpoint)
    return _finish_cleanup(
        workdir=workdir, marker_path=marker_path, checkpoint=checkpoint
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=native_path, required=True)
    parser.add_argument("--wiki", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--original-id", required=True)
    parser.add_argument("--author", default="")
    parser.add_argument("--publish-time", default="")
    parser.add_argument("--captured-at", default="")
    parser.add_argument(
        "--on-exists", choices=["skip", "fail", "version"], default="skip"
    )
    parser.add_argument(
        "--allow-no-timestamps",
        action="store_true",
        help="only after the user explicitly accepted a degraded transcript",
    )
    args = parser.parse_args(argv)
    try:
        code, result = commit(
            workdir=args.workdir,
            wiki=args.wiki,
            title=args.title,
            source_url=args.source_url,
            original_id=args.original_id,
            author=args.author,
            publish_time=args.publish_time,
            captured_at=args.captured_at,
            on_exists=args.on_exists,
            require_timestamps=not args.allow_no_timestamps,
        )
    except (CaptureStateError, OSError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": str(exc),
                    "cleanup": "retained",
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
