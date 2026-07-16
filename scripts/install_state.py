"""Shared, strict install-state primitives used by install and doctor."""

from __future__ import annotations

import hashlib
import contextlib
import json
import os
import shutil
from pathlib import Path


MANIFEST = ".llm-wiki-install.json"
RUNTIME_NAMES = {".git", "__pycache__", ".DS_Store", "data", "reports", MANIFEST}


class LockUnavailable(RuntimeError):
    pass


@contextlib.contextmanager
def advisory_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise LockUnavailable(f"another operation is active: {path}") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise LockUnavailable(f"another operation is active: {path}") from exc
        yield
    finally:
        handle.close()


def is_linklike(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    try:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & 0x400)


def content_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if any(part in RUNTIME_NAMES for part in relative.parts):
            continue
        label = relative.as_posix().encode("utf-8")
        if path.is_symlink():
            digest.update(b"L\0" + label + b"\0" + os.readlink(path).encode("utf-8") + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + label + b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        elif path.is_dir():
            digest.update(b"D\0" + label + b"\0")
    return digest.hexdigest()


def verified_copy(path: Path, slug: str, pack_version: str, source_digest: str) -> bool:
    if not path.is_dir() or is_linklike(path) or not (path / "SKILL.md").is_file():
        return False
    try:
        manifest = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
        installed_digest = content_digest(path)
    except (OSError, json.JSONDecodeError):
        return False
    return (
        manifest.get("schema") == 1
        and manifest.get("slug") == slug
        and manifest.get("pack_version") == pack_version
        and manifest.get("source_digest") == source_digest
        and installed_digest == source_digest
    )


def remove_path(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif is_linklike(path):
        os.rmdir(path)
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
