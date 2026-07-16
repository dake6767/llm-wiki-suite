#!/usr/bin/env python3
"""User-level registry of LLM-WIKI repositories — the table that lets the
skill auto-route a capture to the right wiki by topic.

A user can keep several wikis (a tech one, a life one, …). To decide where a
captured source belongs, the agent needs to know *which wikis exist* and *what
each is about*. This script owns that table. It is deterministic plumbing —
listing, registering, removing entries — and makes **no** routing judgment
itself; the agent reads `list` and classifies (see SKILL.md §1).

Registry file (shared across every agent for the same OS user — register once,
both Claude and Hermes route correctly):

    ~/.my-llm-wiki/wikis.json   (override with $LLM_WIKI_REGISTRY)

    {
      "version": 1,
      "wikis": [
        {"path": "/Users/me/llm-wiki", "name": "技术",
         "description": "AI / 编程 / 工具", "default": true},
        {"path": "/Users/me/llm-wiki-renwen", "name": "人文",
         "description": "影视 / 文学 / 文化评论 / 历史", "default": false}
      ]
    }

`description` is the one line the agent classifies against. It is the single
source of truth for routing — the resolver never opens each wiki's purpose.md.

CLI:
    wikis.py list [--json]
    wikis.py register --path P --name N --description D [--default]
    wikis.py remove --path P

Importable helpers (used by init_wiki.py / normalize_raw.py):
    register(path, name, description, default=False) -> dict
    default_path() -> Path | None      # the default wiki, if it exists on disk
    registered_paths(existing_only=True) -> list[Path]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from path_compat import native_path

VERSION = 1


def registry_path() -> Path:
    # Explicit override, shared with the desktop app ($LLM_WIKI_REGISTRY).
    env = os.environ.get("LLM_WIKI_REGISTRY")
    if env:
        return native_path(env)
    return Path.home() / ".my-llm-wiki" / "wikis.json"


def _norm(path: str | Path) -> str:
    """Canonical key for a wiki: absolute, user-expanded, symlink-resolved."""
    return str(native_path(path).resolve())


def load() -> dict:
    """Read the registry. Missing file → empty registry. A corrupt file is
    reported on stderr and treated as empty rather than crashing a capture
    (the agent can re-register), but is never silently overwritten until the
    next explicit save()."""
    p = registry_path()
    if not p.exists():
        return {"version": VERSION, "wikis": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"warning: registry unreadable ({p}): {e}", file=sys.stderr)
        return {"version": VERSION, "wikis": []}
    if not isinstance(data, dict) or not isinstance(data.get("wikis"), list):
        print(f"warning: registry malformed ({p}) — ignoring", file=sys.stderr)
        return {"version": VERSION, "wikis": []}
    return data


def save(data: dict) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write so a crash can't truncate the registry.
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".wikis-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def register(path: str | Path, name: str, description: str, default: bool = False) -> dict:
    """Upsert a wiki by path. Setting default clears it on every other entry.
    The very first wiki registered becomes the default automatically (a lone
    wiki is the natural default), so a single-wiki setup needs no flag."""
    key = _norm(path)
    data = load()
    wikis = data.setdefault("wikis", [])

    entry = next((w for w in wikis if _norm(w.get("path", "")) == key), None)
    # First wiki is the natural default; an existing default stays default when
    # you re-register just to edit its description (only --default on another
    # wiki moves it).
    make_default = default or len(wikis) == 0 or bool(entry and entry.get("default"))

    if entry is None:
        entry = {"path": key}
        wikis.append(entry)
    entry["path"] = key
    entry["name"] = name
    entry["description"] = description
    entry["default"] = bool(make_default)

    if make_default:
        for w in wikis:
            if w is not entry:
                w["default"] = False

    data["version"] = VERSION
    save(data)
    return entry


def remove(path: str | Path) -> bool:
    """Drop a wiki from the registry (does NOT touch the wiki on disk).
    Returns True if an entry was removed."""
    key = _norm(path)
    data = load()
    wikis = data.get("wikis", [])
    kept = [w for w in wikis if _norm(w.get("path", "")) != key]
    removed = len(kept) != len(wikis)
    if removed:
        # If we removed the default and others remain, promote the first.
        if not any(w.get("default") for w in kept) and kept:
            kept[0]["default"] = True
        data["wikis"] = kept
        save(data)
    return removed


def default_path() -> Path | None:
    """The default wiki's path if it is registered AND exists on disk, else
    None. Routing only ever auto-targets a real directory."""
    for w in load().get("wikis", []):
        if w.get("default"):
            p = Path(w["path"])
            return p if p.is_dir() else None
    return None


def registered_paths(existing_only: bool = True) -> list[Path]:
    out = []
    for w in load().get("wikis", []):
        p = Path(w.get("path", ""))
        if not existing_only or p.is_dir():
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_list(args) -> None:
    data = load()
    wikis = data.get("wikis", [])
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"registry: {registry_path()}")
    print(f"count: {len(wikis)}")
    if not wikis:
        print("wikis: []   # none registered yet — see SKILL.md §0 to register")
        return
    print("wikis:")
    for w in wikis:
        exists = Path(w.get("path", "")).is_dir()
        print(f"  - name: {w.get('name','')}")
        print(f"    path: {w.get('path','')}")
        print(f"    description: {w.get('description','')}")
        print(f"    default: {str(bool(w.get('default'))).lower()}")
        print(f"    exists: {str(exists).lower()}")


def _cmd_register(args) -> None:
    entry = register(args.path, args.name, args.description, args.default)
    print("status: registered")
    print(f"registry: {registry_path()}")
    print(f"  name: {entry['name']}")
    print(f"  path: {entry['path']}")
    print(f"  description: {entry['description']}")
    print(f"  default: {str(entry['default']).lower()}")


def _cmd_remove(args) -> None:
    ok = remove(args.path)
    print(f"status: {'removed' if ok else 'not-found'}")
    print(f"path: {_norm(args.path)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Manage the LLM-WIKI registry (auto-routing table).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list registered wikis (agent reads this to classify)")
    p_list.add_argument("--json", action="store_true", help="emit raw registry JSON")
    p_list.set_defaults(func=_cmd_list)

    p_reg = sub.add_parser("register", help="add or update a wiki by path (upsert)")
    p_reg.add_argument("--path", required=True, help="wiki root")
    p_reg.add_argument("--name", required=True, help="human label, e.g. 技术 / 人文")
    p_reg.add_argument("--description", required=True,
                       help="one line: what content belongs here (the agent classifies against this)")
    p_reg.add_argument("--default", action="store_true",
                       help="make this the default wiki (clears default on others)")
    p_reg.set_defaults(func=_cmd_register)

    p_rm = sub.add_parser("remove", help="drop a wiki from the registry (leaves files on disk)")
    p_rm.add_argument("--path", required=True, help="wiki root to deregister")
    p_rm.set_defaults(func=_cmd_remove)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
