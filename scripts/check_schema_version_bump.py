#!/usr/bin/env python3
"""Require a higher schema version whenever the wiki schema template changes.

`init_wiki.py` copies the schema template into a wiki once and never overwrites
it, so an existing wiki learns about template changes only by comparing version
markers. Edit the template's rules without raising the marker and the change is
invisible to `schema-upgrade` — it reaches no existing wiki, silently.

The unit test in `test_schema_template_sync.py` pins content to a digest table,
which catches an *accidental* edit. It cannot catch a deliberate one: changing
the template and the recorded digest together passes, because both live in the
working tree. Only a comparison against the base commit can tell "this template
changed in this PR" from "this template has always looked like that", so that
check lives here, in CI, where the base revision exists.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = (
    "skills/my-llm-wiki/assets/schema.md",
    "skills/my-llm-wiki-maintainer/assets/templates/schema.md",
)
VERSION_RE = re.compile(r"<!--\s*llm-wiki-schema-version:\s*(\d+)\s*-->")


def version_of(text: str) -> int:
    match = VERSION_RE.search(text)
    return int(match.group(1)) if match else 1


def blob_at(rev: str, path: str) -> str | None:
    """Exact file content at a revision — byte-for-byte, not stripped.

    Deliberately not routed through a trimming helper: `.strip()` drops the
    trailing newline every one of these files ends with, so every comparison
    against the working tree came back "changed" and the gate failed on commits
    that touched nothing.
    """
    try:
        return subprocess.check_output(
            ["git", "show", f"{rev}:{path}"], cwd=ROOT, text=True,
            stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return None  # not present in the base revision — a new file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    # A zero SHA is how CI spells "no base" (first push of a branch).
    if not args.base or set(args.base) == {"0"}:
        print("no base revision — skipping schema version-bump check")
        return 0

    failures: list[str] = []
    for rel in TEMPLATES:
        current_path = ROOT / rel
        if not current_path.is_file():
            failures.append(f"{rel}: missing")
            continue
        current = current_path.read_text(encoding="utf-8")
        base = blob_at(args.base, rel)
        if base is None or base == current:
            continue
        have, was = version_of(current), version_of(base)
        if have <= was:
            failures.append(
                f"{rel}: content changed but version is still v{have} "
                f"(base was v{was}). Existing wikis compare versions, not content, "
                f"so this edit would reach none of them — bump the marker in BOTH "
                f"templates and update KNOWN_SCHEMA_DIGESTS."
            )

    if failures:
        for line in failures:
            print(line)
        return 1
    print("schema template version-bump check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
