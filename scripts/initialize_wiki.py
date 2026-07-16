#!/usr/bin/env python3
"""Ensure the suite has a usable first Wiki before post-install doctor runs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "registry" / "bootstrap.json"
INIT_WIKI = ROOT / "skills" / "my-llm-wiki" / "scripts" / "init_wiki.py"

sys.path.insert(0, str(ROOT / "scripts"))
import install  # noqa: E402


class WikiInitializationError(RuntimeError):
    pass


def load_registry(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WikiInitializationError(
            f"cannot read Wiki registry {path}: {exc}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("wikis"), list):
        raise WikiInitializationError(f"invalid Wiki registry shape: {path}")
    if any(not isinstance(entry, dict) for entry in data["wikis"]):
        raise WikiInitializationError(f"invalid Wiki registry entry: {path}")
    return data["wikis"]


def registry_path(config: dict) -> Path:
    raw = os.environ.get("LLM_WIKI_REGISTRY") or config.get("wiki_registry_path")
    if not isinstance(raw, str) or not raw:
        raise WikiInitializationError("bootstrap.json has no wiki_registry_path")
    return install.expand(raw)


def default_wiki_root(config: dict) -> Path:
    raw = config.get("default_wiki_root")
    if not isinstance(raw, str) or not raw:
        raise WikiInitializationError("bootstrap.json has no default_wiki_root")
    return install.expand(raw)


def ready_wikis(entries: list[dict]) -> list[Path]:
    ready: list[Path] = []
    for entry in entries:
        raw = entry.get("path")
        if not isinstance(raw, str) or not raw:
            continue
        path = install.expand(raw)
        if (path / "schema.md").is_file() and (path / "wiki").is_dir():
            ready.append(path)
    return ready


def ensure_wiki(config: dict, *, dry_run: bool = False) -> Path:
    registry = registry_path(config)
    root = default_wiki_root(config)
    existing = ready_wikis(load_registry(registry))
    if existing:
        print("wiki-init: existing")
        print(f"wiki: {existing[0]}")
        print(f"registry: {registry}")
        return existing[0]

    if dry_run:
        print("wiki-init: planned")
        print(f"wiki: {root}")
        print(f"registry: {registry}")
        return root

    if not INIT_WIKI.is_file():
        raise WikiInitializationError(f"missing Wiki initializer: {INIT_WIKI}")

    print("wiki-init: initializing", flush=True)
    try:
        subprocess.run(
            [
                sys.executable,
                str(INIT_WIKI),
                "--path",
                str(root),
                "--name",
                root.name,
                "--default",
            ],
            check=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise WikiInitializationError(
            "Wiki initialization timed out after 30 seconds"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise WikiInitializationError(
            f"Wiki initializer exited with status {exc.returncode}"
        ) from exc

    initialized = ready_wikis(load_registry(registry))
    try:
        initialized_root = next(path for path in initialized if path == root)
    except StopIteration as exc:
        raise WikiInitializationError(
            f"Wiki files or registry entry were not created for {root}"
        ) from exc

    print("wiki-init: ready")
    print(f"wiki: {initialized_root}")
    print(f"registry: {registry}")
    return initialized_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report without writing"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = install.load_json(BOOTSTRAP)
        install.require_protocol(config)
        install.require_python(config)
        if args.dry_run:
            ensure_wiki(config, dry_run=True)
        else:
            with install.install_lock(config):
                ensure_wiki(config)
    except (install.InstallError, WikiInitializationError, OSError) as exc:
        print(f"wiki-init: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
