#!/usr/bin/env python3
"""Scaffold a new LLM-WIKI repository, compatible with the open-source
`llm_wiki` app (https://github.com/, 10k+ stars) and Obsidian.

When the my-llm-wiki skill is reused on a fresh machine or a new agent, there's
often no wiki yet. This creates a project the `llm_wiki` desktop app can open
directly and that behaves well as an Obsidian vault — so RAW ingestion and the
LLM-generated wiki layer share one well-known home.

Layout produced under <path> (mirrors the app's own `create_project`):

  .llm-wiki/project.json   stable identity { "id": <uuid>, "createdAt": <ms> }
  .obsidian/               app.json / appearance.json / core-plugins.json
  schema.md                the Schema layer — conventions any agent follows
  purpose.md               what this wiki is for (you fill in)
  raw/sources/             immutable RAW captures (this skill writes here)
  raw/assets/              shared media folder (Obsidian attachment path)
  wiki/                    LLM-generated layer
    index.md  log.md  overview.md
    entities/ concepts/ sources/ queries/ comparisons/ synthesis/

The app treats a folder as a valid project when it has `schema.md` + `wiki/`;
project.json is written here too (the app would otherwise create it lazily) so
identity is stable from the first capture.

Idempotent: if the repo is already a wiki (schema.md present), it does nothing
and reports status: exists. Existing schema.md / purpose.md are never overwritten.

Registration: every run also upserts this wiki into the shared registry
(wikis.py) — *even on the idempotent "exists" path* — so re-running with a new
`--description` is how you (re)register an already-built wiki for auto-routing.
Pass `--description` (one line: what belongs here) and optionally `--default`.

For an additional wiki, prefer `--slug <directory-name>` over an arbitrary
`--path`: the script resolves the collection root selected during initial Setup
and creates the new repository beside the original wiki.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
# THIS file is the schema every new wiki actually gets — init copies it verbatim
# and never overwrites it afterward, so a wiki's conventions are frozen at the
# template's state on its creation day. `my-llm-wiki-maintainer` ships a second
# copy at `assets/templates/schema.md` for its own Initialize flow; the two must
# stay byte-identical. They silently diverged once (2026-06-24 → 2026-07-01): a
# Tag & Domain Policy fix went into the maintainer copy only, which no code path
# reads, so every wiki initialized for the next month still got the pre-fix
# schema and ingest kept emitting `tags: []`. Edit both, or neither.
SCHEMA_TEMPLATE = SKILL_DIR / "assets" / "schema.md"

# wikis.py is a sibling in scripts/ — make sure it imports regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import wikis  # type: ignore
except ImportError:  # pragma: no cover — scaffolding still works without it
    wikis = None  # type: ignore
from path_compat import native_path

WIKI_SUBDIRS = ("entities", "concepts", "sources", "queries", "comparisons", "synthesis")

PURPOSE_MD = """# Project Purpose

## Goal

<!-- What are you trying to understand or build? -->

## Key Questions

<!-- List the primary questions driving this research -->

1.
2.
3.

## Scope

<!-- What is in scope? What is explicitly out of scope? -->

**In scope:**
-

**Out of scope:**
-

## Thesis

<!-- Your current working hypothesis or conclusion (update as research progresses) -->

> TBD
"""

INDEX_MD = """# Wiki Index

## Entities

## Concepts

## Sources

## Queries

## Comparisons

## Synthesis
"""

# Exactly four fields, and deliberately no `tags` / `related` — overview is the
# one page type the contract exempts from tagging (project-protocol.md "Refresh
# Overview", lint-query-save.md's overview SOP, and schema.md's Tag Policy all
# say so, and lint skips overview.md for that reason). This template used to
# emit `tags: []` + `related: []`, which contradicted all three and left every
# wiki's overview carrying an empty tag set it was never supposed to have.
OVERVIEW_MD = """---
type: overview
title: Project Overview
created: {today}
updated: {today}
---

# Overview

<!-- Provide a high-level summary of what this wiki covers and its current state. Update regularly as understanding deepens. -->
"""

OBSIDIAN_APP = {
    "attachmentFolderPath": "raw/assets",
    "userIgnoreFilters": [".cache", ".llm-wiki", ".superpowers"],
    "useMarkdownLinks": False,
    "newLinkFormat": "shortest",
    "showUnsupportedFiles": False,
}
OBSIDIAN_APPEARANCE = {"baseFontSize": 16, "theme": "obsidian"}
OBSIDIAN_CORE_PLUGINS = {
    "file-explorer": True,
    "global-search": True,
    "graph": True,
    "backlink": True,
    "tag-pane": True,
    "page-preview": True,
    "outgoing-link": True,
    "starred": True,
}


def _write(path: Path, text: str, created: list[str], *, keep_existing: bool = False) -> None:
    if keep_existing and path.exists():
        created.append(f"(kept existing {path})")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    created.append(str(path))


def main() -> None:
    ap = argparse.ArgumentParser(description="Initialize a new LLM-WIKI repo (llm_wiki / Obsidian compatible).")
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--path", help="explicit wiki root (use only when overriding the initialized collection)")
    target.add_argument("--slug", help="one directory name to create under the initialized wiki collection")
    ap.add_argument("--name", default="", help="human label for the wiki (default: dir name)")
    ap.add_argument("--description", required=True,
                    help="one line: what content belongs here — enables topic auto-routing")
    ap.add_argument("--default", action="store_true",
                    help="make this the default wiki in the registry")
    args = ap.parse_args()

    if not args.description.strip():
        ap.error("--description must be a non-empty topical routing scope")

    if args.slug is not None:
        if (
            not args.slug.strip()
            or args.slug != args.slug.strip()
            or any(separator in args.slug for separator in ("/", "\\"))
            or args.slug in {".", ".."}
        ):
            ap.error("--slug must be one directory name, not a path")
        if wikis is None:
            ap.error("--slug needs wikis.py to resolve the initialized collection root")
        collection = wikis.collection_root()
        if collection is None:
            ap.error(
                "cannot resolve the initialized wiki collection; pass --path only "
                "after confirming an explicit location"
            )
        root = (collection / args.slug).resolve()
    else:
        root = native_path(args.path).resolve()
    name = args.name or root.name
    today = time.strftime("%Y-%m-%d")

    # The app's definition of "already a project" is schema.md presence.
    if (root / "schema.md").exists():
        reg = _register(root, name, args)
        print(_summary("exists", root, name, [], note="already an LLM-WIKI repo — left untouched", reg=reg))
        return

    created: list[str] = []

    # Identity (the app reads this; creates it lazily if missing — we pre-write it).
    identity = {"id": str(uuid.uuid4()), "createdAt": int(time.time() * 1000)}
    _write(root / ".llm-wiki" / "project.json", json.dumps(identity, indent=2) + "\n", created)

    # Directory skeleton.
    for sub in ("raw/sources", "raw/assets", *(f"wiki/{d}" for d in WIKI_SUBDIRS)):
        d = root / sub
        d.mkdir(parents=True, exist_ok=True)
        keep = d / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")
        created.append(str(d) + "/")

    # Schema layer (from the bundled template) + purpose.
    if SCHEMA_TEMPLATE.exists():
        _write(root / "schema.md", SCHEMA_TEMPLATE.read_text(encoding="utf-8"), created, keep_existing=True)
    else:
        created.append(f"(schema template missing at {SCHEMA_TEMPLATE} — skipped schema.md)")
    purpose = PURPOSE_MD
    if args.description:
        purpose = PURPOSE_MD.replace(
            "## Goal\n\n<!-- What are you trying to understand or build? -->",
            f"## Goal\n\n{args.description}",
            1,
        )
    _write(root / "purpose.md", purpose, created, keep_existing=True)

    # Wiki layer seed files.
    _write(root / "wiki" / "index.md", INDEX_MD, created)
    _write(root / "wiki" / "log.md", f"# Research Log\n\n## {today}\n\n- Project created\n", created)
    _write(root / "wiki" / "overview.md", OVERVIEW_MD.format(today=today), created)

    # Obsidian vault config (graph view, attachment folder, ignore .llm-wiki).
    _write(root / ".obsidian" / "app.json", json.dumps(OBSIDIAN_APP, indent=2), created)
    _write(root / ".obsidian" / "appearance.json", json.dumps(OBSIDIAN_APPEARANCE, indent=2), created)
    _write(root / ".obsidian" / "core-plugins.json", json.dumps(OBSIDIAN_CORE_PLUGINS, indent=2), created)

    reg = _register(root, name, args)
    print(_summary("initialized", root, name, created, reg=reg))


def _register(root, name, args):
    """Upsert this wiki into the shared registry so it's known to auto-routing.
    Runs on every init (fresh or idempotent). Registration failure is terminal:
    a scaffold without a routing entry is not a completed Wiki creation."""
    if wikis is None:
        raise SystemExit(
            "error: Wiki files exist, but routing registration failed because "
            "wikis.py is unavailable"
        )
    try:
        entry = wikis.register(str(root), name, args.description, args.default)
    except Exception as e:  # surface the incomplete result instead of claiming success
        raise SystemExit(
            f"error: Wiki files exist, but routing registration failed: {e}"
        ) from e
    bits = [f"default={str(entry['default']).lower()}"]
    return f"{wikis.registry_path()}  ({', '.join(bits)})"


# Scaffolding used to end here, and in practice that is where the setup stopped:
# a wiki ran a month and 22 pages with purpose.md's Key Questions / Scope /
# Thesis still holding their placeholder text. purpose.md and schema.md are the
# only two per-wiki files an ingest agent reads on every single run, so blank
# sections there are not cosmetic — they cost relevance judgement on every page
# the agent writes. Hence an explicit next-steps block.
#
# Note what it does NOT ask for: a tag vocabulary. Someone creating a "政治" wiki
# knows the subject in one sentence and cannot enumerate its tags on day one;
# demanding a taxonomy up front produces a guessed one that fights the corpus.
# Tags grow from real content instead (`wiki_ops.py tags`), and `wiki_ops.py
# health` raises the domain table once there is enough material to see the areas.
NEXT_STEPS = """
next steps (purpose.md and schema.md are the only per-wiki files every ingest reads):
  1. fill in purpose.md — Key Questions, In/Out of scope, Thesis. One line each is
     plenty; it is what lets ingest judge "worth its own page" vs "a mention".
  2. leave schema.md's domain table empty for now. Areas are easier to name after
     a dozen-odd sources than on day one.
  3. leave tags alone entirely — the vocabulary grows from what you capture.
     `wiki_ops.py tags <root>` shows it; ingest reads it before tagging.
  4. run `wiki_ops.py health <root>` occasionally — it tells you when this wiki has
     enough content to be worth filling the domain table and refreshing overview.md.
"""


def _summary(status, root, name, created, note="", reg=""):
    lines = [f"status: {status}", f"wiki: {root}", f"name: {name}", "raw_dir: raw/sources"]
    if reg:
        lines.append(f"registered: {reg}")
    if note:
        lines.append(f"note: {note}")
    if created:
        lines.append("created:")
        lines += [f"  - {c}" for c in created]
    if status == "initialized":
        lines.append(NEXT_STEPS)
    return "\n".join(lines)


if __name__ == "__main__":
    main()
