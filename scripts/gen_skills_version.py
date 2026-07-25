#!/usr/bin/env python3
"""Generate the published `skills-version.json` from the release metadata.

This is the latest-side signal the Browser polls through
`wiki.htmlgo.to/_skills/version.json`. It is generated, never hand-written, so
the version number keeps exactly one source (`registry/skills-release.json`).

Schema 2 adds the payload fields (`sha256`, `size`, `installed_size`) that
schema 1 did not need: back then the app only displayed the version and handed
the actual update to an agent, so it never fetched anything. The Browser now
downloads and installs the pack itself and has to pin what it is getting.

Every field here is treated as untrusted input on the reading side — the app
re-parses versions, bounds the response, and never lets `pack_notes` reach a
prompt. Keep this payload minimal so that contract stays cheap to honour.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .skills_release import load_metadata


SHA_RE = re.compile(r"[0-9a-f]{40}")
SCHEMA = 2


def describe_archive(path: Path) -> dict:
    data = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        installed_size = sum(entry.file_size for entry in archive.infolist())
    if not installed_size:
        raise SystemExit(f"skills archive is empty: {path}")
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "installed_size": installed_size,
    }


def build(
    archive: Path,
    source_commit: str | None = None,
    released_at: str | None = None,
    metadata_path: Path | None = None,
) -> dict:
    metadata = load_metadata(metadata_path)
    payload = {
        "schema": SCHEMA,
        "pack_version": metadata["pack_version"],
        "released_at": released_at or datetime.now(timezone.utc).isoformat(),
        **describe_archive(archive),
    }
    commit = source_commit or os.environ.get("GITHUB_SHA")
    if commit and SHA_RE.fullmatch(commit):
        payload["source_commit"] = commit
    floor = metadata.get("min_app_version")
    if floor:
        payload["min_app_version"] = floor
    notes = metadata.get("pack_notes")
    if isinstance(notes, str) and notes.strip():
        payload["pack_notes"] = notes.strip()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--source-commit", default=None)
    parser.add_argument("--released-at", default=None)
    parser.add_argument("--metadata", type=Path, default=None)
    parser.add_argument("-o", "--output", type=Path, default=None)
    args = parser.parse_args()
    payload = build(
        args.archive, args.source_commit, args.released_at, args.metadata
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
