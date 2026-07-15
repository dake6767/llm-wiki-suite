#!/usr/bin/env python3
"""List captured source ids or find RAW paths for one exact id.

RAW frontmatter is the source of truth for "what have I already captured".  The
default listing mode is the cheap fetch-filter used by bookmark syncs.  ``--find``
is the single-post preflight: it prints every matching absolute RAW path, oldest
``captured_at`` first, and exits 1 when the id is absent.

Scans ``raw/sources/<source>/**/*.md`` in the selected wiki.

Usage:
  captured_ids.py --wiki <wiki-root> [--source x]
  captured_ids.py --wiki <wiki-root> [--source x] --find <original-id>
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def frontmatter_fields(path: Path) -> dict[str, str]:
    """Read only the small scalar fields needed for identity lookup."""
    values: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8") as src:
            if src.readline().strip() != "---":
                return values
            for line in src:
                stripped = line.strip()
                if stripped == "---":
                    break
                match = re.match(r"^(original_id|captured_at):\s*(.*)$", stripped)
                if match:
                    values[match.group(1)] = match.group(2).strip().strip("\"'")
    except OSError:
        pass
    return values


def captured_entries(wiki: Path, source: str) -> list[tuple[str, str, Path]]:
    """Return ``(original_id, captured_at, absolute_path)`` entries."""
    root = wiki / "raw" / "sources" / source
    if not root.is_dir():
        return []
    entries: list[tuple[str, str, Path]] = []
    for path in sorted(root.rglob("*.md")):
        fields = frontmatter_fields(path)
        original_id = fields.get("original_id", "")
        if original_id:
            entries.append((original_id, fields.get("captured_at", ""), path.resolve()))
    return entries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki", required=True,
                    help="wiki root (the dir with schema.md + raw/sources/)")
    ap.add_argument("--source", default="x",
                    help="source_type bucket under raw/sources (default: x)")
    ap.add_argument("--find", metavar="ORIGINAL_ID",
                    help="print matching RAW paths oldest-first; exit 1 if absent")
    args = ap.parse_args(argv)

    wiki = Path(args.wiki).expanduser().resolve()
    entries = captured_entries(wiki, args.source)

    if args.find is not None:
        matches = sorted(
            (entry for entry in entries if entry[0] == args.find),
            key=lambda entry: (entry[1], entry[2].as_posix()),
        )
        for _, _, path in matches:
            print(path)
        print(
            f"# {len(matches)} matching RAW path(s) for original_id={args.find}",
            file=sys.stderr,
        )
        return 0 if matches else 1

    ids = sorted({original_id for original_id, _, _ in entries})
    for original_id in ids:
        print(original_id)
    print(f"# {len(ids)} captured ids under raw/sources/{args.source}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
