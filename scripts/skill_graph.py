#!/usr/bin/env python3
"""Resolve selected skills, dependency roles, and capability profiles.

The registry has two different edges:

- ``requires`` installs runtime support but does not enable that dependency's
  user-facing feature profiles.
- ``bundles`` installs companion feature skills and does enable their profiles.

Keeping those roles explicit prevents a leaf such as ``my-llm-wiki-x`` from
inheriting unrelated Web/doc/video toolchain checks merely because it requires
the shared ``my-llm-wiki`` runtime.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY = REPO_ROOT / "registry" / "skills.json"
ROLE_ORDER = {"required": 0, "bundled": 1, "requested": 2}


class SkillGraphError(ValueError):
    """Raised when the registry or a requested selection is invalid."""


def load_registry(path: Path = DEFAULT_REGISTRY) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface parse/read failures verbatim
        raise SkillGraphError(f"cannot read skill registry {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
        raise SkillGraphError(f"invalid skill registry shape: {path}")
    return data


def _active_index(data: dict) -> tuple[list[dict], dict[str, dict]]:
    active = [s for s in data["skills"] if s.get("lifecycle", "active") == "active"]
    by_slug: dict[str, dict] = {}
    for skill in active:
        slug = skill.get("slug")
        if not isinstance(slug, str) or not slug:
            raise SkillGraphError("active skill missing a non-empty slug")
        if slug in by_slug:
            raise SkillGraphError(f"duplicate active skill slug: {slug}")
        if not isinstance(skill.get("collection_path"), str):
            raise SkillGraphError(f"{slug} missing collection_path")
        for field in ("requires", "bundles", "capabilities", "runtime_capabilities"):
            value = skill.get(field, [])
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise SkillGraphError(f"{slug}.{field} must be a list of strings")
        by_slug[slug] = skill

    for slug, skill in by_slug.items():
        for field in ("requires", "bundles"):
            for related in skill.get(field, []):
                if related not in by_slug:
                    raise SkillGraphError(
                        f"{slug}.{field} references unknown/inactive skill: {related}"
                    )
    return active, by_slug


def resolve_selection(data: dict, requested: list[str] | None = None) -> dict:
    """Return selected skills with roles plus the effective capability profiles."""
    active, by_slug = _active_index(data)
    requested = list(dict.fromkeys(requested or []))
    unknown = sorted(set(requested) - set(by_slug))
    if unknown:
        raise SkillGraphError("unknown or inactive skill slug(s): " + ", ".join(unknown))

    default_all = not requested
    seeds = [s["slug"] for s in active] if default_all else requested
    roles: dict[str, set[str]] = {}
    pending = [(slug, "requested", True) for slug in seeds]
    seen: set[tuple[str, str, bool]] = set()

    while pending:
        slug, role, expand_bundles = pending.pop()
        state = (slug, role, expand_bundles)
        if state in seen:
            continue
        seen.add(state)
        roles.setdefault(slug, set()).add(role)
        skill = by_slug[slug]
        for dependency in skill.get("requires", []):
            pending.append((dependency, "required", False))
        if expand_bundles:
            for companion in skill.get("bundles", []):
                pending.append((companion, "bundled", True))

    selected = []
    profiles: list[str] = []
    for skill in active:
        slug = skill["slug"]
        if slug not in roles:
            continue
        role_set = roles[slug]
        role = max(role_set, key=ROLE_ORDER.__getitem__)
        feature_enabled = role in ("requested", "bundled")
        capabilities = list(skill.get("capabilities", [])) if feature_enabled else []
        runtime_capabilities = list(skill.get("runtime_capabilities", []))
        for profile in capabilities + runtime_capabilities:
            if profile not in profiles:
                profiles.append(profile)
        selected.append({
            "slug": slug,
            "role": role,
            "roles": sorted(role_set, key=ROLE_ORDER.__getitem__, reverse=True),
            "feature_enabled": feature_enabled,
            "collection_path": skill["collection_path"],
            "capabilities": capabilities,
            "runtime_capabilities": runtime_capabilities,
        })

    return {
        "default_all": default_all,
        "requested": seeds,
        "skills": selected,
        "feature_skills": [
            s["slug"] for s in selected
            if s["capabilities"] or s["runtime_capabilities"]
        ],
        "profiles": profiles,
    }


def _install_tsv(selection: dict, repo_root: Path) -> str:
    lines = []
    for skill in selection["skills"]:
        src = (repo_root / skill["collection_path"]).resolve()
        lines.append(f"{skill['slug']}\t{skill['role']}\t{src.as_posix()}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve skill dependency and capability roles.")
    ap.add_argument("slugs", nargs="*", help="requested skill slugs; omit for all active")
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    ap.add_argument("--format", choices=("json", "install-tsv", "profiles"), default="json")
    args = ap.parse_args()

    try:
        data = load_registry(args.registry)
        selection = resolve_selection(data, args.slugs)
    except SkillGraphError as exc:
        print(f"skill graph error: {exc}", file=sys.stderr)
        return 2

    if args.format == "install-tsv":
        repo_root = args.registry.resolve().parent.parent
        print(_install_tsv(selection, repo_root))
    elif args.format == "profiles":
        print("\n".join(selection["profiles"]))
    else:
        print(json.dumps(selection, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
