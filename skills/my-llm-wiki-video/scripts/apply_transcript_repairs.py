#!/usr/bin/env python3
"""Apply exact model-authored text repairs atomically without moving any anchors."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

CORE_SCRIPTS = Path(__file__).resolve().parents[2] / "my-llm-wiki" / "scripts"
sys.path.insert(0, str(CORE_SCRIPTS))
from path_compat import native_path  # noqa: E402

from capture_state import (  # noqa: E402
    CaptureStateError,
    anchors,
    atomic_write_text,
    validate_transcript,
)


MAX_PLAN_BYTES = 2 * 1024 * 1024


def load_plan(path: Path) -> list[dict[str, Any]]:
    if path.stat().st_size > MAX_PLAN_BYTES:
        raise CaptureStateError("repair plan exceeds the 2 MiB safety limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureStateError(f"cannot read repair plan {path}: {exc}") from exc
    if isinstance(value, dict):
        value = value.get("replacements")
    if not isinstance(value, list):
        raise CaptureStateError("repair plan must be a list or {replacements: [...]}")
    for index, item in enumerate(value):
        if not isinstance(item, dict) or not isinstance(item.get("old"), str):
            raise CaptureStateError(f"repair {index} must contain string old/new values")
        if not isinstance(item.get("new"), str) or not item["old"]:
            raise CaptureStateError(f"repair {index} must contain string old/new values")
        count = item.get("count", 1)
        if not isinstance(count, int) or count < 1:
            raise CaptureStateError(f"repair {index} count must be a positive integer")
    return value


def apply_repairs(
    transcript: Path,
    plan: Path | None = None,
    translation_file: Path | None = None,
) -> dict[str, Any]:
    validate_transcript(transcript)
    original = transcript.read_text(encoding="utf-8")
    original_anchors = anchors(original)
    repaired = original
    replacements = load_plan(plan) if plan else []
    for index, item in enumerate(replacements):
        old = item["old"]
        new = item["new"]
        expected = item.get("count", 1)
        if anchors(old) or anchors(new):
            raise CaptureStateError(
                f"repair {index} contains a timestamp anchor; anchors are immutable"
            )
        actual = repaired.count(old)
        if actual != expected:
            raise CaptureStateError(
                f"repair {index} expected {expected} exact match(es), found {actual}"
            )
        repaired = repaired.replace(old, new, expected)
    if anchors(repaired) != original_anchors:
        raise CaptureStateError("repair plan changed or moved timestamp anchors")
    if translation_file:
        if "\n## 中文译文\n" in repaired:
            raise CaptureStateError("transcript already contains a Chinese translation")
        translation = translation_file.read_text(encoding="utf-8").strip()
        if translation.startswith("## 中文译文"):
            translation = translation.removeprefix("## 中文译文").strip()
        if anchors(translation) != original_anchors:
            raise CaptureStateError(
                "Chinese translation must preserve the same anchors in the same order"
            )
        repaired = repaired.rstrip() + "\n\n## 中文译文\n\n" + translation + "\n"
    if repaired == original:
        return {"status": "unchanged", "repairs": len(replacements)}
    atomic_write_text(transcript, repaired)
    verified = validate_transcript(transcript)
    return {"status": "repaired", "repairs": len(replacements), **verified}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=native_path)
    parser.add_argument("--plan", type=native_path)
    parser.add_argument("--translation-file", type=native_path)
    args = parser.parse_args(argv)
    if not args.plan and not args.translation_file:
        parser.error("at least one of --plan or --translation-file is required")
    try:
        result = apply_repairs(
            args.transcript.resolve(),
            args.plan.resolve() if args.plan else None,
            args.translation_file.resolve() if args.translation_file else None,
        )
    except (CaptureStateError, OSError) as exc:
        print(f"apply_transcript_repairs: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
