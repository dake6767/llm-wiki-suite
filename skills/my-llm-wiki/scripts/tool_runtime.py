#!/usr/bin/env python3
"""Resolve capability providers without coupling Skills to an installer.

The official toolchain is the preferred provider when Setup Core has activated
it. Users can override a capability with a system tool or an explicitly
configured custom provider. Skills remain usable when Browser and Setup Core do
not exist at all.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SETUP_STATE_ENV = "MY_LLM_WIKI_SETUP_STATE"
PROVIDERS_CONFIG_ENV = "MY_LLM_WIKI_PROVIDERS"
DEFAULT_SETUP_STATE = "~/.my-llm-wiki/setup-state.json"
DEFAULT_PROVIDERS_CONFIG = "~/.my-llm-wiki/providers.json"
ACTIVE_PYTHON_PROFILE_ENV = "MY_LLM_WIKI_ACTIVE_PYTHON_PROFILE"
ACTIVE_PROVIDER_ENV = "MY_LLM_WIKI_ACTIVE_PROVIDER"


class ToolRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedProvider:
    provider: str
    argv: list[str]
    environment: dict[str, str]
    source: str


def setup_state_path(path: Path | str | None = None) -> Path:
    raw = path or os.environ.get(SETUP_STATE_ENV) or DEFAULT_SETUP_STATE
    return Path(raw).expanduser().resolve()


def providers_config_path(path: Path | str | None = None) -> Path:
    raw = path or os.environ.get(PROVIDERS_CONFIG_ENV) or DEFAULT_PROVIDERS_CONFIG
    return Path(raw).expanduser().resolve()


def load_setup_state(path: Path | str | None = None) -> dict[str, Any] | None:
    state_path = setup_state_path(path)
    if not state_path.is_file():
        return None
    value = _load_object(state_path, "setup state")
    if value.get("schema") != 1 or not isinstance(value.get("packs"), dict):
        raise ToolRuntimeError(f"unsupported Setup Core state: {state_path}")
    return value


def load_provider_config(path: Path | str | None = None) -> dict[str, Any]:
    config_path = providers_config_path(path)
    if not config_path.is_file():
        return {"schema": 1, "policy": "official-preferred", "overrides": {}, "providers": {}}
    value = _load_object(config_path, "provider config")
    if (
        value.get("schema") != 1
        or value.get("policy", "official-preferred") != "official-preferred"
        or not isinstance(value.get("overrides", {}), dict)
        or not isinstance(value.get("providers", {}), dict)
    ):
        raise ToolRuntimeError(f"invalid provider config: {config_path}")
    return value


def resolve_command(
    name: str,
    *,
    capability: str | None = None,
    provider: str | None = None,
    state_path: Path | str | None = None,
    providers_path: Path | str | None = None,
) -> ResolvedProvider:
    _validate_name(name, "tool")
    config = load_provider_config(providers_path)
    state = load_setup_state(state_path)
    explicit = provider
    if explicit:
        resolved = _command_from(explicit, name, state, config)
        if resolved is None:
            raise ToolRuntimeError(
                f"requested provider {explicit!r} cannot supply tool {name!r}"
            )
        return resolved

    override = _saved_override(config, capability, name)
    candidates = _candidate_ids(config, override)
    failures: list[str] = []
    for provider_id in candidates:
        try:
            resolved = _command_from(provider_id, name, state, config)
        except ToolRuntimeError as exc:
            failures.append(f"{provider_id}: {exc}")
            continue
        if resolved is not None:
            return resolved
    detail = f" ({'; '.join(failures)})" if failures else ""
    raise ToolRuntimeError(
        f"no provider can supply tool {name!r}{detail}; configure a provider or install the official toolchain"
    )


def resolve_command_argv(
    name: str,
    *,
    capability: str | None = None,
    provider: str | None = None,
    state_path: Path | str | None = None,
    providers_path: Path | str | None = None,
) -> list[str]:
    return resolve_command(
        name,
        capability=capability,
        provider=provider,
        state_path=state_path,
        providers_path=providers_path,
    ).argv


def resolve_python_provider(
    profile: str,
    *,
    provider: str | None = None,
    state_path: Path | str | None = None,
    providers_path: Path | str | None = None,
) -> ResolvedProvider:
    _validate_name(profile, "Python profile")
    config = load_provider_config(providers_path)
    state = load_setup_state(state_path)
    if provider:
        resolved = _python_from(provider, profile, state, config)
        if resolved is None:
            raise ToolRuntimeError(
                f"requested provider {provider!r} cannot supply Python profile {profile!r}"
            )
        return resolved
    override = _saved_override(config, f"python.{profile}", profile)
    for provider_id in _candidate_ids(config, override):
        try:
            resolved = _python_from(provider_id, profile, state, config)
        except ToolRuntimeError:
            continue
        if resolved is not None:
            return resolved
    raise ToolRuntimeError(
        f"no provider can supply Python profile {profile!r}; run `my-llm-wiki ensure-pack {profile}` or configure a custom provider"
    )


def resolve_python(
    profile: str,
    *,
    provider: str | None = None,
    state_path: Path | str | None = None,
    providers_path: Path | str | None = None,
) -> str:
    return resolve_python_provider(
        profile,
        provider=provider,
        state_path=state_path,
        providers_path=providers_path,
    ).argv[0]


def runtime_env(
    profile: str,
    *,
    provider: str | None = None,
    state_path: Path | str | None = None,
    providers_path: Path | str | None = None,
) -> dict[str, str]:
    return resolve_python_provider(
        profile,
        provider=provider,
        state_path=state_path,
        providers_path=providers_path,
    ).environment


# Provider env vars that locate the pack's own code and must therefore win over
# anything the caller inherited. A stray PYTHONPATH in the launching shell — even
# an empty string, which some shells and agent runtimes export — otherwise
# shadows the pack's isolated `site` dir under plain setdefault and breaks every
# `import` in the re-exec'd interpreter.
_PACK_PATH_VARS = frozenset({"PYTHONPATH"})


def _merge_provider_environment(
    environment: MutableMapping[str, str], provider_env: dict[str, str]
) -> None:
    """Fold a Provider's declared environment into the re-exec environment.

    Path vars that point into the pack (PYTHONPATH) are authoritative: the pack
    value goes first, then any distinct non-empty inherited entries, so the
    pack's own code is always importable and the merge is idempotent across the
    self re-exec. Every other var stays user-overridable via setdefault.
    """
    for key, value in provider_env.items():
        if key in _PACK_PATH_VARS:
            inherited = environment.get(key, "")
            ordered: list[str] = []
            for part in [value, *inherited.split(os.pathsep)]:
                if part and part not in ordered:
                    ordered.append(part)
            environment[key] = os.pathsep.join(ordered)
        else:
            environment.setdefault(key, value)


def ensure_provider_python(profile: str, *, provider: str | None = None) -> None:
    """Re-exec an ASR entry point with the selected Provider's Python."""
    resolved = resolve_python_provider(profile, provider=provider)
    target = Path(resolved.argv[0]).resolve()
    try:
        current = Path(sys.executable).resolve()
    except OSError:
        current = Path(sys.executable)
    already_active = (
        current == target
        and os.environ.get(ACTIVE_PYTHON_PROFILE_ENV) == profile
        and os.environ.get(ACTIVE_PROVIDER_ENV) == resolved.provider
    )
    if already_active:
        _merge_provider_environment(os.environ, resolved.environment)
        return
    environment = os.environ.copy()
    environment[ACTIVE_PYTHON_PROFILE_ENV] = profile
    environment[ACTIVE_PROVIDER_ENV] = resolved.provider
    _merge_provider_environment(environment, resolved.environment)
    os.execve(
        str(target),
        [*resolved.argv, str(Path(sys.argv[0]).resolve()), *sys.argv[1:]],
        environment,
    )


def _command_from(
    provider_id: str,
    name: str,
    state: dict[str, Any] | None,
    config: dict[str, Any],
) -> ResolvedProvider | None:
    if provider_id == "official":
        return _official_entry(state, "commands", name)
    if provider_id == "system":
        executable = shutil.which(name)
        if not executable:
            return None
        return ResolvedProvider("system", [str(Path(executable).resolve())], {}, "PATH")
    return _custom_entry(config, provider_id, "commands", name)


def _python_from(
    provider_id: str,
    profile: str,
    state: dict[str, Any] | None,
    config: dict[str, Any],
) -> ResolvedProvider | None:
    if provider_id == "official":
        return _official_entry(state, "python_profiles", profile)
    if provider_id == "system":
        return None
    return _custom_entry(config, provider_id, "python_profiles", profile)


def _official_entry(
    state: dict[str, Any] | None, section: str, name: str
) -> ResolvedProvider | None:
    if state is None:
        return None
    packs = state.get("packs")
    if not isinstance(packs, dict):
        return None
    for pack_id in sorted(packs):
        pack = packs[pack_id]
        if not isinstance(pack, dict) or not isinstance(pack.get("artifact"), dict):
            continue
        artifact = pack["artifact"]
        entries = artifact.get(section, {})
        raw = entries.get(name) if isinstance(entries, dict) else None
        if raw is None:
            continue
        root = Path(str(pack.get("path", ""))).expanduser().resolve()
        argv = _validate_argv(raw, root=root, label=f"official/{pack_id}/{name}")
        environment = _environment(artifact.get("environment", {}).get(name, {}), root)
        return ResolvedProvider("official", argv, environment, f"pack:{pack_id}")
    return None


def _custom_entry(
    config: dict[str, Any], provider_id: str, section: str, name: str
) -> ResolvedProvider | None:
    providers = config.get("providers", {})
    spec = providers.get(provider_id) if isinstance(providers, dict) else None
    if not isinstance(spec, dict):
        return None
    entries = spec.get(section, {})
    raw = entries.get(name) if isinstance(entries, dict) else None
    if raw is None:
        return None
    argv = _validate_argv(raw, root=None, label=f"{provider_id}/{name}")
    environment = _environment(spec.get("environment", {}).get(name, {}), None)
    return ResolvedProvider(provider_id, argv, environment, f"config:{provider_id}")


def _validate_argv(raw: object, *, root: Path | None, label: str) -> list[str]:
    if not isinstance(raw, list) or not raw or any(
        not isinstance(value, str) or not value for value in raw
    ):
        raise ToolRuntimeError(f"invalid argv for {label}")
    values = [value.replace("{pack}", str(root)) if root else value for value in raw]
    executable = Path(values[0]).expanduser()
    if not executable.is_absolute() or not executable.is_file():
        raise ToolRuntimeError(f"provider executable is missing for {label}: {executable}")
    executable = executable.resolve()
    if root is not None and executable != root and root not in executable.parents:
        raise ToolRuntimeError(f"official executable escapes its pack for {label}: {executable}")
    values[0] = str(executable)
    return values


def _environment(raw: object, root: Path | None) -> dict[str, str]:
    if not isinstance(raw, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in raw.items()
    ):
        raise ToolRuntimeError("invalid provider environment")
    return {
        key: value.replace("{pack}", str(root)) if root else value
        for key, value in raw.items()
    }


def _saved_override(config: dict[str, Any], capability: str | None, name: str) -> str | None:
    overrides = config.get("overrides", {})
    for key in (capability, f"tool.{name}", name):
        value = overrides.get(key) if key and isinstance(overrides, dict) else None
        if isinstance(value, str) and value:
            return value
    return None


def _candidate_ids(config: dict[str, Any], override: str | None) -> list[str]:
    custom = sorted(str(value) for value in config.get("providers", {}))
    values = [override, "official", "system", *custom]
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _validate_name(value: str, label: str) -> None:
    if not value or any(character in value for character in "/\\"):
        raise ToolRuntimeError(f"invalid {label} name: {value!r}")


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolRuntimeError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ToolRuntimeError(f"invalid {label}: {path}")
    return value
