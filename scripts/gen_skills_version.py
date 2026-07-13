#!/usr/bin/env python3
"""Generate the latest-side ``skills-version.json`` from ``registry/skills.json``.

doc 21 §2.2 / §9: the released version file is *generated* from the single source
of truth (``registry/skills.json`` top-level ``pack_version``) — we never maintain a
second copy of the version number. The publish CI runs this and uploads the result
to the rolling ``skills-latest`` release; the Worker proxies it and the desktop app
validates + displays it.

Fields (kept minimal, all others are rejected by the app as untrusted):
  schema         always ``1`` (app hard-requires ``=== 1``)
  pack_version   copied verbatim from registry (semver ``X.Y.Z``)
  source_commit  full 40-hex sha the release was cut from (agent ancestry check)
  released_at    ISO-8601 UTC timestamp
  pack_notes     display-only text (app shows as plain text, never into a prompt)

``source_commit`` defaults to ``$GITHUB_SHA``; ``released_at`` to now.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "registry" / "skills.json"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def build(registry_path: Path, source_commit: str | None, released_at: str | None) -> dict:
    data = json.loads(registry_path.read_text(encoding="utf-8"))

    pack_version = data.get("pack_version")
    if not isinstance(pack_version, str) or not SEMVER_RE.match(pack_version):
        raise SystemExit(
            f"registry pack_version must be semver X.Y.Z, got: {pack_version!r}"
        )

    out: dict = {
        "schema": 1,
        "pack_version": pack_version,
        "released_at": released_at or datetime.now(timezone.utc).isoformat(),
    }

    commit = source_commit or os.environ.get("GITHUB_SHA")
    if commit and SHA_RE.match(commit):
        out["source_commit"] = commit

    notes = data.get("pack_notes")
    if isinstance(notes, str) and notes.strip():
        # display-only, keep it bounded so the app never has to.
        out["pack_notes"] = notes.strip()[:2000]

    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--registry", type=Path, default=REGISTRY)
    ap.add_argument("--source-commit", default=None)
    ap.add_argument("--released-at", default=None)
    ap.add_argument(
        "-o", "--output", type=Path, default=None,
        help="write JSON here (default: stdout)",
    )
    args = ap.parse_args(argv)

    payload = build(args.registry, args.source_commit, args.released_at)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
