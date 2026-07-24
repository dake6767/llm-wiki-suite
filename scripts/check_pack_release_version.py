#!/usr/bin/env python3
"""Require a new immutable pack version whenever pack inputs change."""

from __future__ import annotations

import argparse
import json
import subprocess

from .pack_release import RELEASE_METADATA, ROOT, load_metadata


PACK_INPUT_SCOPES = (
    "scripts/build_distribution.py",
    "scripts/merge_pack_indexes.py",
    "scripts/pack_release.py",
    "registry/pack-build-posix.lock.json",
    "registry/pack-build-windows.lock.json",
    "registry/opencli",
    "registry/requirements",
)


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    ).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    if not args.base or set(args.base) == {"0"}:
        return 0
    current = load_metadata()
    metadata_path = RELEASE_METADATA.relative_to(ROOT).as_posix()
    try:
        previous = json.loads(git("show", f"{args.base}:{metadata_path}"))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return 0
    changed = git(
        "diff", "--name-only", args.base, "--", *PACK_INPUT_SCOPES
    ).splitlines()
    if changed and previous.get("version") == current["version"]:
        raise SystemExit(
            "pack inputs changed without a new pack-release.json version: "
            + ", ".join(changed)
        )
    print(
        json.dumps(
            {
                "changed_inputs": changed,
                "previous_version": previous.get("version"),
                "current_version": current["version"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
