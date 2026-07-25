"""Normalize paths received from shells before native Python resolves them."""

from __future__ import annotations

import os
import re
from pathlib import Path


_MSYS_DRIVE_PATH = re.compile(r"^(?:/cygdrive)?/([A-Za-z])(?:/(.*))?$")
_MSYS_TEMP_PATH = re.compile(r"^/(?:tmp|var/tmp)(?:/|$)", re.IGNORECASE)


def reject_ambiguous_windows_temp_path(
    value: str | os.PathLike[str], *, os_name: str | None = None
) -> None:
    """Reject MSYS temp spellings that a native Windows child cannot map safely."""
    raw = os.fspath(value)
    if (os_name or os.name) == "nt" and _MSYS_TEMP_PATH.match(
        raw.replace("\\", "/")
    ):
        raise ValueError(
            f"ambiguous Git Bash temp path on Windows: {raw!r}; allocate the "
            "workspace with scripts/create_temp_dir.py and reuse its C:/... path"
        )


def native_path_text(value: str | os.PathLike[str], *, os_name: str | None = None) -> str:
    """Normalize shell paths before native Windows Python resolves them.

    Translate Git-Bash ``/c/...`` / ``/cygdrive/c/...`` drive paths. Reject
    ``/tmp`` spellings because their MSYS install-root mapping cannot be inferred
    safely after a shell-free native child-process boundary.
    """
    raw = os.fspath(value)
    if (os_name or os.name) != "nt":
        return raw
    reject_ambiguous_windows_temp_path(raw, os_name="nt")
    match = _MSYS_DRIVE_PATH.match(raw.replace("\\", "/"))
    if not match:
        return raw
    drive, rest = match.groups()
    return f"{drive.upper()}:/{rest or ''}"


def native_path(value: str | os.PathLike[str]) -> Path:
    return Path(native_path_text(value)).expanduser()
