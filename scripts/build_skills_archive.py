#!/usr/bin/env python3
"""Pack `skills/` into the archive published as one immutable Skills Pack release.

Entries are stored relative to `skills/` (`my-llm-wiki/SKILL.md`, ...) so the
Browser can expand one slug per top-level directory, matching the layout it also
gets from its embedded baseline.

The archive is byte-deterministic — sorted entries and a fixed timestamp — so
rebuilding the same commit reproduces the same SHA-256 and a republish can never
silently diverge from what a release already pinned.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from .skills_release import SKILLS_DIR, SkillsReleaseError


EXCLUDED_DIRS = {"__pycache__", ".git"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
# Zip's epoch. Any fixed value works; this one is the format's own floor.
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def archive_members(root: Path) -> list[tuple[str, Path]]:
    members = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if EXCLUDED_DIRS.intersection(relative.parts):
            continue
        if path.suffix in EXCLUDED_SUFFIXES:
            continue
        members.append((relative.as_posix(), path))
    members.sort(key=lambda member: member[0])
    return members


def build(destination: Path, root: Path | None = None) -> Path:
    source = root or SKILLS_DIR
    members = archive_members(source)
    if not members:
        raise SkillsReleaseError(f"no skill files found under {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in members:
            info = zipfile.ZipInfo(name, date_time=FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--skills-dir", type=Path, default=None)
    args = parser.parse_args()
    print(build(args.out, args.skills_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
