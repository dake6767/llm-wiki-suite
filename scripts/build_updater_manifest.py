#!/usr/bin/env python3
"""Build one Tauri updater manifest after every platform upload completes."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


VERSION_RE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")


def asset_rows(payload: object) -> list[dict]:
    if isinstance(payload, dict):
        payload = payload.get("assets")
    if not isinstance(payload, list) or not all(
        isinstance(row, dict) for row in payload
    ):
        raise ValueError("release assets JSON must be a list or an object with an assets list")
    return payload


def bundle_layout(version: str) -> dict[str, tuple[str, tuple[str, ...]]]:
    return {
        "darwin-aarch64": (
            "My.LLM.Wiki.Browser_aarch64.app.tar.gz",
            ("darwin-aarch64", "darwin-aarch64-app"),
        ),
        "darwin-x86_64": (
            "My.LLM.Wiki.Browser_x64.app.tar.gz",
            ("darwin-x86_64", "darwin-x86_64-app"),
        ),
        "linux-x86_64-appimage": (
            f"My.LLM.Wiki.Browser_{version}_amd64.AppImage",
            ("linux-x86_64", "linux-x86_64-appimage"),
        ),
        "linux-x86_64-deb": (
            f"My.LLM.Wiki.Browser_{version}_amd64.deb",
            ("linux-x86_64-deb",),
        ),
        "windows-x86_64-nsis": (
            f"My.LLM.Wiki.Browser_{version}_x64-setup.exe",
            ("windows-x86_64", "windows-x86_64-nsis"),
        ),
    }


def build(
    release_tag: str,
    repository: str,
    asset_names: set[str],
    signatures: Path,
    pub_date: str,
) -> dict:
    version = release_tag.removeprefix("v")
    if release_tag != f"v{version}" or not VERSION_RE.fullmatch(version):
        raise ValueError(f"invalid release tag: {release_tag!r}")
    platforms = {}
    for bundle, keys in bundle_layout(version).values():
        signature_name = f"{bundle}.sig"
        missing = {bundle, signature_name} - asset_names
        if missing:
            raise ValueError(f"updater assets are missing: {sorted(missing)}")
        signature_path = signatures / signature_name
        if not signature_path.is_file():
            raise ValueError(f"downloaded updater signature is missing: {signature_name}")
        signature = signature_path.read_text(encoding="utf-8").strip()
        if not signature:
            raise ValueError(f"downloaded updater signature is empty: {signature_name}")
        entry = {
            "signature": signature,
            "url": (
                f"https://github.com/{repository}/releases/download/"
                f"{release_tag}/{quote(bundle)}"
            ),
        }
        for key in keys:
            if key in platforms:
                raise ValueError(f"duplicate updater platform key: {key}")
            platforms[key] = entry
    return {
        "version": version,
        "notes": "",
        "pub_date": pub_date,
        "platforms": platforms,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--assets-json", type=Path, required=True)
    parser.add_argument("--signatures", type=Path, required=True)
    parser.add_argument("--pub-date")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.assets_json.read_text(encoding="utf-8"))
    rows = asset_rows(payload)
    names = {row["name"] for row in rows}
    pub_date = args.pub_date or (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    result = build(
        args.release_tag, args.repository, names, args.signatures, pub_date
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
