#!/usr/bin/env python3
"""Stage a portable ffmpeg build for Windows machines behind restricted networks.

The winget recipe for ffmpeg (`Gyan.FFmpeg`) downloads its installer payload
from GitHub Releases, which fails on mainland-China networks even though
winget's own CDN is reachable. This script is the structured `cn` route for
that platform: it resolves the official gyan.dev "essentials" build (the same
distribution winget points at), verifies its SHA-256, and unzips the `bin/`
directory under ``~/.my-llm-wiki/tools/ffmpeg/bin``. preflight/doctor probe
that directory in addition to PATH, so no global PATH mutation is needed.

This is an install-flow step; run it from the suite checkout
(`~/.my-llm-wiki/suite`):

    python3 scripts/fetch_ffmpeg.py
    python3 scripts/fetch_ffmpeg.py --status --json
    python3 scripts/fetch_ffmpeg.py --mirror-prefix <accelerator>

Channel order: gyan.dev direct (it is ffmpeg's official non-GitHub Windows
distribution and serves mainland networks), then the project's own mirror on
the wiki relay Worker (`wiki.htmlgo.to`), which pins one release and proxies
its zip with a manifest-recorded hash. A generic accelerator prefix
(`--mirror-prefix`) stays opt-in and never defaulted; choose one at runtime
per the cn-mirrors skill only when both channels fail. Every channel must
supply a SHA-256; a download that cannot be verified is refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path


GYAN_BASE = "https://www.gyan.dev/ffmpeg/builds"
GYAN_VERSION_URL = f"{GYAN_BASE}/release-version"
GYAN_ZIP_URL = f"{GYAN_BASE}/ffmpeg-release-essentials.zip"
GYAN_SHA256_URL = f"{GYAN_ZIP_URL}.sha256"
# Project-owned mirror (wiki relay Worker): pins one essentials build and
# proxies exactly that zip. Default fallback when gyan.dev is unreachable;
# not a third-party accelerator.
PROJECT_MIRROR_LATEST = "https://wiki.htmlgo.to/_mirror/ffmpeg/latest.json"
DEFAULT_DEST = "~/.my-llm-wiki/tools/ffmpeg"
POINTER = "current.json"
VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+)*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024
USER_AGENT = "llm-wiki-suite-fetch-ffmpeg"


class FetchError(RuntimeError):
    pass


def parse_version(text: str) -> str:
    version = text.strip()
    if not VERSION_PATTERN.match(version):
        raise FetchError(f"unexpected ffmpeg version string {version!r}")
    return version


def parse_sha256(text: str) -> str:
    # gyan.dev serves a bare hex digest; also tolerate "digest *filename".
    digest = text.strip().split()[0].lower() if text.strip() else ""
    if not SHA256_PATTERN.match(digest):
        raise FetchError(f"unexpected sha256 payload {text[:80]!r}")
    return digest


def apply_mirror(url: str, prefix: str) -> str:
    """Prefix an https download URL with an opt-in accelerator."""
    if not prefix.startswith("https://"):
        raise FetchError(f"mirror prefix must be https://, got {prefix!r}")
    if not url.startswith("https://"):
        raise FetchError(f"refusing to mirror a non-https URL: {url!r}")
    return prefix.rstrip("/") + "/" + url


def http_text(url: str, timeout: float, limit: int = 4096) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(limit).decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError) as exc:
        raise FetchError(f"cannot fetch {url}: {exc}") from exc


def http_json(url: str, timeout: float) -> dict:
    try:
        payload = json.loads(http_text(url, timeout, limit=1 << 20))
    except json.JSONDecodeError as exc:
        raise FetchError(f"invalid JSON from {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FetchError(f"expected a JSON object from {url}")
    return payload


def download(url: str, target: Path, sha256: str, timeout: float) -> None:
    """Stream ``url`` to ``target`` and verify its SHA-256 before returning."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, \
                target.open("wb") as sink:
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise FetchError(
                        f"download exceeds {MAX_DOWNLOAD_BYTES} bytes; refusing"
                    )
                digest.update(chunk)
                sink.write(chunk)
    except (OSError, urllib.error.URLError) as exc:
        raise FetchError(f"cannot download {url}: {exc}") from exc
    if total == 0:
        raise FetchError(f"empty download from {url}")
    if digest.hexdigest() != sha256:
        raise FetchError(
            f"sha256 mismatch for {url}: expected {sha256}, got {digest.hexdigest()}"
        )


def extract_zip(archive: Path, staging: Path) -> None:
    """Extract with a zip-slip guard; every member must stay inside staging."""
    try:
        with zipfile.ZipFile(archive) as bundle:
            root = staging.resolve()
            for info in bundle.infolist():
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise FetchError(f"unsafe zip member path: {info.filename!r}")
                resolved = (root / member).resolve()
                if root != resolved and root not in resolved.parents:
                    raise FetchError(f"unsafe zip member path: {info.filename!r}")
            bundle.extractall(staging)
    except zipfile.BadZipFile as exc:
        raise FetchError(f"not a valid zip archive: {archive.name}") from exc


def find_bin_dir(staging: Path) -> Path:
    """Locate the extracted directory that holds the ffmpeg executable."""
    candidates = sorted(
        (len(path.parent.relative_to(staging).parts), path.parent)
        for name in ("ffmpeg.exe", "ffmpeg")
        for path in staging.rglob(name)
        if path.is_file()
    )
    if not candidates:
        raise FetchError("extracted archive contains no ffmpeg executable")
    return candidates[0][1]


def ffmpeg_executable(bin_dir: Path) -> Path | None:
    for name in ("ffmpeg.exe", "ffmpeg"):
        candidate = bin_dir / name
        if candidate.is_file():
            return candidate
    return None


def run_postcheck(executable: Path, timeout: float = 30.0) -> None:
    """Run ``ffmpeg -version`` where the staged binary can execute (Windows)."""
    if os.name != "nt":
        return
    import subprocess

    try:
        result = subprocess.run(
            [str(executable), "-version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FetchError(f"staged ffmpeg failed to run: {exc}") from exc
    if result.returncode != 0:
        raise FetchError(f"staged ffmpeg exited {result.returncode} on -version")


def read_pointer(dest: Path) -> dict | None:
    pointer = dest / POINTER
    try:
        value = json.loads(pointer.read_text(encoding="utf-8"))
    except OSError:
        return None
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def write_pointer(dest: Path, record: dict) -> None:
    temp = dest / f".{POINTER}.{uuid.uuid4().hex}"
    temp.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temp.replace(dest / POINTER)


def status_report(dest: Path) -> dict:
    pointer = read_pointer(dest)
    bin_dir = dest / "bin"
    executable = ffmpeg_executable(bin_dir)
    if executable is not None:
        try:
            run_postcheck(executable)
        except FetchError as exc:
            return {"status": "not-staged", "detail": str(exc)}
        return {
            "status": "staged",
            "version": (pointer or {}).get("version", ""),
            "bin": str(bin_dir),
            "ffmpeg": str(executable),
        }
    return {
        "status": "not-staged",
        "detail": "no staged ffmpeg; run this script without --status",
    }


def resolve_via_gyan(timeout: float, mirror_prefix: str = "") -> dict:
    def routed(url: str) -> str:
        return apply_mirror(url, mirror_prefix) if mirror_prefix else url

    version = parse_version(http_text(routed(GYAN_VERSION_URL), timeout))
    sha256 = parse_sha256(http_text(routed(GYAN_SHA256_URL), timeout))
    return {
        "version": version,
        "asset": f"ffmpeg-{version}-essentials_build.zip",
        "url": routed(GYAN_ZIP_URL),
        "sha256": sha256,
    }


def resolve_via_project_mirror(timeout: float) -> dict:
    info = http_json(PROJECT_MIRROR_LATEST, timeout)
    version = parse_version(str(info.get("version", "")))
    sha256 = parse_sha256(str(info.get("sha256", "")))
    url = str(info.get("url", ""))
    if not url.startswith("https://"):
        raise FetchError("project mirror returned a non-https download url")
    return {
        "version": version,
        "asset": str(info.get("asset", f"ffmpeg-{version}-essentials_build.zip")),
        "url": url,
        "sha256": sha256,
    }


def stage(
    dest: Path,
    *,
    asset_url: str = "",
    asset_sha256: str = "",
    mirror_prefix: str = "",
    force: bool = False,
    timeout: float = 120.0,
) -> dict:
    if asset_url:
        if not asset_url.startswith("https://"):
            raise FetchError("--asset-url must be https://")
        asset = {
            "version": "explicit",
            "asset": Path(asset_url.split("?", 1)[0]).name,
            "url": asset_url,
            "sha256": parse_sha256(asset_sha256),
        }
        return _stage_resolved(dest, asset, "explicit", force, timeout)

    failures: list[str] = []
    channels = (
        ("gyan.dev", lambda: resolve_via_gyan(timeout, mirror_prefix)),
        ("project-mirror", lambda: resolve_via_project_mirror(timeout)),
    )
    for channel, resolve in channels:
        try:
            asset = resolve()
            return _stage_resolved(dest, asset, channel, force, timeout)
        except FetchError as exc:
            failures.append(f"{channel}: {exc}")
    raise FetchError(
        "every channel failed — " + "; ".join(failures)
        + " (as a last resort pass --mirror-prefix per the cn-mirrors skill)"
    )


def _stage_resolved(
    dest: Path,
    asset: dict,
    channel: str,
    force: bool,
    timeout: float,
) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    bin_dir = dest / "bin"
    pointer = read_pointer(dest)
    existing = ffmpeg_executable(bin_dir)
    if (
        not force
        and pointer
        and pointer.get("version") == asset["version"]
        and existing is not None
    ):
        return {
            "status": "already-staged",
            "version": asset["version"],
            "bin": str(bin_dir),
            "ffmpeg": str(existing),
            "channel": channel,
        }

    staging = dest / f".staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        archive = staging / asset["asset"]
        download(asset["url"], archive, asset["sha256"], timeout)
        unpacked = staging / "unpacked"
        unpacked.mkdir()
        extract_zip(archive, unpacked)
        source_bin = find_bin_dir(unpacked)
        if bin_dir.exists():
            shutil.rmtree(bin_dir)
        source_bin.replace(bin_dir)
        executable = ffmpeg_executable(bin_dir)
        if executable is None:
            raise FetchError("staged bin directory lost its ffmpeg executable")
        run_postcheck(executable)
        write_pointer(dest, {
            "schema": 1,
            "version": asset["version"],
            "asset": asset["asset"],
            "sha256": asset["sha256"],
            "source_url": asset["url"],
            "channel": channel,
            "bin": str(bin_dir),
            "staged_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "status": "staged",
        "version": asset["version"],
        "bin": str(bin_dir),
        "ffmpeg": str(ffmpeg_executable(bin_dir)),
        "sha256": asset["sha256"],
        "channel": channel,
    }


def emit_human(report: dict) -> None:
    if report["status"] == "not-staged":
        print("portable ffmpeg: not staged")
        print(f"  {report['detail']}")
        return
    label = "already staged" if report["status"] == "already-staged" else "staged"
    version = report.get("version") or "unknown version"
    print(f"portable ffmpeg {label} ({version}):")
    print(f"  {report['ffmpeg']}")
    print()
    print("The suite probes this location automatically; no PATH change needed.")
    print("When invoking yt-dlp by hand, pass:")
    print(f"  --ffmpeg-location {report['bin']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--status", action="store_true", help="report the staged state only")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--force", action="store_true", help="re-download even if current")
    parser.add_argument("--dest", type=Path, default=None, help=f"staging root (default {DEFAULT_DEST})")
    parser.add_argument(
        "--mirror-prefix",
        default="",
        help="opt-in https accelerator prefix for the gyan.dev downloads (cn-mirrors)",
    )
    parser.add_argument(
        "--asset-url",
        default="",
        help="explicit https URL of an ffmpeg essentials zip (requires --sha256)",
    )
    parser.add_argument(
        "--sha256",
        default="",
        help="expected SHA-256 of the --asset-url download",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="network timeout seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.asset_url and not args.sha256:
        print("fetch-ffmpeg: --asset-url requires --sha256", file=sys.stderr)
        return 1
    dest = (args.dest or Path(DEFAULT_DEST)).expanduser()
    try:
        if args.status:
            report = status_report(dest)
        else:
            report = stage(
                dest,
                asset_url=args.asset_url,
                asset_sha256=args.sha256,
                mirror_prefix=args.mirror_prefix,
                force=args.force,
                timeout=args.timeout,
            )
    except FetchError as exc:
        print(f"fetch-ffmpeg: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        emit_human(report)
    return 3 if report["status"] == "not-staged" else 0


if __name__ == "__main__":
    raise SystemExit(main())
