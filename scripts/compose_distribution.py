#!/usr/bin/env python3
"""Compose one jointly tested distribution from an immutable pack index."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .pack_release import load_metadata, validate_index


VERSION_RE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")


def compose(index: dict, distribution_version: str, metadata: dict) -> dict:
    if not VERSION_RE.fullmatch(distribution_version):
        raise ValueError(f"invalid distribution version: {distribution_version!r}")
    validate_index(index, metadata)
    return {
        "schema": 1,
        "channel": "stable",
        "distribution_version": distribution_version,
        "browser_version": distribution_version,
        "skills_pack_version": distribution_version,
        "pack_version": metadata["version"],
        "artifacts": index["artifacts"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack-index", type=Path, required=True)
    parser.add_argument("--distribution-version", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    index = json.loads(args.pack_index.read_text(encoding="utf-8"))
    result = compose(index, args.distribution_version, load_metadata())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
