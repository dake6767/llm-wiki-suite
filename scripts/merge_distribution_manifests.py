#!/usr/bin/env python3
"""Merge per-platform release manifests into distribution.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


class MergeError(RuntimeError):
    pass


def merge(paths: list[Path]) -> dict:
    if not paths:
        raise MergeError("no distribution manifests supplied")
    manifests = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    header = {
        key: manifests[0][key]
        for key in ("schema", "channel", "distribution_version", "browser_version", "skills_pack_version")
    }
    artifacts = []
    identities = set()
    for manifest in manifests:
        if any(manifest.get(key) != value for key, value in header.items()):
            raise MergeError("distribution manifest headers differ")
        for artifact in manifest.get("artifacts", []):
            identity = (artifact["id"], artifact["platform"], artifact["architecture"])
            if identity in identities:
                raise MergeError(f"duplicate artifact: {identity}")
            identities.add(identity)
            artifacts.append(artifact)
    artifacts.sort(key=lambda row: (row["id"], row["platform"], row["architecture"]))
    return {**header, "artifacts": artifacts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    manifest = merge(args.input)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
