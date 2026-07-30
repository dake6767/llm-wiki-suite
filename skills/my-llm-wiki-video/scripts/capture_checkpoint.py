#!/usr/bin/env python3
"""Inspect a video temp workspace and select the next resumable capture stage."""

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
    parse_flat_yaml,
    validate_asr_wav,
    validate_cover,
    validate_srt,
    validate_status_identity,
    validate_transcript,
)


def inspect_capture(
    workdir: Path, *, source_url: str = "", original_id: str = ""
) -> tuple[int, dict[str, Any]]:
    workdir = workdir.resolve()
    if not workdir.is_dir():
        raise CaptureStateError(f"capture workspace does not exist: {workdir}")

    status_path = workdir / "status.yaml"
    status = parse_flat_yaml(status_path)
    if status.get("status") == "error":
        return 2, {
            "stage": "asr-error",
            "next_action": "fix the reported error; retain audio and SRT",
            "error": status.get("error") or "unknown ASR error",
        }
    if status:
        validate_status_identity(
            status, source_url=source_url, original_id=original_id
        )

    marker = workdir / ".capture-commit.json"
    if marker.is_file():
        try:
            checkpoint = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CaptureStateError(f"invalid commit checkpoint: {exc}") from exc
        raw = Path(str(checkpoint.get("raw_dest") or ""))
        if checkpoint.get("status") == "committed" and raw.is_file():
            return 0, {
                "stage": "complete",
                "next_action": "reuse committed RAW",
                "raw_dest": str(raw),
            }
        if raw.is_file():
            return 0, {
                "stage": "normalized-needs-cleanup",
                "next_action": "rerun commit_capture.py; do not normalize again",
                "raw_dest": str(raw),
            }

    transcript = workdir / "transcript.md"
    if transcript.exists():
        details = validate_transcript(transcript, source_url=source_url)
        validate_cover(workdir)
        return 0, {
            "stage": "ready-to-commit",
            "next_action": "run commit_capture.py; do not download or rerun ASR",
            **details,
        }

    anchored = workdir / "anchored.md"
    anchored_links = (
        anchors(anchored.read_text(encoding="utf-8")) if anchored.is_file() else []
    )
    if anchored_links:
        if source_url and any(
            not link.startswith(source_url) for _, link in anchored_links
        ):
            raise CaptureStateError("anchored transcript belongs to a different video")
        validate_cover(workdir)
        return 0, {
            "stage": "ready-to-assemble",
            "next_action": "run assemble_transcript.py; do not download or rerun ASR",
        }

    captions = workdir / "subs.srt"
    if captions.is_file():
        validate_srt(captions)
        return 0, {
            "stage": "captions-ready",
            "next_action": "run srt_to_anchors.py; do not fetch captions again",
            "subs": str(captions),
        }

    transcript_srt = workdir / "transcript.srt"
    if transcript_srt.is_file() or status_path.is_file():
        if not status:
            return 2, {
                "stage": "asr-incomplete",
                "next_action": "wait/reap the existing ASR process; do not redownload",
            }
        validate_srt(transcript_srt)
        return 0, {
            "stage": "asr-ready",
            "next_action": "run srt_to_anchors.py; do not download or rerun ASR",
            "subs": str(transcript_srt),
            "cues": status.get("transcript_cues"),
        }

    wav = workdir / "audio.wav"
    invalid_wav = ""
    if wav.is_file():
        try:
            details = validate_asr_wav(wav)
        except CaptureStateError as exc:
            invalid_wav = str(exc)
        else:
            return 0, {
                "stage": "wav-ready",
                "next_action": "run SenseVoice; do not download or convert again",
                "audio": str(wav),
                **details,
            }

    audio_files = sorted(
        path
        for path in workdir.glob("audio.*")
        if path.is_file()
        and path != wav
        and path.stat().st_size
        and path.suffix != ".part"
    )
    if audio_files:
        return 0, {
            "stage": "audio-ready",
            "next_action": "reuse this audio; convert with audio_to_wav.py only if needed",
            "audio": str(audio_files[0]),
        }
    if invalid_wav:
        return 2, {
            "stage": "invalid-wav",
            "next_action": "retain it for diagnosis and reconvert from downloaded audio",
            "error": invalid_wav,
        }
    return 0, {
        "stage": "download-needed",
        "next_action": "download one audio-only stream",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workdir", type=native_path)
    parser.add_argument("--source-url", default="")
    parser.add_argument("--original-id", default="")
    args = parser.parse_args(argv)
    try:
        code, result = inspect_capture(
            args.workdir, source_url=args.source_url, original_id=args.original_id
        )
    except (CaptureStateError, OSError) as exc:
        print(
            json.dumps(
                {"stage": "error", "error": str(exc)}, ensure_ascii=False
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
