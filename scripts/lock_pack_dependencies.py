#!/usr/bin/env python3
"""Resolve every Python runtime pack to platform-specific hashed requirements."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
POSIX_LOCK = ROOT / "registry" / "pack-build-posix.lock.json"
WINDOWS_LOCK = ROOT / "registry" / "pack-build-windows.lock.json"
OUTPUT = ROOT / "registry" / "requirements"


def packages(spec: dict, system: str) -> list[str]:
    return (spec.get(system) or {}).get("packages", spec["packages"])


def resolve(name: str, values: list[str], platform: str, *, torch_cpu: bool = False) -> None:
    destination = OUTPUT / f"{name}.txt"
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "requirements.in"
        source.write_text("\n".join(values) + "\n", encoding="utf-8")
        command = [
            "uv",
            "pip",
            "compile",
            str(source),
            "--output-file",
            str(destination),
            "--python-version",
            "3.12.10",
            "--python-platform",
            platform,
            "--generate-hashes",
            "--no-header",
            "--no-annotate",
            "--upgrade",
            "--quiet",
        ]
        if torch_cpu:
            command.extend(["--torch-backend", "cpu"])
        subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    posix = json.loads(POSIX_LOCK.read_text(encoding="utf-8"))
    windows = json.loads(WINDOWS_LOCK.read_text(encoding="utf-8"))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    posix_targets = {
        "darwin-arm64": ("darwin", "aarch64-apple-darwin"),
        "darwin-x64": ("darwin", "x86_64-apple-darwin"),
        "linux-x64": ("linux", "x86_64-unknown-linux-gnu"),
    }
    for target, (system, uv_platform) in posix_targets.items():
        for component in ("documents", "video", "asr-other"):
            resolve(
                f"{component}-{target}",
                packages(posix["components"][component], system),
                uv_platform,
            )
        if target != "darwin-x64":
            resolve(
                f"asr-zh-{target}",
                packages(posix["components"]["asr-zh"], system),
                uv_platform,
                torch_cpu=system == "linux",
            )
    for component in ("documents", "asr-zh", "asr-other"):
        resolve(
            f"{component}-windows-x64",
            windows["components"][component]["packages"],
            "x86_64-pc-windows-msvc",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
