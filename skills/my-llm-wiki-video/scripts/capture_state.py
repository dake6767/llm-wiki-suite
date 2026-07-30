#!/usr/bin/env python3
"""Shared persistence and validation helpers for the video capture transaction."""

from __future__ import annotations

import json
import os
import re
import tempfile
import wave
from pathlib import Path
from typing import Any


ANCHOR_RE = re.compile(r"\*\*\[([^\]]+)\]\((https://[^)]+)\)\*\*")
IMAGE_RE = re.compile(r"!\[[^\]]*]\(([^)]+)\)")
SRT_ARROW_RE = re.compile(
    r"(?m)^\s*\d{1,2}:\d{2}(?::\d{2})?[,.]\d{3}\s+-->\s+"
    r"\d{1,2}:\d{2}(?::\d{2})?[,.]\d{3}"
)


class CaptureStateError(RuntimeError):
    """The staged capture is incomplete, inconsistent, or unsafe to commit."""


def _sync_directory(path: Path) -> None:
    """Best-effort directory fsync after an atomic replace."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, text: str) -> None:
    """Durably replace one text file and verify the bytes by reading them back."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
        if path.read_text(encoding="utf-8") != text:
            raise CaptureStateError(f"write verification failed for {path}")
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def parse_flat_yaml(path: Path) -> dict[str, Any]:
    """Parse the scalar-only YAML emitted by the shipped ASR runners."""
    values: dict[str, Any] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        raw = raw.strip()
        if not key.strip():
            continue
        try:
            values[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            values[key.strip()] = raw
    return values


def parse_summary(text: str) -> dict[str, Any]:
    """Parse normalize_raw.py's compact top-level YAML summary."""
    values: dict[str, Any] = {}
    for line in text.splitlines():
        if not line or line[0].isspace() or ": " not in line:
            continue
        key, raw = line.split(": ", 1)
        raw = raw.strip()
        try:
            values[key] = json.loads(raw)
        except json.JSONDecodeError:
            values[key] = raw
    return values


def anchors(text: str) -> list[tuple[str, str]]:
    return ANCHOR_RE.findall(text)


def validate_srt(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise CaptureStateError(f"subtitle output is missing or empty: {path}")
    text = path.read_text(encoding="utf-8")
    if not SRT_ARROW_RE.search(text):
        raise CaptureStateError(f"subtitle output has no valid timestamp cues: {path}")


def validate_transcript(
    path: Path,
    *,
    require_timestamps: bool = True,
    source_url: str = "",
) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise CaptureStateError(f"transcript is missing or empty: {path}")
    text = path.read_text(encoding="utf-8")
    first = text.splitlines()[0].strip() if text.splitlines() else ""
    if not first.startswith("# ") or not first[2:].strip():
        raise CaptureStateError("transcript.md must start with a meaningful H1")
    required = (
        "> 原文链接:",
        "![封面](images/cover.jpg)",
        "## 文字转写",
    )
    missing = [item for item in required if item not in text]
    if missing:
        raise CaptureStateError(
            "transcript.md is missing required structure: " + ", ".join(missing)
        )
    found_anchors = anchors(text)
    if require_timestamps and not found_anchors:
        raise CaptureStateError("transcript.md has no timestamp anchors")
    if source_url:
        if f"> 原文链接: {source_url}" not in text:
            raise CaptureStateError("transcript.md source URL does not match the capture")
        if any(not link.startswith(source_url) for _, link in found_anchors):
            raise CaptureStateError(
                "transcript.md contains an anchor for a different video"
            )
    return {
        "title": first[2:].strip(),
        "anchors": len(found_anchors),
        "chars": len(text),
    }


def validate_cover(workdir: Path) -> Path:
    cover = workdir / "images" / "cover.jpg"
    if not cover.is_file() or cover.stat().st_size == 0:
        raise CaptureStateError(f"localized cover is missing or empty: {cover}")
    return cover


def wav_info(path: Path) -> dict[str, int]:
    try:
        with wave.open(str(path), "rb") as audio:
            return {
                "channels": audio.getnchannels(),
                "sample_rate": audio.getframerate(),
                "sample_width": audio.getsampwidth(),
                "frames": audio.getnframes(),
            }
    except (OSError, EOFError, wave.Error) as exc:
        raise CaptureStateError(f"invalid WAV output {path}: {exc}") from exc


def validate_asr_wav(path: Path) -> dict[str, int]:
    info = wav_info(path)
    if info["channels"] != 1:
        raise CaptureStateError(
            f"SenseVoice WAV must be mono; got {info['channels']} channels"
        )
    if info["sample_rate"] != 16_000:
        raise CaptureStateError(
            f"SenseVoice WAV must be 16000 Hz; got {info['sample_rate']} Hz"
        )
    if info["frames"] <= 0:
        raise CaptureStateError("SenseVoice WAV contains no audio frames")
    return info


def validate_status_identity(
    status: dict[str, Any], *, source_url: str = "", original_id: str = ""
) -> None:
    if status.get("status") != "ok":
        detail = status.get("error") or status.get("status") or "missing"
        raise CaptureStateError(f"ASR status is not ok: {detail}")
    expected = (("source_url", source_url), ("original_id", original_id))
    for key, value in expected:
        recorded = str(status.get(key) or "")
        if value and recorded != value:
            raise CaptureStateError(
                f"ASR checkpoint {key} mismatch: expected {value!r}, got {recorded!r}"
            )


def validate_raw(
    path: Path, *, source_url: str, require_timestamps: bool = True
) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise CaptureStateError(f"normalized RAW is missing or empty: {path}")
    text = path.read_text(encoding="utf-8")
    if "source_type: video" not in text:
        raise CaptureStateError("normalized RAW is not source_type: video")
    if source_url and source_url not in text:
        raise CaptureStateError("normalized RAW does not contain the source URL")
    found_anchors = anchors(text)
    if require_timestamps and not found_anchors:
        raise CaptureStateError("normalized RAW has no timestamp anchors")
    if source_url and any(not link.startswith(source_url) for _, link in found_anchors):
        raise CaptureStateError("normalized RAW contains an anchor for a different video")
    local_assets: list[Path] = []
    for target in IMAGE_RE.findall(text):
        if target.startswith(("http://", "https://", "data:")):
            continue
        candidate = (path.parent / target).resolve()
        if candidate.is_file() and candidate.stat().st_size:
            local_assets.append(candidate)
    if not local_assets:
        raise CaptureStateError("normalized RAW has no readable localized cover")
    return {
        "capture_health": "warn" if "capture_health: warn" in text else "ok",
        "anchors": len(found_anchors),
        "assets": len(local_assets),
    }


def media_cleanup_candidates(workdir: Path) -> list[Path]:
    """Return exact, top-level ephemeral media/intermediate files only."""
    names = {"transcript.srt", "subs.srt", "anchored.md", "captions.json"}
    candidates = {workdir / name for name in names}
    for pattern in ("audio.*", "video.*", "candidate-audio-*"):
        candidates.update(workdir.glob(pattern))
    return sorted(path for path in candidates if path.exists())
