#!/usr/bin/env python3
"""Validate immutable runtime-pack release metadata and indexes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parent.parent
RELEASE_METADATA = ROOT / "registry" / "pack-release.json"
PACK_IDS = ("toolchain-base", "asr-zh", "asr-other")
TARGETS = (
    ("darwin", "arm64", PACK_IDS),
    ("darwin", "x64", ("toolchain-base", "asr-other")),
    ("linux", "x64", PACK_IDS),
    ("windows", "x64", PACK_IDS),
)
VERSION_RE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class PackReleaseError(RuntimeError):
    pass


def input_paths() -> list[Path]:
    paths = [
        ROOT / "scripts" / "build_distribution.py",
        ROOT / "scripts" / "merge_pack_indexes.py",
        ROOT / "scripts" / "pack_release.py",
        ROOT / "registry" / "pack-build-posix.lock.json",
        ROOT / "registry" / "pack-build-windows.lock.json",
        ROOT / "registry" / "opencli" / "package.json",
        ROOT / "registry" / "opencli" / "package-lock.json",
    ]
    paths.extend(sorted((ROOT / "registry" / "requirements").glob("*.txt")))
    missing = [str(path.relative_to(ROOT)) for path in paths if not path.is_file()]
    if missing:
        raise PackReleaseError(f"pack release inputs are missing: {missing}")
    return sorted(paths)


def canonical_input_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def input_sha256() -> str:
    digest = hashlib.sha256()
    for path in input_paths():
        relative = path.relative_to(ROOT).as_posix().encode()
        payload = canonical_input_bytes(path)
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def load_metadata(path: Path = RELEASE_METADATA) -> dict:
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if metadata.get("schema") != 1:
        raise PackReleaseError("unsupported pack release metadata schema")
    version = metadata.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise PackReleaseError(f"invalid pack release version: {version!r}")
    expected = input_sha256()
    if metadata.get("input_sha256") != expected:
        raise PackReleaseError(
            "pack release input digest differs; bump version and refresh "
            f"registry/pack-release.json to {expected}"
        )
    return metadata


def pack_tag(version: str) -> str:
    return f"packs-v{version}"


def asset_name(pack_id: str, version: str, platform: str, architecture: str) -> str:
    return f"My-LLM-Wiki-{pack_id}_{version}_{platform}_{architecture}.zip"


def expected_identities() -> set[tuple[str, str, str]]:
    return {
        (pack_id, platform, architecture)
        for platform, architecture, packs in TARGETS
        for pack_id in packs
    }


def expected_asset_names(version: str) -> set[str]:
    return {
        asset_name(pack_id, version, platform, architecture)
        for pack_id, platform, architecture in expected_identities()
    }


def asset_rows(payload: object) -> list[dict]:
    if isinstance(payload, dict):
        payload = payload.get("assets")
    if not isinstance(payload, list) or not all(
        isinstance(row, dict) for row in payload
    ):
        raise PackReleaseError(
            "release assets JSON must be a list or an object with an assets list"
        )
    return payload


def validate_index(index: dict, metadata: dict, asset_names: set[str] | None = None) -> None:
    version = metadata["version"]
    if index.get("schema") != 1:
        raise PackReleaseError("unsupported pack index schema")
    if index.get("pack_version") != version:
        raise PackReleaseError(
            f"pack index version differs: {index.get('pack_version')!r} != {version!r}"
        )
    if index.get("input_sha256") != metadata["input_sha256"]:
        raise PackReleaseError("pack index input digest differs")
    artifacts = index.get("artifacts")
    if not isinstance(artifacts, list):
        raise PackReleaseError("pack index artifacts must be a list")
    observed: set[tuple[str, str, str]] = set()
    expected_names = expected_asset_names(version)
    for artifact in artifacts:
        identity = (
            artifact.get("id"),
            artifact.get("platform"),
            artifact.get("architecture"),
        )
        if identity in observed:
            raise PackReleaseError(f"duplicate pack artifact: {identity}")
        observed.add(identity)
        if artifact.get("version") != version:
            raise PackReleaseError(f"pack artifact version differs: {identity}")
        sha = artifact.get("sha256")
        if not isinstance(sha, str) or not SHA256_RE.fullmatch(sha):
            raise PackReleaseError(f"invalid pack artifact SHA-256: {identity}")
        if not isinstance(artifact.get("size"), int) or artifact["size"] <= 0:
            raise PackReleaseError(f"invalid pack artifact size: {identity}")
        if (
            not isinstance(artifact.get("installed_size"), int)
            or artifact["installed_size"] <= 0
        ):
            raise PackReleaseError(f"invalid installed pack size: {identity}")
        urls = artifact.get("urls")
        expected_name = asset_name(identity[0], version, identity[1], identity[2])
        if not isinstance(urls, list) or len(urls) != 2:
            raise PackReleaseError(f"invalid pack artifact URLs: {identity}")
        if any(Path(unquote(urlparse(url).path)).name != expected_name for url in urls):
            raise PackReleaseError(f"pack artifact URL name differs: {identity}")
        expected_tag = pack_tag(version)
        if any(f"/{expected_tag}/" not in url for url in urls):
            raise PackReleaseError(f"pack artifact URL tag differs: {identity}")
    expected = expected_identities()
    if observed != expected:
        raise PackReleaseError(
            f"pack artifact mismatch; missing={sorted(expected - observed)}, "
            f"extra={sorted(observed - expected)}"
        )
    if asset_names is not None:
        expected_release_assets = expected_names | {"pack-index.json"}
        if asset_names != expected_release_assets:
            raise PackReleaseError(
                "pack release asset mismatch; "
                f"missing={sorted(expected_release_assets - asset_names)}, "
                f"extra={sorted(asset_names - expected_release_assets)}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("metadata")
    validate = subparsers.add_parser("validate-index")
    validate.add_argument("--index", type=Path, required=True)
    validate.add_argument("--assets-json", type=Path)
    args = parser.parse_args()
    metadata = load_metadata()
    if args.command == "metadata":
        print(
            json.dumps(
                {
                    **metadata,
                    "tag": pack_tag(metadata["version"]),
                    "input_paths": [
                        path.relative_to(ROOT).as_posix() for path in input_paths()
                    ],
                }
            )
        )
        return 0
    index = json.loads(args.index.read_text(encoding="utf-8"))
    names = None
    if args.assets_json:
        payload = json.loads(args.assets_json.read_text(encoding="utf-8"))
        rows = asset_rows(payload)
        names = {row["name"] for row in rows}
    validate_index(index, metadata, names)
    print(json.dumps({"status": "valid", "pack_version": metadata["version"]}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PackReleaseError as exc:
        print(f"pack-release: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
