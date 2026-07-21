#!/usr/bin/env python3
"""Protocol 5 receipt-managed component archives for macOS and Linux."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable


SCHEMA = 2
PROTOCOL = 5
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ASSET_BYTES = 8 * 1024 * 1024 * 1024
MAX_RELEASE_PART_BYTES = 2_000_000_000
MAX_ASSET_PARTS = 16
MAX_INSTALLED_BYTES = 32 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 250_000
PYTHON_COMPONENTS = {"documents", "video", "asr-zh", "asr-other"}


class ComponentError(RuntimeError):
    pass


def platform_id() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system not in {"darwin", "linux"}:
        raise ComponentError(f"managed POSIX components do not support {system}")
    if machine in {"arm64", "aarch64"}:
        arch = "arm64"
    elif machine in {"x86_64", "amd64"}:
        arch = "x64"
    else:
        raise ComponentError(f"unsupported component architecture: {machine}")
    if system == "linux" and arch != "x64":
        raise ComponentError("released Linux components currently support x64 only")
    return system, arch


def sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ComponentError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ComponentError(f"{label} must be a JSON object")
    return value


def _manifest_asset(config: dict) -> tuple[str, str, list[str]]:
    spec = config.get("agent_installer") or {}
    tag = spec.get("release_tag")
    pattern = spec.get("component_manifest_asset")
    sources = spec.get("release_sources")
    if not isinstance(tag, str) or not tag:
        raise ComponentError("agent installer release tag is missing")
    if not isinstance(pattern, str) or not pattern:
        raise ComponentError("component manifest asset pattern is missing")
    if not isinstance(sources, list) or not sources:
        raise ComponentError("component release sources are missing")
    system, arch = platform_id()
    return tag, pattern.format(platform=system, arch=arch), list(sources)


def _validate_asset(spec: object, label: str) -> dict:
    if not isinstance(spec, dict):
        raise ComponentError(f"invalid {label} asset")
    required = ("version", "asset", "sha256", "size", "installed_size")
    for key in required:
        if key not in spec:
            raise ComponentError(f"{label} asset has no {key}")
    if not re.fullmatch(r"[0-9a-f]{64}", str(spec["sha256"])):
        raise ComponentError(f"{label} asset has an invalid SHA-256")
    if not isinstance(spec["size"], int) or not 0 < spec["size"] <= MAX_ASSET_BYTES:
        raise ComponentError(f"{label} asset has an invalid size")
    if (
        not isinstance(spec["installed_size"], int)
        or not 0 < spec["installed_size"] <= MAX_INSTALLED_BYTES
    ):
        raise ComponentError(f"{label} asset has an invalid installed size")
    if Path(str(spec["asset"])).name != str(spec["asset"]):
        raise ComponentError(f"{label} asset name is unsafe")
    parts = spec.get("parts")
    if parts is None and spec["size"] > MAX_RELEASE_PART_BYTES:
        raise ComponentError(f"{label} asset exceeds the release limit and must be split")
    if parts is not None:
        if not isinstance(parts, list) or not 2 <= len(parts) <= MAX_ASSET_PARTS:
            raise ComponentError(f"{label} asset parts are invalid")
        names = {str(spec["asset"])}
        total = 0
        for index, part in enumerate(parts, 1):
            if not isinstance(part, dict):
                raise ComponentError(f"{label} asset part {index} is invalid")
            name = str(part.get("asset", ""))
            digest = str(part.get("sha256", ""))
            size = part.get("size")
            if not name or Path(name).name != name or name in names:
                raise ComponentError(f"{label} asset part {index} name is unsafe")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ComponentError(f"{label} asset part {index} has an invalid SHA-256")
            if not isinstance(size, int) or not 0 < size <= MAX_RELEASE_PART_BYTES:
                raise ComponentError(f"{label} asset part {index} has an invalid size")
            names.add(name)
            total += size
        if total != spec["size"]:
            raise ComponentError(f"{label} asset part sizes differ from total size")
    return spec


def validate_manifest(value: dict) -> dict:
    system, arch = platform_id()
    if value.get("schema") != SCHEMA or value.get("protocol") != PROTOCOL:
        raise ComponentError("unsupported component manifest")
    if value.get("platform") != system or value.get("architecture") != arch:
        raise ComponentError("component manifest belongs to another platform")
    sources = value.get("sources")
    if not isinstance(sources, list) or not sources or any(
        not isinstance(row, str) or "{asset}" not in row for row in sources
    ):
        raise ComponentError("component manifest sources are invalid")
    _validate_asset(value.get("runtime"), "runtime")
    components = value.get("components")
    if not isinstance(components, dict):
        raise ComponentError("component manifest components are invalid")
    for component, spec in components.items():
        _validate_asset(spec, component)
        if not isinstance(spec.get("tools", {}), dict):
            raise ComponentError(f"component {component} tools are invalid")
    return value


def load_manifest(config: dict) -> tuple[dict | None, str]:
    """Read a local override or fetch the immutable release manifest."""
    override = os.environ.get("LLM_WIKI_COMPONENT_MANIFEST")
    if override:
        try:
            return validate_manifest(_read_json(Path(override), "component manifest")), ""
        except ComponentError as exc:
            return None, str(exc)
    try:
        tag, asset, sources = _manifest_asset(config)
    except ComponentError as exc:
        return None, str(exc)
    errors = []
    for template in sources:
        url = template.format(tag=tag, asset=asset)
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "my-llm-wiki-agent-installer/5"}
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(MAX_MANIFEST_BYTES + 1)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise ComponentError("manifest exceeds size limit")
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ComponentError("manifest is not an object")
            return validate_manifest(value), ""
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ComponentError) as exc:
            errors.append(f"{url}: {exc}")
    return None, "; ".join(errors)


def _installed_component(receipt: dict | None, component: str, spec: dict) -> bool:
    row = ((receipt or {}).get("components") or {}).get(component)
    if not isinstance(row, dict):
        return False
    path = Path(str(row.get("path", ""))).expanduser()
    return (
        row.get("sha256") == spec.get("sha256")
        and row.get("version") == spec.get("version")
        and path.is_dir()
        and (path / ".llm-wiki-component.json").is_file()
    )


def catalog(config: dict, receipt: dict | None) -> tuple[list[dict], dict | None, str]:
    manifest, error = load_manifest(config)
    lock_path = Path(__file__).resolve().parent.parent / "registry" / "agent-components.lock.json"
    lock = _read_json(lock_path, "component lock")
    rows = []
    for component, local in (lock.get("components") or {}).items():
        released = (manifest or {}).get("components", {}).get(component)
        installed = bool(released and _installed_component(receipt, component, released))
        rows.append(
            {
                "id": component,
                "label": local.get("label", component),
                "description": local.get("description", ""),
                "default": bool(local.get("default")),
                "status": "satisfied" if installed else "installable" if released else "blocked",
                "installed": installed,
                "unattended": bool(released),
                "version": (released or {}).get("version"),
                "download_size": (released or {}).get("size"),
                "installed_size": (released or {}).get("installed_size"),
                "blockers": [] if released else [error or "release component is unavailable"],
                "managed": True,
            }
        )
    return rows, manifest, error


def freeze_plan(manifest: dict, selected: list[str], receipt: dict | None) -> dict:
    components = manifest["components"]
    unknown = sorted(set(selected) - set(components))
    if unknown:
        raise ComponentError("unknown released component(s): " + ", ".join(unknown))
    items = []
    for component in selected:
        spec = json.loads(json.dumps(components[component]))
        spec["id"] = component
        spec["state"] = "satisfied" if _installed_component(receipt, component, spec) else "install"
        items.append(spec)
    runtime = None
    if set(selected) & PYTHON_COMPONENTS:
        runtime = json.loads(json.dumps(manifest["runtime"]))
        installed_runtime = (receipt or {}).get("runtime")
        runtime["state"] = (
            "satisfied"
            if isinstance(installed_runtime, dict)
            and installed_runtime.get("sha256") == runtime.get("sha256")
            and installed_runtime.get("version") == runtime.get("version")
            and Path(str(installed_runtime.get("path", ""))).is_dir()
            and _marker_matches(
                Path(str(installed_runtime.get("path", ""))), runtime, "runtime"
            )
            else "install"
        )
    pending = [row for row in items if row["state"] == "install"]
    pending_specs = [*pending, *([runtime] if runtime and runtime["state"] == "install" else [])]
    download_bytes = sum(int(row["size"]) for row in pending_specs)
    assembly_bytes = sum(int(row["size"]) for row in pending_specs if row.get("parts"))
    installed_bytes = sum(int(row["installed_size"]) for row in pending_specs)
    return {
        "release_tag": manifest.get("release_tag"),
        "platform": manifest["platform"],
        "architecture": manifest["architecture"],
        "sources": manifest["sources"],
        "runtime": runtime,
        "items": items,
        "disk": {
            "download_bytes": download_bytes,
            "assembly_bytes": assembly_bytes,
            "installed_bytes": installed_bytes,
            "required_bytes": download_bytes + assembly_bytes + installed_bytes,
        },
    }


def _download_file(asset_spec: dict, plan: dict, downloads: Path) -> Path:
    asset = str(asset_spec["asset"])
    expected_hash = str(asset_spec["sha256"])
    expected_size = int(asset_spec["size"])
    local_dir = os.environ.get("LLM_WIKI_COMPONENT_ASSET_DIR")
    if local_dir:
        source = Path(local_dir) / asset
        if not source.is_file():
            raise ComponentError(f"component asset is missing: {source}")
        if source.stat().st_size != expected_size or sha256_file(source) != expected_hash:
            raise ComponentError(f"component asset failed verification: {source}")
        return source

    target = downloads / asset
    if target.is_file() and target.stat().st_size == expected_size:
        if sha256_file(target) == expected_hash:
            return target
        target.unlink()
    errors = []
    for template in plan["sources"]:
        url = template.format(tag=plan["release_tag"], asset=asset)
        partial = downloads / f".{asset}.{uuid.uuid4().hex}.part"
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "my-llm-wiki-agent-installer/5"}
            )
            total = 0
            with urllib.request.urlopen(request, timeout=20) as response, partial.open("wb") as out:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > expected_size or total > MAX_ASSET_BYTES:
                        raise ComponentError("download exceeds declared size")
                    out.write(block)
            if total != expected_size or sha256_file(partial) != expected_hash:
                raise ComponentError("download failed SHA-256/size verification")
            os.replace(partial, target)
            return target
        except (OSError, urllib.error.URLError, ComponentError) as exc:
            errors.append(f"{url}: {exc}")
        finally:
            partial.unlink(missing_ok=True)
    raise ComponentError("all component sources failed: " + "; ".join(errors))


def _download(spec: dict, plan: dict, home: Path) -> Path:
    downloads = home / "downloads" / str(plan["release_tag"])
    downloads.mkdir(parents=True, exist_ok=True)
    target = downloads / str(spec["asset"])
    expected_size = int(spec["size"])
    expected_hash = str(spec["sha256"])
    if target.is_file() and target.stat().st_size == expected_size:
        if sha256_file(target) == expected_hash:
            return target
        target.unlink()
    if not spec.get("parts"):
        return _download_file(spec, plan, downloads)

    local_dir = os.environ.get("LLM_WIKI_COMPONENT_ASSET_DIR")
    if local_dir:
        combined = Path(local_dir) / str(spec["asset"])
        if combined.is_file():
            if combined.stat().st_size != expected_size or sha256_file(combined) != expected_hash:
                raise ComponentError(f"component asset failed verification: {combined}")
            return combined

    part_paths = [_download_file(part, plan, downloads) for part in spec["parts"]]
    partial = downloads / f".{spec['asset']}.{uuid.uuid4().hex}.assembling"
    digest = hashlib.sha256()
    total = 0
    try:
        with partial.open("wb") as destination:
            for part in part_paths:
                with part.open("rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        total += len(block)
                        if total > expected_size or total > MAX_ASSET_BYTES:
                            raise ComponentError("assembled component exceeds declared size")
                        digest.update(block)
                        destination.write(block)
        if total != expected_size or digest.hexdigest() != expected_hash:
            raise ComponentError("assembled component failed SHA-256/size verification")
        os.replace(partial, target)
        if not local_dir:
            for part in part_paths:
                part.unlink(missing_ok=True)
        return target
    finally:
        partial.unlink(missing_ok=True)


def _safe_extract(archive: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as bundle:
            root = destination.resolve()
            for info in bundle.infolist():
                target = (destination / info.filename).resolve()
                if target != root and root not in target.parents:
                    raise ComponentError(f"unsafe archive member: {info.filename}")
                mode = info.external_attr >> 16
                if mode & 0o170000 == 0o120000:
                    raise ComponentError(f"archive symlink is not allowed: {info.filename}")
            bundle.extractall(destination)
            for info in bundle.infolist():
                mode = (info.external_attr >> 16) & 0o777
                if mode and not info.is_dir():
                    (destination / info.filename).chmod(mode)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ComponentError(f"invalid component archive {archive}: {exc}") from exc


def _verify_expanded_size(archive: Path, spec: dict) -> None:
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ComponentError("component archive has too many members")
            actual = sum(info.file_size for info in members if not info.is_dir())
    except (OSError, zipfile.BadZipFile) as exc:
        raise ComponentError(f"invalid component archive {archive}: {exc}") from exc
    if actual != int(spec["installed_size"]):
        raise ComponentError(
            "component archive expanded size differs from the signed manifest: "
            f"expected {spec['installed_size']}, got {actual}"
        )


def _marker_matches(path: Path, spec: dict, kind: str) -> bool:
    marker = path / f".llm-wiki-{kind}.json"
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return value.get("sha256") == spec["sha256"] and value.get("version") == spec["version"]


def _activate_archive(archive: Path, target: Path, spec: dict, kind: str) -> None:
    if target.exists():
        if _marker_matches(target, spec, kind):
            return
        raise ComponentError(f"refusing to replace unverified managed path: {target}")
    _verify_expanded_size(archive, spec)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".stage-agent-{uuid.uuid4().hex}"
    stage.mkdir()
    try:
        _safe_extract(archive, stage)
        marker = {
            "schema": SCHEMA,
            "protocol": PROTOCOL,
            "kind": kind,
            "version": spec["version"],
            "asset": spec["asset"],
            "sha256": spec["sha256"],
        }
        (stage / f".llm-wiki-{kind}.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def _runtime_python(runtime: Path) -> Path:
    candidates = [runtime / "bin" / "python3", runtime / "bin" / "python"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ComponentError(f"managed Python executable is missing under {runtime}")


def _expand(values: list[str], component: Path, runtime: Path) -> list[str]:
    python = _runtime_python(runtime) if runtime.exists() else None
    replacements = {
        "{component}": str(component),
        "{runtime}": str(runtime),
        "{runtime_python}": str(python) if python else "",
    }
    result = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise ComponentError("component argv is invalid")
        for key, replacement in replacements.items():
            value = value.replace(key, replacement)
        result.append(value)
    return result


def _profile_launcher(component: Path, runtime: Path, profile: str) -> Path:
    python = _runtime_python(runtime)
    site = component / "site"
    target = component / f"python-{profile}"
    target.write_text(
        "#!/bin/sh\n"
        f"export PYTHONPATH={shlex.quote(str(site))}\n"
        f"exec {shlex.quote(str(python))} \"$@\"\n",
        encoding="utf-8",
    )
    target.chmod(0o755)
    return target


def _run(argv: list[str], timeout: int = 300) -> None:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ComponentError(f"component postcheck could not run: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
        raise ComponentError(f"component postcheck failed: {detail[-4000:]}")


def cleanup_staging(home: Path) -> None:
    for parent in (home / "runtime" / "versions", home / "components"):
        if not parent.is_dir():
            continue
        for path in parent.rglob(".stage-agent-*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)


def _existing_ancestor(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise ComponentError(f"cannot resolve a filesystem for {path}")
        candidate = parent
    return candidate


def _required_space(plan: dict, home: Path) -> dict[str, int]:
    pending = []
    runtime_spec = plan.get("runtime")
    if runtime_spec:
        target = home / "runtime" / "versions" / str(runtime_spec["version"])
        if not _marker_matches(target, runtime_spec, "runtime"):
            pending.append(runtime_spec)
    for item in plan.get("items", []):
        target = home / "components" / item["id"] / str(item["version"])
        if not _marker_matches(target, item, "component"):
            pending.append(item)
    download_bytes = sum(int(row["size"]) for row in pending)
    assembly_bytes = sum(int(row["size"]) for row in pending if row.get("parts"))
    installed_bytes = sum(int(row["installed_size"]) for row in pending)
    return {
        "download_bytes": download_bytes,
        "assembly_bytes": assembly_bytes,
        "installed_bytes": installed_bytes,
        "required_bytes": download_bytes + assembly_bytes + installed_bytes,
    }


def install_selected(
    plan: dict,
    home: Path,
    *,
    route: str = "global",
    emit: Callable[..., None] | None = None,
    stop_on_failure: bool = False,
) -> tuple[list[dict], dict | None, dict, dict, dict, bool, list[dict]]:
    """Install frozen assets and return receipt-ready component state."""
    emit = emit or (lambda *args, **kwargs: None)
    cleanup_staging(home)
    disk = _required_space(plan, home)
    free_bytes = shutil.disk_usage(_existing_ancestor(home)).free
    emit("disk-preflight", **disk, free_bytes=free_bytes)
    if free_bytes < disk["required_bytes"]:
        raise ComponentError(
            "insufficient free disk space for selected components: "
            f"need {disk['required_bytes']} bytes, have {free_bytes} bytes"
        )
    runtime_spec = plan.get("runtime")
    runtime_state = None
    runtime = home / "runtime" / "versions" / str((runtime_spec or {}).get("version", "none"))
    if runtime_spec:
        emit("runtime-start", version=runtime_spec["version"])
        archive = _download(runtime_spec, plan, home)
        _activate_archive(archive, runtime, runtime_spec, "runtime")
        _run([str(_runtime_python(runtime)), "-c", "import sys; print(sys.version)"])
        runtime_state = {
            key: runtime_spec[key]
            for key in ("version", "asset", "sha256", "size", "installed_size")
        }
        runtime_state["path"] = str(runtime)
        emit("runtime-complete", version=runtime_spec["version"])

    results: list[dict] = []
    tools: dict[str, dict] = {}
    profiles: dict[str, str] = {}
    runtime_env: dict[str, dict[str, str]] = {}
    manual_actions: list[dict] = []
    failed = False
    for item in plan.get("items", []):
        component = item["id"]
        if failed and stop_on_failure:
            results.append({"id": component, "state": "skipped-after-failure"})
            continue
        emit("component-start", component=component)
        target = home / "components" / component / str(item["version"])
        try:
            if item.get("state") != "satisfied" or not _marker_matches(target, item, "component"):
                archive = _download(item, plan, home)
                _activate_archive(archive, target, item, "component")
            component_tools = {}
            for name, spec in item.get("tools", {}).items():
                argv = _expand(spec["argv"], target, runtime)
                _run([*argv, *spec.get("postcheck", [])])
                component_tools[name] = {"argv": argv, "component": component}
                tools[name] = component_tools[name]
            component_profiles = {}
            profile = item.get("python_profile")
            if profile:
                launcher = _profile_launcher(target, runtime, profile)
                _run([str(launcher), *item.get("postcheck", [])], timeout=600)
                profiles[profile] = str(launcher)
                component_profiles[profile] = str(launcher)
                routes = item.get("runtime_env") or {}
                runtime_env[profile] = dict(routes.get(route) or routes.get("global") or {})
            extension_path = target / "extension"
            if component == "web" and extension_path.is_dir():
                manual_actions.append(
                    {
                        "id": "opencli-browser-bridge-load",
                        "component": "web",
                        "severity": "required",
                        "state": "pending",
                        "path": str(extension_path),
                        "instructions": [
                            "Open chrome://extensions",
                            "Enable Developer mode",
                            f"Choose Load unpacked and select {extension_path}",
                            "Run opencli doctor after loading",
                        ],
                        "verification": [*component_tools.get("opencli", {}).get("argv", ["opencli"]), "doctor"],
                    }
                )
            row = {
                "id": component,
                "state": "complete",
                "version": item["version"],
                "path": str(target),
                "asset": item["asset"],
                "sha256": item["sha256"],
                "size": item["size"],
                "installed_size": item["installed_size"],
                "tools": component_tools,
                "python_profiles": component_profiles,
                "runtime_env": runtime_env.get(profile, {}) if profile else {},
            }
            results.append(row)
            emit("component-complete", component=component, state="complete")
        except ComponentError as exc:
            failed = True
            results.append({"id": component, "state": "failed", "error": str(exc)})
            emit("component-complete", component=component, state="failed", error=str(exc))
    return results, runtime_state, tools, profiles, runtime_env, failed, manual_actions


def remove_owned(receipt: dict, home: Path, component_ids: set[str] | None = None) -> list[str]:
    removed = []
    rows = receipt.get("components") or {}
    for component, row in rows.items():
        if component_ids is not None and component not in component_ids:
            continue
        if not isinstance(row, dict):
            continue
        path = Path(str(row.get("path", ""))).expanduser()
        try:
            owned = path == home or home in path.resolve().parents
        except OSError:
            owned = False
        if owned and path.is_dir() and _marker_matches(path, row, "component"):
            shutil.rmtree(path)
            removed.append(str(path))
    if component_ids is None:
        runtime = receipt.get("runtime")
        if isinstance(runtime, dict):
            path = Path(str(runtime.get("path", ""))).expanduser()
            try:
                owned = home in path.resolve().parents
            except OSError:
                owned = False
            if owned and path.is_dir() and _marker_matches(path, runtime, "runtime"):
                shutil.rmtree(path)
                removed.append(str(path))
    return removed
