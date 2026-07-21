#!/usr/bin/env python3
"""Resolve Protocol 5 tools exclusively from the atomic install receipt."""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path


INSTALL_RECEIPT_ENV = "LLM_WIKI_INSTALL_RECEIPT"
DEFAULT_INSTALL_RECEIPT = "~/.my-llm-wiki/install-state.json"
_PLACEHOLDERS = {"home", "suite", "runtime"}


class ToolRuntimeError(RuntimeError):
    pass


def _system_name(system: str | None = None) -> str:
    return (system or platform.system()).lower()


def is_windows(system: str | None = None) -> bool:
    return _system_name(system) == "windows"


def setup_receipt_path(path: Path | str | None = None) -> Path:
    raw = path or os.environ.get(INSTALL_RECEIPT_ENV) or DEFAULT_INSTALL_RECEIPT
    return Path(raw).expanduser().resolve()


def load_setup_receipt(
    path: Path | str | None = None, *, system: str | None = None
) -> dict:
    receipt_path = setup_receipt_path(path)
    try:
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ToolRuntimeError(
            f"Protocol 5 managed install receipt is missing: {receipt_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ToolRuntimeError(f"invalid Protocol 5 receipt {receipt_path}: {exc}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != 1
        or value.get("protocol") != 5
    ):
        raise ToolRuntimeError(f"unsupported Protocol 5 receipt: {receipt_path}")
    if value.get("platform") != _system_name(system):
        raise ToolRuntimeError(f"install receipt belongs to another platform: {receipt_path}")
    return value


def _receipt_roots(receipt: dict) -> dict[str, str]:
    roots = {}
    for key in _PLACEHOLDERS:
        raw = receipt.get(key)
        if key == "runtime" and isinstance(raw, dict):
            raw = raw.get("path")
        if isinstance(raw, str) and raw:
            roots[key] = str(Path(raw).expanduser().resolve())
    if "home" not in roots:
        raise ToolRuntimeError("Protocol 5 receipt has no managed home")
    return roots


def _expand_arg(value: str, roots: dict[str, str]) -> str:
    for name, replacement in roots.items():
        value = value.replace("{" + name + "}", replacement)
    return value


def _validated_argv(raw: object, receipt: dict, label: str) -> list[str]:
    if not isinstance(raw, list) or not raw or any(
        not isinstance(arg, str) or not arg for arg in raw
    ):
        raise ToolRuntimeError(f"invalid managed argv for {label}")
    roots = _receipt_roots(receipt)
    argv = [_expand_arg(arg, roots) for arg in raw]
    executable = Path(argv[0]).expanduser()
    if not executable.is_absolute() or not executable.is_file():
        raise ToolRuntimeError(
            f"managed executable for {label} is missing: {executable}"
        )
    home = Path(roots["home"])
    resolved = executable.resolve()
    if resolved != home and home not in resolved.parents:
        raise ToolRuntimeError(
            f"managed executable for {label} escapes install home: {resolved}"
        )
    argv[0] = str(resolved)
    return argv


def resolve_command_argv(
    name: str,
    *,
    system: str | None = None,
    receipt_path: Path | str | None = None,
) -> list[str]:
    """Return the argv prefix used to invoke ``name``.

    A prefix may contain more than one item, for example bundled OpenCLI is
    ``[node.exe, opencli.js]`` and MarkItDown is
    ``[python.exe, -m, markitdown]``.
    """
    if not name or any(ch in name for ch in "/\\"):
        raise ToolRuntimeError(f"invalid tool name: {name!r}")
    receipt = load_setup_receipt(receipt_path, system=system)
    tools = receipt.get("tools")
    spec = tools.get(name) if isinstance(tools, dict) else None
    if not isinstance(spec, dict):
        component = name.replace("_", "-")
        raise ToolRuntimeError(
            f"managed tool {name!r} is not installed; repair component {component}"
        )
    return _validated_argv(spec.get("argv"), receipt, name)


def resolve_python(
    profile: str,
    *,
    system: str | None = None,
    receipt_path: Path | str | None = None,
) -> str:
    receipt = load_setup_receipt(receipt_path, system=system)
    profiles = receipt.get("python_profiles")
    raw = profiles.get(profile) if isinstance(profiles, dict) else None
    argv = _validated_argv([raw] if isinstance(raw, str) else raw, receipt, profile)
    if len(argv) != 1:
        raise ToolRuntimeError(f"managed Python profile {profile!r} is not an executable")
    return argv[0]


def runtime_env(
    profile: str,
    *,
    system: str | None = None,
    receipt_path: Path | str | None = None,
) -> dict[str, str]:
    receipt = load_setup_receipt(receipt_path, system=system)
    values = (receipt.get("runtime_env") or {}).get(profile, {})
    if not isinstance(values, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in values.items()
    ):
        raise ToolRuntimeError(f"invalid runtime environment for {profile}")
    return dict(values)


def ensure_managed_python(profile: str) -> None:
    """Re-exec an ASR entry point with its receipt-managed Python profile."""
    target = resolve_python(profile)
    try:
        same = Path(sys.executable).resolve() == Path(target).resolve()
    except OSError:
        same = False
    if same:
        for key, value in runtime_env(profile).items():
            os.environ.setdefault(key, value)
        return
    env = os.environ.copy()
    for key, value in runtime_env(profile).items():
        env.setdefault(key, value)
    os.execve(target, [target, str(Path(sys.argv[0]).resolve()), *sys.argv[1:]], env)
