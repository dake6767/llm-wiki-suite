#!/usr/bin/env python3
"""Enforce that ``pack_version`` was bumped when distribution changes.

doc 21 §9: when ``skills/**`` or distribution-affecting registry content changes
relative to the PR's target branch, ``registry/skills.json`` ``pack_version`` must
strictly increase. Otherwise the desktop app can never tell a shipped change apart
from the previous release.

Usage:
    check_pack_version_bump.py --base <ref> [--head <ref>]

``--base`` is the target branch ref (e.g. ``origin/main``); ``--head`` defaults to
the working tree. Reads both ``registry/skills.json`` blobs via ``git show`` /
filesystem and compares the semver core triple.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_REL = "registry/skills.json"

# Distribution-affecting paths: anything under skills/, the skills registry itself,
# and the bootstrap target list (changes what gets installed where).
DIST_PREFIXES = ("skills/",)
DIST_FILES = ("registry/skills.json", "registry/bootstrap.json")

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def semver_core(v: object) -> tuple[int, int, int]:
    if not isinstance(v, str):
        raise SystemExit(f"pack_version must be a string, got {v!r}")
    m = SEMVER_RE.match(v)
    if not m:
        raise SystemExit(f"pack_version must be semver X.Y.Z, got {v!r}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def pack_version_at(ref: str | None) -> object:
    if ref is None:
        text = (ROOT / REGISTRY_REL).read_text(encoding="utf-8")
    else:
        text = git("show", f"{ref}:{REGISTRY_REL}")
    return json.loads(text).get("pack_version")


def changed_files(base: str, head: str | None) -> list[str]:
    head_ref = head or "HEAD"
    out = git("diff", "--name-only", f"{base}...{head_ref}")
    return [line for line in out.splitlines() if line.strip()]


def is_distribution_change(path: str) -> bool:
    return path in DIST_FILES or any(path.startswith(p) for p in DIST_PREFIXES)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True, help="target branch ref, e.g. origin/main")
    ap.add_argument("--head", default=None, help="head ref (default: working tree)")
    args = ap.parse_args(argv)

    files = changed_files(args.base, args.head)
    dist = [f for f in files if is_distribution_change(f)]
    if not dist:
        print("no distribution-affecting changes; pack_version bump not required")
        return 0

    base_v = pack_version_at(args.base)
    head_v = pack_version_at(args.head)
    # First introduction: base predates pack_version → treat as the 0.0.0 baseline so
    # any valid head version counts as a bump.
    base_core = (0, 0, 0) if base_v is None else semver_core(base_v)
    head_core = semver_core(head_v)

    print(f"distribution changes:\n  " + "\n  ".join(dist))
    print(f"pack_version base={base_v} head={head_v}")

    if head_core <= base_core:
        print(
            "\nERROR: distribution content changed but pack_version did not increase.\n"
            f"  bump registry/skills.json pack_version above {base_v}.",
            file=sys.stderr,
        )
        return 1
    print("pack_version bump OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
