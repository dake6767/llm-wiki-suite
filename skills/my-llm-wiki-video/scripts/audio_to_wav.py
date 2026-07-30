#!/usr/bin/env python3
"""Convert one downloaded audio file to an atomic 16 kHz mono PCM WAV."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parents[2] / "my-llm-wiki" / "scripts"
sys.path.insert(0, str(CORE_SCRIPTS))
from path_compat import native_path  # noqa: E402
from tool_runtime import ToolRuntimeError, resolve_command_argv  # noqa: E402

from capture_state import CaptureStateError, validate_asr_wav  # noqa: E402


def convert(
    source: Path, output: Path, *, provider: str | None = None
) -> dict[str, object]:
    if not source.is_file() or source.stat().st_size == 0:
        raise CaptureStateError(f"downloaded audio is missing or empty: {source}")
    if output.is_file():
        try:
            details = validate_asr_wav(output)
        except CaptureStateError:
            pass
        else:
            return {"status": "reused", "output": str(output), **details}
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=".wav", dir=output.parent
    )
    os.close(descriptor)
    os.unlink(temporary_name)
    temporary = Path(temporary_name)
    try:
        try:
            prefix = resolve_command_argv(
                "ffmpeg", capability="media.extract-audio", provider=provider
            )
        except ToolRuntimeError as exc:
            raise CaptureStateError(str(exc)) from exc
        command = [
            *prefix,
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(temporary),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise CaptureStateError(
                f"ffmpeg audio conversion failed ({result.returncode}): {detail}"
            )
        details = validate_asr_wav(temporary)
        os.replace(temporary, output)
        details = validate_asr_wav(output)
        return {"status": "converted", "output": str(output), **details}
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=native_path)
    parser.add_argument("output", type=native_path)
    parser.add_argument("--provider", help="explicit FFmpeg Provider id")
    args = parser.parse_args(argv)
    try:
        result = convert(
            args.source.resolve(), args.output.resolve(), provider=args.provider
        )
    except (CaptureStateError, OSError) as exc:
        print(f"audio_to_wav: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
