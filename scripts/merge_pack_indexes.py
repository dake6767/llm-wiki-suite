#!/usr/bin/env python3
"""Merge platform pack indexes into one immutable pack release index."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pack_release import PackReleaseError, load_metadata, validate_index


class MergeError(RuntimeError):
    pass


def merge(paths: list[Path], metadata: dict) -> dict:
    if not paths:
        raise MergeError("no platform pack indexes supplied")
    artifacts = []
    identities = set()
    for path in paths:
        part = json.loads(path.read_text(encoding="utf-8"))
        if part.get("schema") != 1:
            raise MergeError(f"unsupported platform pack index: {path}")
        if part.get("pack_version") != metadata["version"]:
            raise MergeError(f"platform pack version differs: {path}")
        if part.get("input_sha256") != metadata["input_sha256"]:
            raise MergeError(f"platform pack input digest differs: {path}")
        for artifact in part.get("artifacts", []):
            identity = (
                artifact.get("id"),
                artifact.get("platform"),
                artifact.get("architecture"),
            )
            if identity in identities:
                raise MergeError(f"duplicate artifact: {identity}")
            identities.add(identity)
            artifacts.append(artifact)
    artifacts.sort(
        key=lambda row: (row["id"], row["platform"], row["architecture"])
    )
    result = {
        "schema": 1,
        "pack_version": metadata["version"],
        "input_sha256": metadata["input_sha256"],
        "artifacts": artifacts,
    }
    try:
        validate_index(result, metadata)
    except PackReleaseError as exc:
        raise MergeError(str(exc)) from exc
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = merge(args.input, load_metadata())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(args.out)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MergeError as exc:
        print(f"pack-index-merge: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
