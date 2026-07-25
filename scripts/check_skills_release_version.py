#!/usr/bin/env python3
"""Require a new Skills Pack version whenever the skills themselves change.

Without this, a shipped skill fix is indistinguishable from the previous release:
the Browser compares versions to decide whether to fetch, so an unbumped change
simply never reaches anyone.
"""

from __future__ import annotations

import argparse
import json
import subprocess

from .skills_release import (
    ROOT,
    SKILLS_RELEASE,
    SkillsReleaseError,
    load_metadata,
    version_key,
)


SKILLS_INPUT_SCOPES = (
    "skills",
    "registry/skills-release.json",
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
    current = load_metadata(SKILLS_RELEASE)
    metadata_path = SKILLS_RELEASE.relative_to(ROOT).as_posix()
    try:
        previous = json.loads(git("show", f"{args.base}:{metadata_path}"))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        # The base predates the version file; any valid current version counts.
        return 0
    changed = git(
        "diff", "--name-only", args.base, "--", *SKILLS_INPUT_SCOPES
    ).splitlines()
    if changed:
        try:
            previous_key = version_key(str(previous.get("pack_version")))
        except SkillsReleaseError:
            previous_key = (0, 0, 0)
        if version_key(current["pack_version"]) <= previous_key:
            raise SystemExit(
                "skills changed without a higher skills-release.json pack_version "
                f"(base {previous.get('pack_version')}): " + ", ".join(changed)
            )
    print(
        json.dumps(
            {
                "changed_inputs": changed,
                "previous_version": previous.get("pack_version"),
                "current_version": current["pack_version"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
