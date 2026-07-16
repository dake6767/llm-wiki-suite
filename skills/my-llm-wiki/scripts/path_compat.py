"""Normalize paths received from shells before native Python resolves them."""

from __future__ import annotations

import os
import re
from pathlib import Path


_MSYS_DRIVE_PATH = re.compile(r"^/([A-Za-z])(?:/(.*))?$")


def native_path_text(value: str | os.PathLike[str], *, os_name: str | None = None) -> str:
    """Translate Git-Bash ``/c/...`` paths for native Windows Python.

    Git Bash expands ``~`` to an MSYS path before invoking a native executable.
    ``pathlib`` otherwise interprets ``/c/Users/...`` as ``C:\\c\\Users\\...``.
    """
    raw = os.fspath(value)
    if (os_name or os.name) != "nt":
        return raw
    match = _MSYS_DRIVE_PATH.match(raw.replace("\\", "/"))
    if not match:
        return raw
    drive, rest = match.groups()
    return f"{drive.upper()}:/{rest or ''}"


def native_path(value: str | os.PathLike[str]) -> Path:
    return Path(native_path_text(value)).expanduser()
