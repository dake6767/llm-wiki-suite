#!/usr/bin/env python3
"""Run VAD-first SenseVoice ASR and atomically write timestamped SRT + status.

This is the vetted implementation of the recipe that used to be copied from a
Markdown code block into a temporary Python file on every capture.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

CORE_SCRIPTS = Path(__file__).resolve().parents[2] / "my-llm-wiki" / "scripts"
sys.path.insert(0, str(CORE_SCRIPTS))
from tool_runtime import ensure_provider_python  # noqa: E402


# Keep Unicode controls escaped in source so command/security scanners never
# see literal variation-selector characters in generated shell text.
RICH_MARKERS = re.compile("[\\U0001F000-\\U0001FAFF\\u2600-\\u27BF\\uFE0F]")


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
    language: str,
    max_segment_ms: int,
) -> tuple[int, int]:
    print("sensevoice_to_srt: loading managed dependencies", flush=True)
    try:
        import soundfile
        from funasr import AutoModel
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
    except ImportError as exc:
        raise RuntimeError(
            "SenseVoice dependencies are missing; run this with the configured ASR venv"
        ) from exc

    print("sensevoice_to_srt: decoding 16 kHz mono audio", flush=True)
    wav, sample_rate = soundfile.read(
        str(audio),
        dtype="float32",
        always_2d=False,
    )
    if sample_rate != 16_000:
        raise RuntimeError(
            f"SenseVoice requires 16 kHz WAV input; got {sample_rate} Hz"
        )
    if getattr(wav, "ndim", 1) != 1:
        raise RuntimeError("SenseVoice requires mono WAV input")
    print("sensevoice_to_srt: loading fsmn-vad", flush=True)
    vad = AutoModel(
        model="fsmn-vad",
        max_single_segment_time=max_segment_ms,
        disable_update=True,
    )
    generated = vad.generate(input=str(audio)) or [{}]
    spans = generated[0].get("value") or [[0, len(wav) // 16]]
    print(f"sensevoice_to_srt: VAD found {len(spans)} segments", flush=True)
    print("sensevoice_to_srt: loading SenseVoiceSmall", flush=True)
    recognizer = AutoModel(model="iic/SenseVoiceSmall", disable_update=True)

    cues: list[tuple[float, float, str]] = []
    for index, span in enumerate(spans, start=1):
        if not isinstance(span, (list, tuple)) or len(span) != 2:
            continue
        start_ms, end_ms = int(span[0]), int(span[1])
        chunk = wav[start_ms * 16:end_ms * 16]
        if len(chunk) < 160:
            continue
        result = recognizer.generate(
            input=chunk,
            cache={},
            language=language,
            use_itn=True,
        )
        text = ""
        if result:
            text = rich_transcription_postprocess(result[0].get("text", ""))
            text = RICH_MARKERS.sub("", text).strip()
        if text:
            cues.append((start_ms / 1000, end_ms / 1000, text))
        print(f"Processed {index}/{len(spans)} segments", flush=True)

    if not cues:
        raise RuntimeError("SenseVoice produced no non-empty speech cues")
    body = "\n".join(
        f"{index}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}\n"
        for index, (start, end, text) in enumerate(cues, start=1)
    )
    atomic_write(output, body)
    return len(cues), sum(len(text) for _, _, text in cues)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument(
        "--language",
        default="zh",
        choices=["zh", "en", "yue", "ja", "ko", "auto"],
    )
    parser.add_argument("--source-url", default="")
    parser.add_argument("--original-id", default="")
    parser.add_argument("--max-segment-ms", type=int, default=30_000)
    parser.add_argument("--provider", help="explicit Provider id for this task only")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_provider_python("asr-zh", provider=args.provider)
    if not args.audio.is_file():
        print(f"sensevoice_to_srt: audio not found: {args.audio}", file=sys.stderr)
        return 2
    try:
        cues, chars = transcribe(
            args.audio,
            args.output,
            args.language,
            args.max_segment_ms,
        )
        write_status(args.status, {
            "status": "ok",
            "source_url": args.source_url,
            "original_id": args.original_id,
            "transcript_source": f"sensevoice({args.language})",
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
        print(f"sensevoice_to_srt: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
