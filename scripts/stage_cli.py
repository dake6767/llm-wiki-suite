#!/usr/bin/env python3
"""Build the shared Setup Core CLI and stage it as a Tauri sidecar."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT / "apps" / "my-llm-wiki-browser"
BINARIES = WORKSPACE / "desktop" / "src-tauri" / "binaries"


def host_target() -> str:
    output = subprocess.check_output(["rustc", "-vV"], text=True)
    for line in output.splitlines():
        if line.startswith("host: "):
            return line.removeprefix("host: ")
    raise RuntimeError("rustc did not report a host target")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", help="Rust target triple; defaults to the current host")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--dist", type=Path, help="also create a standalone CLI zip")
    args = parser.parse_args()
    target = args.target or host_target()
    profile = "debug" if args.debug else "release"
    command = [
        "cargo", "build", "-p", "llm-wiki-setup", "--bin", "my-llm-wiki"
    ]
    if not args.debug:
        command.append("--release")
    if args.target:
        command.extend(["--target", args.target])
    subprocess.run(command, cwd=WORKSPACE, check=True)
    extension = ".exe" if "windows" in target else ""
    target_root = WORKSPACE / "target"
    source = target_root / target / profile / f"my-llm-wiki{extension}" if args.target else target_root / profile / f"my-llm-wiki{extension}"
    if not source.is_file():
        raise RuntimeError(f"CLI build output is missing: {source}")
    BINARIES.mkdir(parents=True, exist_ok=True)
    destination = BINARIES / f"my-llm-wiki-{target}{extension}"
    shutil.copy2(source, destination)
    result = {"status": "staged", "target": target, "path": str(destination)}
    if args.dist:
        version = json.loads(
            (WORKSPACE / "desktop" / "src-tauri" / "tauri.conf.json").read_text(
                encoding="utf-8"
            )
        )["version"]
        platform = "windows" if "windows" in target else "darwin" if "apple-darwin" in target else "linux"
        architecture = "arm64" if target.startswith("aarch64") else "x64"
        args.dist.mkdir(parents=True, exist_ok=True)
        archive = args.dist / f"My-LLM-Wiki-CLI_{version}_{platform}_{architecture}.zip"
        info = zipfile.ZipInfo(f"my-llm-wiki{extension}")
        info.external_attr = (0o100755 if not extension else 0o100644) << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(info, source.read_bytes())
        result["archive"] = str(archive)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
