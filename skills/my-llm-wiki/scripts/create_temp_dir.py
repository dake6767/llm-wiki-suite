#!/usr/bin/env python3
"""Create one fresh temp directory and print its native absolute path."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path, PureWindowsPath


_SAFE_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def validate_prefix(prefix: str) -> str:
    """Keep the caller-controlled prefix a name, never a path."""
    if not _SAFE_PREFIX.fullmatch(prefix):
        raise ValueError(
            "prefix must be 1-64 ASCII letters, digits, dots, underscores, or hyphens"
        )
    return prefix


def create_temp_dir(prefix: str, *, parent: Path | None = None) -> Path:
    """Allocate a unique directory under the host's real system temp root."""
    validate_prefix(prefix)
    directory = str(parent.resolve()) if parent is not None else None
    return Path(tempfile.mkdtemp(prefix=prefix, dir=directory)).resolve()


def display_path(
    path: str | os.PathLike[str], *, os_name: str | None = None
) -> str:
    """Use a drive-qualified, Git-Bash-safe spelling for native Windows tools."""
    raw = os.fspath(path)
    if (os_name or os.name) == "nt":
        return PureWindowsPath(raw).as_posix()
    return raw


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prefix",
        default="llmwiki-",
        help="safe directory-name prefix (default: llmwiki-)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workspace = create_temp_dir(args.prefix)
    except (OSError, ValueError) as exc:
        print(f"create_temp_dir: {exc}", file=sys.stderr)
        return 1
    print(display_path(workspace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
