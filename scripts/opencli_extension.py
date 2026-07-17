#!/usr/bin/env python3
"""Stage the OpenCLI Browser Bridge Chrome extension for manual loading.

opencli's browser adapters need the Browser Bridge extension loaded in the
user's Chrome. The Chrome Web Store listing is not reachable from every
network, so this script stages the official unpacked build instead: it
resolves the newest ``opencli-extension-v*.zip`` asset from the OpenCLI
GitHub releases, downloads and unzips it under
``~/.my-llm-wiki/opencli-extension/``, and prints the ``chrome://extensions``
steps the user performs by hand. Loading the extension is always a manual,
user-visible browser action; this script never touches Chrome itself.

This is an install-flow step, the companion to installing the `opencli` CLI
itself; run it from the suite checkout (`~/.my-llm-wiki/suite`):

    python3 scripts/opencli_extension.py
    python3 scripts/opencli_extension.py --status --json
    python3 scripts/opencli_extension.py --mirror-prefix <accelerator>

When GitHub (API or download) is unreachable — the default situation on
mainland-China networks — the script automatically falls back to the
project's own mirror on the wiki relay Worker (`wiki.htmlgo.to`), which
resolves and proxies exactly this one release asset. A generic accelerator
prefix (`--mirror-prefix`) stays opt-in and never defaulted; choose one at
runtime per the cn-mirrors skill only when both GitHub and the project
mirror fail, and treat it as untrusted transport: after loading,
`opencli doctor` must confirm the bridge.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path


REPO = "jackwener/OpenCLI"
RELEASES_LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
WEB_STORE_URL = (
    "https://chromewebstore.google.com/detail/opencli/"
    "ildkmabpimmkaediidaifkhjpohdnifk"
)
ASSET_PATTERN = re.compile(r"^opencli-extension-v(\d+(?:\.\d+)*)\.zip$")
# Project-owned mirror (wiki relay Worker): resolves and proxies exactly this
# one release asset. Default fallback when GitHub is unreachable (the normal
# case on mainland-China networks); not a third-party accelerator.
PROJECT_MIRROR_LATEST = "https://wiki.htmlgo.to/_mirror/opencli-extension/latest.json"
DEFAULT_DEST = "~/.my-llm-wiki/opencli-extension"
POINTER = "current.json"
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
USER_AGENT = "llm-wiki-suite-opencli-extension"


class ExtensionError(RuntimeError):
    pass


def parse_asset_name(name: str) -> str:
    """Return the extension version encoded in an official asset name."""
    match = ASSET_PATTERN.match(name)
    if not match:
        raise ExtensionError(
            f"unexpected asset name {name!r}; expected opencli-extension-v<version>.zip"
        )
    return match.group(1)


def select_asset(release: dict) -> dict:
    """Pick the extension asset out of a GitHub release payload."""
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ExtensionError("release payload has no assets list")
    matches = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", ""))
        url = str(asset.get("browser_download_url", ""))
        if ASSET_PATTERN.match(name) and url.startswith("https://"):
            matches.append({"name": name, "version": parse_asset_name(name), "url": url})
    if not matches:
        raise ExtensionError(
            f"release {release.get('tag_name', '?')} has no opencli-extension-v*.zip asset"
        )
    if len(matches) > 1:
        names = ", ".join(item["name"] for item in matches)
        raise ExtensionError(f"release has multiple extension assets: {names}")
    return matches[0]


def apply_mirror(url: str, prefix: str) -> str:
    """Prefix an https download URL with an opt-in accelerator."""
    if not prefix.startswith("https://"):
        raise ExtensionError(f"mirror prefix must be https://, got {prefix!r}")
    if not url.startswith("https://"):
        raise ExtensionError(f"refusing to mirror a non-https URL: {url!r}")
    return prefix.rstrip("/") + "/" + url


def http_json(url: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise ExtensionError(f"cannot fetch {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExtensionError(f"expected a JSON object from {url}")
    return payload


def download(url: str, target: Path, timeout: float) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
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
                    raise ExtensionError(
                        f"download exceeds {MAX_DOWNLOAD_BYTES} bytes; refusing"
                    )
                sink.write(chunk)
    except (OSError, urllib.error.URLError) as exc:
        raise ExtensionError(f"cannot download {url}: {exc}") from exc
    if total == 0:
        raise ExtensionError(f"empty download from {url}")


def extract_zip(archive: Path, staging: Path) -> None:
    """Extract with a zip-slip guard; every member must stay inside staging."""
    try:
        with zipfile.ZipFile(archive) as bundle:
            root = staging.resolve()
            for info in bundle.infolist():
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise ExtensionError(f"unsafe zip member path: {info.filename!r}")
                resolved = (root / member).resolve()
                if root != resolved and root not in resolved.parents:
                    raise ExtensionError(f"unsafe zip member path: {info.filename!r}")
            bundle.extractall(staging)
    except zipfile.BadZipFile as exc:
        raise ExtensionError(f"not a valid zip archive: {archive.name}") from exc


def find_extension_root(staging: Path) -> Path:
    """Locate the directory holding manifest.json (root or a nested folder)."""
    candidates = sorted(
        (len(path.parent.relative_to(staging).parts), path.parent)
        for path in staging.rglob("manifest.json")
    )
    if not candidates:
        raise ExtensionError("extracted archive contains no manifest.json")
    depth = candidates[0][0]
    shallowest = [path for level, path in candidates if level == depth]
    if len(shallowest) > 1:
        shown = ", ".join(str(path.relative_to(staging)) for path in shallowest)
        raise ExtensionError(f"ambiguous extension layout; manifest.json in: {shown}")
    return shallowest[0]


def load_steps(path: Path) -> list[str]:
    return [
        "Open chrome://extensions in Chrome",
        "Toggle on \"Developer mode\" (top-right corner)",
        f"Click \"Load unpacked\" and select: {path}",
        "Verify the bridge afterwards: opencli doctor",
    ]


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
    if pointer and isinstance(pointer.get("path"), str):
        staged = Path(pointer["path"])
        if (staged / "manifest.json").is_file():
            return {
                "status": "staged",
                "version": pointer.get("version", ""),
                "path": str(staged),
                "load_steps": load_steps(staged),
                "web_store": WEB_STORE_URL,
            }
    return {
        "status": "not-staged",
        "detail": "no staged Browser Bridge extension; run this script without --status",
        "web_store": WEB_STORE_URL,
    }


def resolve_via_github(timeout: float, mirror_prefix: str = "") -> tuple[dict, str]:
    release = http_json(RELEASES_LATEST_API, timeout)
    asset = select_asset(release)
    source_url = (
        apply_mirror(asset["url"], mirror_prefix) if mirror_prefix else asset["url"]
    )
    return asset, source_url


def resolve_via_project_mirror(timeout: float) -> tuple[dict, str]:
    info = http_json(PROJECT_MIRROR_LATEST, timeout)
    name = str(info.get("asset", ""))
    version = parse_asset_name(name)
    url = str(info.get("url", ""))
    if not url.startswith("https://"):
        raise ExtensionError("project mirror returned a non-https download url")
    return {"name": name, "version": version, "url": url}, url


def stage(
    dest: Path,
    *,
    asset_url: str = "",
    mirror_prefix: str = "",
    force: bool = False,
    timeout: float = 60.0,
) -> dict:
    if asset_url:
        name = Path(urllib.parse.urlsplit(asset_url).path).name
        asset = {"name": name, "version": parse_asset_name(name), "url": asset_url}
        return _stage_resolved(dest, asset, asset_url, "explicit", force, timeout)

    failures: list[str] = []
    channels = (
        ("github", lambda: resolve_via_github(timeout, mirror_prefix)),
        ("project-mirror", lambda: resolve_via_project_mirror(timeout)),
    )
    for channel, resolve in channels:
        try:
            asset, source_url = resolve()
            return _stage_resolved(dest, asset, source_url, channel, force, timeout)
        except ExtensionError as exc:
            failures.append(f"{channel}: {exc}")
    raise ExtensionError(
        "every channel failed — " + "; ".join(failures)
        + " (as a last resort pass --mirror-prefix per the cn-mirrors skill)"
    )


def _stage_resolved(
    dest: Path,
    asset: dict,
    source_url: str,
    channel: str,
    force: bool,
    timeout: float,
) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / f"opencli-extension-v{asset['version']}"
    pointer = read_pointer(dest)
    if (
        not force
        and pointer
        and pointer.get("version") == asset["version"]
        and (final / "manifest.json").is_file()
        and pointer.get("path") == str(final)
    ):
        return {
            "status": "already-staged",
            "version": asset["version"],
            "path": str(final),
            "asset": asset["name"],
            "channel": channel,
            "load_steps": load_steps(final),
            "web_store": WEB_STORE_URL,
        }

    staging = dest / f".staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        archive = staging / asset["name"]
        download(source_url, archive, timeout)
        unpacked = staging / "unpacked"
        unpacked.mkdir()
        extract_zip(archive, unpacked)
        extension_root = find_extension_root(unpacked)
        if final.exists():
            shutil.rmtree(final)
        extension_root.replace(final)
        write_pointer(dest, {
            "schema": 1,
            "version": asset["version"],
            "path": str(final),
            "asset": asset["name"],
            "source_url": source_url,
            "staged_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "status": "staged",
        "version": asset["version"],
        "path": str(final),
        "asset": asset["name"],
        "source_url": source_url,
        "channel": channel,
        "load_steps": load_steps(final),
        "web_store": WEB_STORE_URL,
    }


def emit_human(report: dict) -> None:
    if report["status"] == "not-staged":
        print("Browser Bridge extension: not staged")
        print(f"  {report['detail']}")
        print(f"  Chrome Web Store alternative: {report['web_store']}")
        return
    label = "already staged" if report["status"] == "already-staged" else "staged"
    print(f"Browser Bridge extension {label} (v{report['version']}):")
    print(f"  {report['path']}")
    print()
    print("Load it once in Chrome (manual, one-time):")
    for index, step in enumerate(report["load_steps"], start=1):
        print(f"  {index}. {step}")
    print()
    print("Keep the staged folder in place; Chrome loads the extension from it.")
    print("Chrome Web Store alternative (usually NOT reachable from mainland-China")
    print("networks; the staged folder above is the default path):")
    print(f"  {report['web_store']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--status", action="store_true", help="report the staged state only")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--force", action="store_true", help="re-download even if current")
    parser.add_argument("--dest", type=Path, default=None, help=f"staging root (default {DEFAULT_DEST})")
    parser.add_argument(
        "--mirror-prefix",
        default="",
        help="opt-in https accelerator prefix for the release download (cn-mirrors)",
    )
    parser.add_argument(
        "--asset-url",
        default="",
        help="explicit URL of an official opencli-extension-v<version>.zip asset",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="network timeout seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dest = (args.dest or Path(DEFAULT_DEST)).expanduser()
    try:
        if args.status:
            report = status_report(dest)
        else:
            report = stage(
                dest,
                asset_url=args.asset_url,
                mirror_prefix=args.mirror_prefix,
                force=args.force,
                timeout=args.timeout,
            )
    except ExtensionError as exc:
        print(f"opencli-extension: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        emit_human(report)
    return 3 if report["status"] == "not-staged" else 0


if __name__ == "__main__":
    raise SystemExit(main())
