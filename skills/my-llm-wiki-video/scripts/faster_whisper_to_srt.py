#!/usr/bin/env python3
"""Run faster-whisper ASR and atomically write timestamped SRT + status.

faster-whisper (github.com/SYSTRAN/faster-whisper) is a Python library with no
CLI; this is the shipped runner the video SOP invokes for non-Chinese audio.
Chinese audio routes to sensevoice_to_srt.py instead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


def srt_timestamp(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def write_status(path: Path, values: dict[str, Any]) -> None:
    body = "\n".join(f"{key}: {yaml_scalar(value)}" for key, value in values.items()) + "\n"
    atomic_write(path, body)


def transcribe(
    audio: Path,
    output: Path,
    model_name: str,
    language: str,
    initial_prompt: str,
    device: str,
    compute_type: str,
) -> tuple[int, int, str]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is missing; run this with the configured ASR venv"
        ) from exc

    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        str(audio),
        language=language or None,
        initial_prompt=initial_prompt or None,
        vad_filter=True,
    )

    cues: list[tuple[float, float, str]] = []
    for index, segment in enumerate(segments, start=1):
        text = segment.text.strip()
        if not text:
            continue
        cues.append((segment.start, segment.end, text))
        print(
            f"Processed segment {index} (audio time {srt_timestamp(segment.end)})",
            flush=True,
        )

    if not cues:
        raise RuntimeError("faster-whisper produced no non-empty speech cues")
    body = "\n".join(
        f"{index}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}\n"
        for index, (start, end, text) in enumerate(cues, start=1)
    )
    atomic_write(output, body)
    chars = sum(len(text) for _, _, text in cues)
    return len(cues), chars, info.language or (language or "auto")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--model", default="medium")
    parser.add_argument(
        "--language",
        default="",
        help="ISO 639-1 code; empty = auto-detect",
    )
    parser.add_argument(
        "--initial-prompt",
        default="",
        help="decoder priming text: video title + keywords + first description line",
    )
    parser.add_argument("--device", default="auto")
    parser.add_argument("--compute-type", default="auto")
    parser.add_argument("--source-url", default="")
    parser.add_argument("--original-id", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.audio.is_file():
        print(f"faster_whisper_to_srt: audio not found: {args.audio}", file=sys.stderr)
        return 2
    try:
        cues, chars, detected = transcribe(
            args.audio,
            args.output,
            args.model,
            args.language,
            args.initial_prompt,
            args.device,
            args.compute_type,
        )
        write_status(args.status, {
            "status": "ok",
            "source_url": args.source_url,
            "original_id": args.original_id,
            "transcript_source": f"faster-whisper/{args.model}({detected})",
            "transcript_cues": cues,
            "transcript_chars": chars,
        })
    except Exception as exc:  # surface model/audio failures and leave an atomic signal
        write_status(args.status, {
            "status": "error",
            "source_url": args.source_url,
            "original_id": args.original_id,
            "error": str(exc),
        })
        print(f"faster_whisper_to_srt: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
