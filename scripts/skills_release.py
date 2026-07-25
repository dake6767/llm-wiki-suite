#!/usr/bin/env python3
"""Read and validate the metadata that versions the Skills Pack.

`registry/skills-release.json` is the single source of the Skills Pack version.
Everything downstream — the published `skills-version.json`, the immutable
`skills-v<version>` release, and the baseline version the Browser embeds — reads
it from here rather than keeping a second copy of the number.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SKILLS_RELEASE = ROOT / "registry" / "skills-release.json"
SKILLS_DIR = ROOT / "skills"
VERSION_RE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
# The notes are display-only text that reaches an untrusted-data path in the app.
# Bound them here so no consumer has to.
NOTES_MAX = 2000


class SkillsReleaseError(RuntimeError):
    pass


def load_metadata(path: Path | str | None = None) -> dict:
    source = Path(path) if path else SKILLS_RELEASE
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillsReleaseError(f"cannot read {source}: {error}") from error
    if data.get("schema") != 1:
        raise SkillsReleaseError(f"unsupported skills release schema: {source}")
    version = data.get("pack_version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise SkillsReleaseError(f"pack_version must be X.Y.Z, got {version!r}")
    notes = data.get("pack_notes")
    if notes is not None and (not isinstance(notes, str) or len(notes) > NOTES_MAX):
        raise SkillsReleaseError(
            f"pack_notes must be text of at most {NOTES_MAX} characters"
        )
    floor = data.get("min_app_version")
    if floor is not None and (
        not isinstance(floor, str) or not VERSION_RE.fullmatch(floor)
    ):
        raise SkillsReleaseError(f"min_app_version must be X.Y.Z, got {floor!r}")
    return data


def version_key(value: str) -> tuple[int, int, int]:
    if not VERSION_RE.fullmatch(value):
        raise SkillsReleaseError(f"invalid version: {value!r}")
    major, minor, patch = value.split(".")
    return (int(major), int(minor), int(patch))


def main() -> int:
    print(json.dumps(load_metadata(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
