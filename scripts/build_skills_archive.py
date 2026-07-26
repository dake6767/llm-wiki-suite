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
import subprocess
import zipfile
from pathlib import Path

from .skills_release import SKILLS_DIR, SkillsReleaseError


EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
# Zip's epoch. Any fixed value works; this one is the format's own floor.
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def archive_members(root: Path) -> list[tuple[str, Path]]:
    """Tracked files under `root`, as (archive name, source path).

    The listing comes from `git ls-files`, not a directory walk, so the archive
    contains exactly what the repository contains. A walk answers "what is on
    this disk", which is a different question and the wrong one: it silently
    swept up whatever a working tree happened to be carrying. On one maintainer
    machine that was `.DS_Store` plus two gitignored `skills/*/data/`
    directories of personal ASR-correction notes — content that would have
    shipped to every user had the archive ever been built outside CI's clean
    checkout, and which made a local build unable to reproduce the released
    SHA-256 while debugging.

    No fallback to walking. Being unable to ask git is a broken build
    environment, and quietly answering the wrong question is precisely the
    failure this replaces.
    """
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout
    except FileNotFoundError as exc:  # no git binary
        raise SkillsReleaseError(
            f"git is required to list skill files under {root} (it defines what "
            f"ships); install git or build from a checkout") from exc
    except subprocess.CalledProcessError as exc:
        raise SkillsReleaseError(
            f"{root} is not inside a git work tree, so the archive contents "
            f"cannot be determined: {exc.stderr.strip()}") from exc

    members: list[tuple[str, Path]] = []
    for name in listing.split("\0"):
        if not name:
            continue
        path = root / name
        # Defense in depth: `git ls-files` already omits ignored files, but a
        # committed .pyc would still be wrong to ship.
        if path.suffix in EXCLUDED_SUFFIXES:
            continue
        if not path.is_file():
            raise SkillsReleaseError(
                f"tracked file missing from the working tree: {name} — the "
                f"checkout under {root} is incomplete")
        members.append((Path(name).as_posix(), path))
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
