#!/usr/bin/env python3
"""Native Windows entry point for My LLM Wiki skills and tool components.

The release build freezes this module into ``My-LLM-Wiki-Setup.exe`` and
embeds four payload files: the suite, a private CPython runtime, the committed
upstream lock, and a release component manifest.  Windows has no legacy
fallback: this executable owns install, update, repair, component state, and
uninstall.  macOS and Linux never execute this module.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOME = "~/.my-llm-wiki"
SETUP_DIR = "setup"
RECEIPT_NAME = "install-state.json"
SETUP_EXE = "My-LLM-Wiki-Setup.exe"
MAX_DOWNLOAD = 2 * 1024 * 1024 * 1024
OFFICIAL_REMOTES = {
    "https://github.com/dake6767/llm-wiki-suite",
    "https://github.com/dake6767/llm-wiki-suite.git",
    "https://gitee.com/dake6767/llm-wiki-suite",
    "https://gitee.com/dake6767/llm-wiki-suite.git",
    "git@github.com:dake6767/llm-wiki-suite.git",
    "ssh://git@github.com/dake6767/llm-wiki-suite.git",
}


class SetupError(RuntimeError):
    pass


def emit(event: str, **values) -> None:
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def payload_dir(explicit: Path | None = None) -> Path:
    if explicit is not None:
        root = explicit.resolve()
    elif getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        root = Path(sys._MEIPASS) / "payload"
    else:
        configured = os.environ.get("LLM_WIKI_SETUP_PAYLOAD")
        root = Path(configured).expanduser().resolve() if configured else REPO_ROOT / ".setup-payload"
    required = {
        "suite.zip",
        "python.zip",
        "windows-toolchain.lock.json",
        "component-manifest.json",
        "setup-payload.json",
    }
    missing = sorted(name for name in required if not (root / name).is_file())
    if missing:
        raise SetupError(f"Setup payload is incomplete at {root}: {', '.join(missing)}")
    payload_manifest = load_json(root / "setup-payload.json")
    if payload_manifest.get("schema") != 1:
        raise SetupError("unsupported Setup payload manifest")
    files = payload_manifest.get("files")
    if not isinstance(files, dict):
        raise SetupError("Setup payload manifest has no files map")
    for name in sorted(required - {"setup-payload.json"}):
        expected = files.get(name)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise SetupError(f"Setup payload has no valid hash for {name}")
        actual = sha256_file(root / name)
        if actual != expected:
            raise SetupError(
                f"Setup payload hash mismatch for {name}: expected {expected}, got {actual}"
            )
    return root


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"expected JSON object: {path}")
    return value


def embedded_json(payload: Path, name: str) -> dict:
    return load_json(payload / name)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise SetupError(f"unsafe zip member: {info.filename}")
                resolved = (root / member).resolve()
                if resolved != root and root not in resolved.parents:
                    raise SetupError(f"unsafe zip member: {info.filename}")
            bundle.extractall(destination)
    except zipfile.BadZipFile as exc:
        raise SetupError(f"invalid zip archive: {archive}") from exc


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def receipt_path(home: Path) -> Path:
    return home / SETUP_DIR / RECEIPT_NAME


def read_receipt(home: Path) -> dict | None:
    path = receipt_path(home)
    if not path.is_file():
        return None
    value = load_json(path)
    if value.get("schema") != 1 or value.get("platform") != "windows":
        raise SetupError(f"unsupported Setup receipt: {path}")
    declared_home = value.get("home")
    if not isinstance(declared_home, str) or not declared_home:
        raise SetupError(f"Setup receipt has no managed home: {path}")
    if Path(declared_home).expanduser().resolve() != home.expanduser().resolve():
        raise SetupError(f"Setup receipt home does not match its location: {path}")
    return value


def write_receipt(home: Path, receipt: dict) -> None:
    atomic_write(
        receipt_path(home),
        (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def read_suite_registry_from_zip(payload: Path, name: str) -> dict:
    try:
        with zipfile.ZipFile(payload / "suite.zip") as bundle:
            return json.loads(bundle.read(name).decode("utf-8"))
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise SetupError(f"invalid embedded suite registry {name}: {exc}") from exc


def host_rows(payload: Path) -> list[dict]:
    bootstrap = read_suite_registry_from_zip(payload, "registry/bootstrap.json")
    rows = []
    for host, spec in (bootstrap.get("agent_hosts") or {}).items():
        if not isinstance(spec, dict):
            continue
        detect = Path(str(spec.get("detect_dir", ""))).expanduser()
        skills = Path(str(spec.get("skills_dir", ""))).expanduser()
        rows.append({
            "id": host,
            "detect_dir": str(detect),
            "skills_dir": str(skills),
            "detected": detect.is_dir(),
        })
    return rows


def validate_platform(allow_test_platform: bool = False) -> None:
    if platform.system().lower() != "windows" and not allow_test_platform:
        raise SetupError(
            "My-LLM-Wiki-Setup.exe is Windows-only; macOS and Linux keep bootstrap.sh v4"
        )
    machine = platform.machine().lower()
    if not allow_test_platform and machine not in {"amd64", "x86_64", "x64"}:
        raise SetupError(f"unsupported Windows architecture: {platform.machine()}")


def ensure_suite(payload: Path, home: Path) -> tuple[Path, str]:
    registry = read_suite_registry_from_zip(payload, "registry/skills.json")
    version = registry.get("pack_version")
    if not isinstance(version, str) or not version:
        raise SetupError("embedded suite has no pack_version")
    root = home / "suite" / "versions" / version
    if root.is_dir():
        installed = load_json(root / "registry" / "skills.json")
        if installed.get("pack_version") != version:
            raise SetupError(f"managed suite version directory is corrupt: {root}")
        return root, version
    staging = root.parent / f".{version}.{uuid.uuid4().hex}.staging"
    try:
        safe_extract(payload / "suite.zip", staging)
        installed = load_json(staging / "registry" / "skills.json")
        if installed.get("pack_version") != version:
            raise SetupError("extracted suite pack_version mismatch")
        root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    emit("suite-ready", version=version, path=str(root))
    return root, version


def enable_embedded_python(runtime: Path) -> None:
    pth = list(runtime.glob("python*._pth"))
    if len(pth) != 1:
        raise SetupError(f"private Python has an unexpected layout: {runtime}")
    pth[0].write_text(
        "python312.zip\n.\nLib\nLib/site-packages\nimport site\n",
        encoding="utf-8",
    )
    (runtime / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)


def ensure_runtime(payload: Path, home: Path, lock: dict) -> Path:
    expected = str((lock.get("python") or {}).get("sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise SetupError("embedded Python lock has no valid sha256")
    actual = sha256_file(payload / "python.zip")
    if actual != expected:
        raise SetupError(
            f"embedded Python hash mismatch: expected {expected}, got {actual}"
        )
    runtime = home / "runtime" / "python"
    executable = runtime / "python.exe"
    marker = runtime / ".llm-wiki-runtime.json"
    if executable.is_file() and marker.is_file():
        current = load_json(marker)
        if current.get("version") == lock["python"]["version"]:
            return runtime
    staging = runtime.parent / f".python.{uuid.uuid4().hex}.staging"
    backup = runtime.parent / f".python.backup.{uuid.uuid4().hex}"
    try:
        safe_extract(payload / "python.zip", staging)
        enable_embedded_python(staging)
        (staging / ".llm-wiki-runtime.json").write_text(
            json.dumps({
                "schema": 1,
                "version": lock["python"]["version"],
                "source_sha256": lock["python"]["sha256"],
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        runtime.parent.mkdir(parents=True, exist_ok=True)
        if runtime.exists():
            os.replace(runtime, backup)
        os.replace(staging, runtime)
        shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        if backup.exists() and not runtime.exists():
            os.replace(backup, runtime)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    emit("runtime-ready", version=lock["python"]["version"], path=str(runtime))
    return runtime


def copy_setup_executable(home: Path) -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    source = Path(sys.executable).resolve()
    destination = home / SETUP_DIR / SETUP_EXE
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if source == destination.resolve():
            return destination
    except OSError:
        pass
    temp = destination.parent / f".{destination.name}.{uuid.uuid4().hex}"
    shutil.copy2(source, temp)
    os.replace(temp, destination)
    return destination


def download_component(
    component: str,
    spec: dict,
    manifest: dict,
    home: Path,
    asset_dir: Path | None,
) -> Path:
    asset = str(spec.get("asset", ""))
    expected = str(spec.get("sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise SetupError(f"component {component} has no valid sha256")
    if Path(asset).name != asset or not asset.endswith(".zip"):
        raise SetupError(f"component {component} has an unsafe asset name")
    if asset_dir is not None:
        local = asset_dir / asset
        if not local.is_file():
            raise SetupError(f"component asset is missing: {local}")
        if sha256_file(local) != expected:
            raise SetupError(f"component asset hash mismatch: {local}")
        return local

    cache = home / SETUP_DIR / "downloads" / f"{expected}-{asset}"
    if cache.is_file() and sha256_file(cache) == expected:
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    failures = []
    tag = manifest.get("release_tag", "")
    for template in manifest.get("sources", []):
        if not isinstance(template, str):
            continue
        url = template.replace("{tag}", str(tag)).replace("{asset}", asset)
        if not url.startswith("https://"):
            failures.append(f"refused non-HTTPS source: {url}")
            continue
        temp = cache.parent / f".{asset}.{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        total = 0
        emit("component-download", component=component, source=url)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "My-LLM-Wiki-Setup"})
            with urllib.request.urlopen(request, timeout=30) as response, temp.open("wb") as sink:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD:
                        raise SetupError(f"component exceeds {MAX_DOWNLOAD} bytes")
                    digest.update(chunk)
                    sink.write(chunk)
            if digest.hexdigest() != expected:
                raise SetupError(
                    f"component hash mismatch: expected {expected}, got {digest.hexdigest()}"
                )
            os.replace(temp, cache)
            return cache
        except (OSError, urllib.error.URLError, SetupError) as exc:
            failures.append(f"{url}: {exc}")
        finally:
            temp.unlink(missing_ok=True)
    raise SetupError(
        f"all release sources failed for component {component}: " + "; ".join(failures)
    )


def expand_component_argv(
    raw: object,
    *,
    home: Path,
    suite: Path,
    runtime: Path,
    component: Path,
) -> list[str]:
    if not isinstance(raw, list) or not raw or any(
        not isinstance(arg, str) or not arg for arg in raw
    ):
        raise SetupError("component manifest has invalid argv")
    replacements = {
        "home": str(home),
        "suite": str(suite),
        "runtime": str(runtime),
        "component": str(component),
    }
    out = []
    for arg in raw:
        for key, value in replacements.items():
            arg = arg.replace("{" + key + "}", value)
        out.append(arg)
    return out


def selected_runtime_env(component: str, spec: dict) -> dict[str, str]:
    routes = spec.get("runtime_env") or {}
    if not isinstance(routes, dict) or not routes:
        return {}
    route = "global"
    if component == "asr-other":
        request = urllib.request.Request(
            "https://huggingface.co/", method="HEAD", headers={"User-Agent": "My-LLM-Wiki-Setup"}
        )
        try:
            with urllib.request.urlopen(request, timeout=3):
                pass
        except Exception:
            route = "cn"
    values = routes.get(route, {})
    return dict(values) if isinstance(values, dict) else {}


def postcheck_component(
    component: str,
    spec: dict,
    root: Path,
    home: Path,
    suite: Path,
    runtime: Path,
    *,
    skip: bool = False,
) -> tuple[dict[str, dict], dict[str, str], dict[str, dict[str, str]]]:
    tools: dict[str, dict] = {}
    profiles: dict[str, str] = {}
    runtime_env: dict[str, dict[str, str]] = {}
    for name, tool in (spec.get("tools") or {}).items():
        if not isinstance(tool, dict):
            raise SetupError(f"invalid tool declaration for {component}/{name}")
        prefix = expand_component_argv(
            tool.get("argv"), home=home, suite=suite, runtime=runtime, component=root
        )
        if not Path(prefix[0]).is_file():
            raise SetupError(f"component {component} is missing {prefix[0]}")
        postcheck = tool.get("postcheck") or []
        if not skip:
            result = subprocess.run(
                [*prefix, *postcheck],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                raise SetupError(
                    f"component {component} postcheck failed for {name}: {result.returncode}"
                )
        tools[name] = {"argv": prefix, "component": component}

    profile = spec.get("python_profile")
    if isinstance(profile, str) and profile:
        python = root / "python.exe"
        if not python.is_file():
            raise SetupError(f"component {component} has no private python.exe")
        env_values = selected_runtime_env(component, spec)
        if not skip:
            env = os.environ.copy()
            env.update(env_values)
            result = subprocess.run(
                [str(python), *(spec.get("postcheck") or [])],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )
            if result.returncode != 0:
                raise SetupError(
                    f"component {component} Python postcheck failed: {result.returncode}"
                )
        profiles[profile] = str(python)
        runtime_env[profile] = env_values
    return tools, profiles, runtime_env


def ensure_component(
    component: str,
    manifest: dict,
    home: Path,
    suite: Path,
    runtime: Path,
    asset_dir: Path | None,
    *,
    skip_postcheck: bool = False,
) -> dict:
    spec = (manifest.get("components") or {}).get(component)
    if not isinstance(spec, dict):
        raise SetupError(f"unknown Windows component: {component}")
    version = str(spec.get("version", ""))
    if not version:
        raise SetupError(f"component {component} has no version")
    root = home / "components" / component / "versions" / version
    marker = root / ".llm-wiki-component.json"
    expected_marker = {
        "schema": 1,
        "component": component,
        "version": version,
        "asset": spec["asset"],
        "sha256": spec["sha256"],
    }
    marker_valid = False
    if marker.is_file():
        try:
            marker_valid = load_json(marker) == expected_marker
        except SetupError:
            marker_valid = False

    def replace_component_bytes() -> None:
        archive = download_component(component, spec, manifest, home, asset_dir)
        staging = root.parent / f".{version}.{uuid.uuid4().hex}.staging"
        backup = root.parent / f".{version}.{uuid.uuid4().hex}.backup"
        try:
            safe_extract(archive, staging)
            (staging / ".llm-wiki-component.json").write_text(
                json.dumps(expected_marker, indent=2) + "\n", encoding="utf-8"
            )
            root.parent.mkdir(parents=True, exist_ok=True)
            if root.exists():
                os.replace(root, backup)
            os.replace(staging, root)
            shutil.rmtree(backup, ignore_errors=True)
        except Exception:
            if backup.exists() and not root.exists():
                os.replace(backup, root)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    if not marker_valid:
        replace_component_bytes()
    try:
        tools, profiles, runtime_env = postcheck_component(
            component, spec, root, home, suite, runtime, skip=skip_postcheck
        )
    except SetupError:
        if not marker_valid or skip_postcheck:
            raise
        emit("component-repair", component=component, reason="postcheck-failed")
        replace_component_bytes()
        tools, profiles, runtime_env = postcheck_component(
            component, spec, root, home, suite, runtime, skip=False
        )
    emit("component-ready", component=component, version=version, path=str(root))
    return {
        "version": version,
        "path": str(root),
        "asset": spec["asset"],
        "sha256": spec["sha256"],
        "tools": tools,
        "python_profiles": profiles,
        "runtime_env": runtime_env,
    }


def stage_opencli_extension(home: Path, component_state: dict) -> list[str]:
    root = Path(component_state["path"])
    extension = root / "extension"
    manifest = extension / "manifest.json"
    if not manifest.is_file():
        raise SetupError(f"web component has no Browser Bridge extension: {manifest}")
    try:
        version = str(json.loads(manifest.read_text(encoding="utf-8")).get("version", ""))
    except json.JSONDecodeError:
        version = ""
    destination = home / "opencli-extension"
    destination.mkdir(parents=True, exist_ok=True)
    pointer = {
        "schema": 1,
        "version": version,
        "path": str(extension),
        "asset": component_state["asset"],
        "source": "windows-setup-component",
    }
    atomic_write(
        destination / "current.json",
        (json.dumps(pointer, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return [
        "Open chrome://extensions in Chrome",
        "Toggle on Developer mode",
        f"Click Load unpacked and select: {extension}",
        "Run opencli doctor through the managed tool runner",
    ]


def git_config_root(path: Path) -> Path | None:
    for parent in (path, *path.parents):
        config = parent / ".git" / "config"
        if config.is_file() and (parent / "registry" / "skills.json").is_file():
            return parent
    return None


def official_checkout(path: Path) -> bool:
    root = git_config_root(path)
    if root is None:
        return False
    try:
        text = (root / ".git" / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    urls = re.findall(r"^\s*url\s*=\s*(\S+)\s*$", text, flags=re.MULTILINE)
    return any(url in OFFICIAL_REMOTES for url in urls)


def owned_or_legacy_destination(path: Path, home: Path) -> bool:
    manifest = path / ".llm-wiki-install.json"
    if manifest.is_file():
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if value.get("installer") == "windows-setup":
            return True
        source = value.get("source_repo")
        if isinstance(source, str) and source and official_checkout(Path(source)):
            return True
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    managed_suite = home / "suite"
    if resolved == managed_suite or managed_suite in resolved.parents:
        return True
    return official_checkout(resolved)


def setup_copy_matches(path: Path, install_id: str) -> bool:
    try:
        value = json.loads(
            (path / ".llm-wiki-install.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False
    return (
        value.get("schema") == 1
        and value.get("installer") == "windows-setup"
        and value.get("distribution") == "managed-pack"
        and value.get("install_id") == install_id
    )


def import_suite_modules(suite: Path):
    scripts = str(suite / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    for name in ("install", "initialize_wiki"):
        if name in sys.modules:
            del sys.modules[name]
    install = importlib.import_module("install")
    initialize = importlib.import_module("initialize_wiki")
    return install, initialize


def rollback_skill_plan(install, plan: dict) -> None:
    for item in reversed(plan.get("actions", [])):
        if item.get("state") != "installed":
            continue
        destination = Path(item["destination"])
        try:
            install.remove_path(destination)
            backup = item.get("backup")
            if backup and Path(backup).exists():
                os.replace(backup, destination)
        except OSError as exc:
            emit("rollback-error", destination=str(destination), error=str(exc))


def doctor_command(runtime: Path, suite: Path, hosts: list[str]) -> list[str]:
    command = [str(runtime / "python.exe"), str(suite / "scripts" / "doctor.py")]
    for host in hosts:
        command += ["--host", host]
    return command


def run_doctor(runtime: Path, suite: Path, hosts: list[str], home: Path) -> int:
    result = subprocess.run(
        doctor_command(runtime, suite, hosts),
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
        env={
            **os.environ,
            "PYTHONUTF8": "1",
            "LLM_WIKI_SETUP_RECEIPT": str(receipt_path(home)),
        },
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def build_receipt(
    *,
    old: dict | None,
    manifest: dict,
    lock: dict,
    home: Path,
    suite: Path,
    runtime: Path,
    pack_version: str,
    hosts: list[str],
    components: dict[str, dict],
) -> dict:
    tools: dict[str, dict] = {}
    profiles = {"core": str(runtime / "python.exe")}
    runtime_env: dict[str, dict[str, str]] = {}
    component_rows = {}
    for component, state in components.items():
        tools.update(state.get("tools", {}))
        profiles.update(state.get("python_profiles", {}))
        runtime_env.update(state.get("runtime_env", {}))
        component_rows[component] = {
            key: state[key] for key in ("version", "path", "asset", "sha256")
        }
    install_id = old.get("install_id") if old else None
    return {
        "schema": 1,
        "install_id": install_id or uuid.uuid4().hex,
        "installer_version": manifest.get("setup_version", lock.get("setup_version")),
        "platform": "windows",
        "architecture": "x86_64",
        "release_tag": manifest.get("release_tag", ""),
        "home": str(home),
        "suite": str(suite),
        # Store the concrete private-Python root used by component argv
        # expansion.  The purge boundary remains ``home/runtime``; this field
        # is an execution contract, not the ownership boundary.
        "runtime": str(runtime),
        "pack_version": pack_version,
        "hosts": sorted(set([*(old or {}).get("hosts", []), *hosts])),
        "components": component_rows,
        "tools": tools,
        "python_profiles": profiles,
        "runtime_env": runtime_env,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def install_flow(
    *,
    hosts: list[str],
    components: list[str],
    home: Path,
    payload: Path,
    asset_dir: Path | None,
    allow_test_platform: bool = False,
    skip_postcheck: bool = False,
) -> int:
    validate_platform(allow_test_platform)
    rows = {row["id"]: row for row in host_rows(payload)}
    if not hosts:
        raise SetupError("select at least one agent host")
    unknown = sorted(set(hosts) - set(rows))
    if unknown:
        raise SetupError("unknown host(s): " + ", ".join(unknown))

    lock = embedded_json(payload, "windows-toolchain.lock.json")
    manifest = embedded_json(payload, "component-manifest.json")
    if lock.get("schema") != 1 or manifest.get("schema") != 1:
        raise SetupError("unsupported Windows Setup manifest")
    suite, pack_version = ensure_suite(payload, home)
    runtime = ensure_runtime(payload, home, lock)
    copy_setup_executable(home)
    old = read_receipt(home)

    requested = list(dict.fromkeys(components))
    unknown_components = sorted(set(requested) - set(manifest.get("components", {})))
    if unknown_components:
        raise SetupError("unknown component(s): " + ", ".join(unknown_components))
    component_states: dict[str, dict] = {}
    for component in requested:
        component_states[component] = ensure_component(
            component,
            manifest,
            home,
            suite,
            runtime,
            asset_dir,
            skip_postcheck=skip_postcheck,
        )
    # Keep already installed components across a core/host repair.
    if old:
        for component, row in (old.get("components") or {}).items():
            if component in component_states:
                continue
            spec = (manifest.get("components") or {}).get(component)
            root = Path(str(row.get("path", ""))) if isinstance(row, dict) else Path()
            if isinstance(spec, dict) and root.is_dir():
                tools, profiles, env = postcheck_component(
                    component, spec, root, home, suite, runtime, skip=skip_postcheck
                )
                component_states[component] = {
                    **row,
                    "tools": tools,
                    "python_profiles": profiles,
                    "runtime_env": env,
                }

    receipt = build_receipt(
        old=old,
        manifest=manifest,
        lock=lock,
        home=home,
        suite=suite,
        runtime=runtime,
        pack_version=pack_version,
        hosts=hosts,
        components=component_states,
    )
    install, initialize = import_suite_modules(suite)
    config = install.load_json(suite / "registry" / "bootstrap.json")
    registry = install.load_json(suite / "registry" / "skills.json")
    old_receipt = receipt_path(home).read_bytes() if receipt_path(home).is_file() else None
    plan = None
    try:
        with install.install_lock(config):
            plan = install.build_plan(config, registry, hosts, [], [], "copy", True)
            # A digest-current generic v4 copy is not current for the Windows
            # hard-cutover contract. Every selected copy must carry this
            # receipt's install_id, otherwise repair it through backup/replace.
            for item in plan["actions"]:
                destination = Path(item["destination"])
                if item["state"] == "current" and not setup_copy_matches(
                    destination, receipt["install_id"]
                ):
                    item["state"] = "replace"
            foreign = [
                item["destination"]
                for item in plan["actions"]
                if item["state"] == "replace"
                and not owned_or_legacy_destination(Path(item["destination"]), home)
            ]
            if foreign:
                raise SetupError(
                    "foreign skill destinations require manual resolution; nothing was replaced: "
                    + ", ".join(foreign)
                )
            plan["copy_manifest"] = {
                "installer": "windows-setup",
                "install_id": receipt["install_id"],
                "distribution": "managed-pack",
            }
            install.apply_plan(config, plan)
            write_receipt(home, receipt)
            initialize.ensure_wiki(
                config,
                python_executable=str(runtime / "python.exe"),
            )
        doctor_status = 0 if skip_postcheck else run_doctor(runtime, suite, hosts, home)
        if doctor_status not in {0, 3}:
            raise SetupError(f"doctor failed with status {doctor_status}")
    except Exception:
        if plan is not None:
            rollback_skill_plan(install, plan)
        if old_receipt is None:
            receipt_path(home).unlink(missing_ok=True)
        else:
            atomic_write(receipt_path(home), old_receipt)
        raise

    if "web" in component_states:
        steps = stage_opencli_extension(home, component_states["web"])
        for index, step in enumerate(steps, start=1):
            print(f"Browser Bridge {index}. {step}")
    emit(
        "installed",
        hosts=hosts,
        components=sorted(component_states),
        pack_version=pack_version,
        doctor_status=doctor_status,
        receipt=str(receipt_path(home)),
    )
    return doctor_status


def install_components(
    *,
    components: list[str],
    home: Path,
    payload: Path,
    asset_dir: Path | None,
    allow_test_platform: bool = False,
    skip_postcheck: bool = False,
) -> int:
    validate_platform(allow_test_platform)
    old = read_receipt(home)
    if old is None:
        raise SetupError("install the core and at least one agent host first")
    return install_flow(
        hosts=list(old.get("hosts", [])),
        components=[*old.get("components", {}).keys(), *components],
        home=home,
        payload=payload,
        asset_dir=asset_dir,
        allow_test_platform=allow_test_platform,
        skip_postcheck=skip_postcheck,
    )


def doctor_components(
    components: list[str], home: Path, payload: Path, *, skip: bool = False
) -> int:
    receipt = read_receipt(home)
    if receipt is None:
        raise SetupError("Windows Setup receipt is missing")
    manifest = embedded_json(payload, "component-manifest.json")
    failed = []
    for component in components:
        row = (receipt.get("components") or {}).get(component)
        spec = (manifest.get("components") or {}).get(component)
        if not isinstance(row, dict) or not isinstance(spec, dict):
            failed.append(component)
            continue
        try:
            postcheck_component(
                component,
                spec,
                Path(row["path"]),
                home,
                Path(receipt["suite"]),
                Path(receipt["runtime"]),
                skip=skip,
            )
            emit("component-ok", component=component, version=row.get("version"))
        except SetupError as exc:
            failed.append(component)
            emit("component-failed", component=component, error=str(exc))
    return 1 if failed else 0


def managed_argv(receipt: dict, name: str) -> list[str]:
    tools = receipt.get("tools") or {}
    spec = tools.get(name) if isinstance(tools, dict) else None
    raw = spec.get("argv") if isinstance(spec, dict) else None
    if not isinstance(raw, list) or not raw or any(
        not isinstance(arg, str) or not arg for arg in raw
    ):
        raise SetupError(f"managed tool is not installed: {name}")
    executable = Path(raw[0])
    raw_home = receipt.get("home")
    if not isinstance(raw_home, str) or not raw_home:
        raise SetupError("Setup receipt has no managed home")
    home = Path(raw_home).resolve()
    try:
        resolved = executable.resolve(strict=True)
    except OSError as exc:
        raise SetupError(f"managed tool executable is missing: {executable}") from exc
    if resolved != home and home not in resolved.parents:
        raise SetupError(f"managed tool executable escapes Setup home: {resolved}")
    return [str(resolved), *raw[1:]]


def _remainder(values: list[str]) -> list[str]:
    return values[1:] if values[:1] == ["--"] else values


def run_managed_tool(name: str, values: list[str], home: Path) -> int:
    receipt = read_receipt(home)
    if receipt is None:
        raise SetupError("Windows Setup receipt is missing")
    command = [*managed_argv(receipt, name), *_remainder(values)]
    return subprocess.run(command, check=False).returncode


def run_managed_python(profile: str, values: list[str], home: Path) -> int:
    receipt = read_receipt(home)
    if receipt is None:
        raise SetupError("Windows Setup receipt is missing")
    profiles = receipt.get("python_profiles") or {}
    executable = profiles.get(profile) if isinstance(profiles, dict) else None
    if not isinstance(executable, str) or not executable:
        raise SetupError(f"managed Python profile is not installed: {profile}")
    prefix = managed_argv(
        {**receipt, "tools": {"python": {"argv": [executable]}}}, "python"
    )
    env = os.environ.copy()
    env["LLM_WIKI_SETUP_RECEIPT"] = str(receipt_path(home))
    values_by_profile = (receipt.get("runtime_env") or {}).get(profile, {})
    if isinstance(values_by_profile, dict):
        env.update({
            key: value
            for key, value in values_by_profile.items()
            if isinstance(key, str) and isinstance(value, str)
        })
    return subprocess.run([*prefix, *_remainder(values)], env=env, check=False).returncode


def uninstall_hosts(
    hosts: list[str], home: Path, payload: Path, *, purge: bool = False
) -> int:
    receipt = read_receipt(home)
    if receipt is None:
        raise SetupError("Windows Setup receipt is missing")
    installed_hosts = list(receipt.get("hosts", []))
    selected = hosts or installed_hosts
    unknown = sorted(set(selected) - set(installed_hosts))
    if unknown:
        raise SetupError("host(s) are not owned by this Setup: " + ", ".join(unknown))
    if purge and set(selected) != set(installed_hosts):
        raise SetupError("--purge requires uninstalling every Setup-owned host")
    suite = Path(receipt["suite"])
    install, _ = import_suite_modules(suite)
    bootstrap = install.load_json(suite / "registry" / "bootstrap.json")
    skills = install.load_json(suite / "registry" / "skills.json")
    targets = install.resolve_targets(bootstrap, selected, []) if selected else []
    removed = []
    for target in targets:
        for skill in skills.get("skills", []):
            destination = target["path"] / skill["slug"]
            manifest_path = destination / ".llm-wiki-install.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                manifest.get("installer") == "windows-setup"
                and manifest.get("install_id") == receipt.get("install_id")
            ):
                install.remove_path(destination)
                removed.append(str(destination))
    receipt["hosts"] = [host for host in installed_hosts if host not in selected]
    receipt["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if purge:
        # Wikis and their registry are user data and deliberately remain.
        for managed in (
            home / "components",
            home / "runtime",
            home / "suite",
            home / "opencli-extension",
            home / SETUP_DIR / "downloads",
        ):
            if managed.is_dir():
                shutil.rmtree(managed)
            elif managed.exists():
                managed.unlink()
        receipt_path(home).unlink(missing_ok=True)
    else:
        write_receipt(home, receipt)
    emit(
        "uninstalled",
        hosts=selected,
        removed=removed,
        preserved=(
            [str(home / "wikis.json")]
            if purge
            else [str(home / "wikis.json"), str(home / "components")]
        ),
        purged=purge,
    )
    if purge:
        stable = home / SETUP_DIR / SETUP_EXE
        print(
            f"Managed runtime removed. Delete {stable} after this Setup process exits."
        )
    return 0


def component_choices(manifest: dict) -> list[dict]:
    out = []
    for component, spec in (manifest.get("components") or {}).items():
        out.append({
            "id": component,
            "label": spec.get("label", component),
            "description": spec.get("description", ""),
            "default": bool(spec.get("default")),
            "size": int(spec.get("size", 0)),
        })
    return out


def launch_gui(home: Path, payload: Path, asset_dir: Path | None) -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError as exc:
        raise SetupError("Windows Setup GUI is unavailable; use the install command") from exc

    manifest = embedded_json(payload, "component-manifest.json")
    root = tk.Tk()
    root.title("My LLM Wiki Setup")
    root.geometry("720x640")
    tk.Label(root, text="My LLM Wiki Setup", font=("Segoe UI", 18, "bold")).pack(
        anchor="w", padx=24, pady=(20, 4)
    )
    tk.Label(
        root,
        text="Windows native install · select every agent host and optional tool component",
        font=("Segoe UI", 10),
    ).pack(anchor="w", padx=24, pady=(0, 16))

    tk.Label(root, text="Agent hosts", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=24)
    host_vars = {}
    for row in host_rows(payload):
        var = tk.BooleanVar(value=False)
        host_vars[row["id"]] = var
        suffix = "detected" if row["detected"] else "not detected"
        tk.Checkbutton(
            root,
            text=f"{row['id']} — {row['skills_dir']} ({suffix})",
            variable=var,
            anchor="w",
        ).pack(fill="x", padx=36)

    tk.Label(root, text="Tool components", font=("Segoe UI", 11, "bold")).pack(
        anchor="w", padx=24, pady=(16, 0)
    )
    component_vars = {}
    for row in component_choices(manifest):
        var = tk.BooleanVar(value=row["default"])
        component_vars[row["id"]] = var
        size = f" · {row['size'] / (1024 * 1024):.0f} MiB" if row["size"] else ""
        tk.Checkbutton(
            root,
            text=f"{row['label']}{size}\n    {row['description']}",
            variable=var,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=36, pady=2)

    result = {"code": 2}
    status = tk.StringVar(value="Ready")
    tk.Label(root, textvariable=status, fg="#555").pack(anchor="w", padx=24, pady=(18, 4))

    def run_install() -> None:
        hosts = [name for name, var in host_vars.items() if var.get()]
        components = [name for name, var in component_vars.items() if var.get()]
        if not hosts:
            messagebox.showerror("My LLM Wiki Setup", "Select at least one agent host.")
            return
        status.set("Installing… this can take several minutes for ASR components.")
        install_button.configure(state="disabled")

        def worker() -> None:
            try:
                code = install_flow(
                    hosts=hosts,
                    components=components,
                    home=home,
                    payload=payload,
                    asset_dir=asset_dir,
                )
                result["code"] = code
                root.after(0, lambda: messagebox.showinfo(
                    "My LLM Wiki Setup",
                    "Installation completed." + (
                        " Some selected capabilities still need action; see the Setup log."
                        if code == 3 else ""
                    ),
                ))
                root.after(0, root.destroy)
            except Exception as exc:  # noqa: BLE001 - surface the full installer failure
                message = str(exc)
                root.after(0, lambda message=message: messagebox.showerror(
                    "My LLM Wiki Setup", message
                ))
                root.after(0, lambda: status.set("Installation failed; no foreign paths were replaced."))
                root.after(0, lambda: install_button.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    install_button = tk.Button(root, text="Install", command=run_install, width=18)
    install_button.pack(side="left", padx=(24, 8), pady=12)
    tk.Button(root, text="Cancel", command=root.destroy, width=18).pack(
        side="left", padx=8, pady=12
    )
    root.mainloop()
    return int(result["code"])


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", type=Path, default=Path(DEFAULT_HOME).expanduser())
    ap.add_argument("--payload", type=Path, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--asset-dir", type=Path, default=None)
    ap.add_argument("--allow-test-platform", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--skip-postcheck", action="store_true", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="command")

    hosts = sub.add_parser("hosts", help="list supported agent hosts")
    hosts.add_argument("--json", action="store_true")

    plan = sub.add_parser("plan", help="render an offline install plan")
    plan.add_argument("--host", action="append", default=[])
    plan.add_argument("--component", action="append", default=[])
    plan.add_argument("--all-tools", action="store_true")

    install = sub.add_parser("install", help="install/update/repair core and selected components")
    install.add_argument("--host", action="append", default=[])
    install.add_argument("--component", action="append", default=[])
    install.add_argument("--all-tools", action="store_true")

    components = sub.add_parser("components", help="maintain installed tool components")
    component_sub = components.add_subparsers(dest="component_command", required=True)
    component_install = component_sub.add_parser("install")
    component_install.add_argument("--component", action="append", required=True)
    component_doctor = component_sub.add_parser("doctor")
    component_doctor.add_argument("--component", action="append", required=True)

    tools = sub.add_parser("tools", help="run a receipt-managed external tool")
    tool_sub = tools.add_subparsers(dest="tool_command", required=True)
    tool_run = tool_sub.add_parser("run")
    tool_run.add_argument("tool")
    tool_run.add_argument("args", nargs=argparse.REMAINDER)

    python = sub.add_parser("python", help="run a receipt-managed Python profile")
    python_sub = python.add_subparsers(dest="python_command", required=True)
    python_run = python_sub.add_parser("run")
    python_run.add_argument("--profile", required=True)
    python_run.add_argument("args", nargs=argparse.REMAINDER)

    status = sub.add_parser("status", help="show managed installation state")
    status.add_argument("--json", action="store_true")

    uninstall = sub.add_parser("uninstall", help="remove Setup-owned agent skill copies")
    uninstall.add_argument("--host", action="append", default=[])
    uninstall.add_argument(
        "--purge",
        action="store_true",
        help="after removing every host, remove managed suite/runtime/components; preserve Wikis",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        home = args.home.expanduser().resolve()
        payload = payload_dir(args.payload)
        asset_dir = args.asset_dir.resolve() if args.asset_dir else None
        if args.command is None:
            validate_platform(args.allow_test_platform)
            return launch_gui(home, payload, asset_dir)
        if args.command == "hosts":
            rows = host_rows(payload)
            if args.json:
                print(json.dumps({"hosts": rows}, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    print(
                        f"{row['id']}: {row['skills_dir']} "
                        f"({'detected' if row['detected'] else 'not detected'})"
                    )
            return 0
        manifest = embedded_json(payload, "component-manifest.json")
        all_components = list((manifest.get("components") or {}).keys())
        if args.command == "plan":
            selected = all_components if args.all_tools else list(dict.fromkeys(args.component))
            print(json.dumps({
                "status": "planned",
                "platform": "windows",
                "hosts": args.host,
                "components": [
                    row for row in component_choices(manifest) if row["id"] in selected
                ],
                "home": str(home),
                "foreign_conflicts": "stop-before-write",
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "install":
            selected = all_components if args.all_tools else list(dict.fromkeys(args.component))
            return install_flow(
                hosts=list(dict.fromkeys(args.host)),
                components=selected,
                home=home,
                payload=payload,
                asset_dir=asset_dir,
                allow_test_platform=args.allow_test_platform,
                skip_postcheck=args.skip_postcheck,
            )
        if args.command == "components":
            selected = list(dict.fromkeys(args.component))
            if args.component_command == "install":
                return install_components(
                    components=selected,
                    home=home,
                    payload=payload,
                    asset_dir=asset_dir,
                    allow_test_platform=args.allow_test_platform,
                    skip_postcheck=args.skip_postcheck,
                )
            return doctor_components(selected, home, payload, skip=args.skip_postcheck)
        if args.command == "tools":
            validate_platform(args.allow_test_platform)
            return run_managed_tool(args.tool, args.args, home)
        if args.command == "python":
            validate_platform(args.allow_test_platform)
            return run_managed_python(args.profile, args.args, home)
        if args.command == "status":
            receipt = read_receipt(home)
            result = receipt or {"status": "not-installed", "platform": "windows"}
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print("not installed" if receipt is None else json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if receipt else 3
        if args.command == "uninstall":
            validate_platform(args.allow_test_platform)
            return uninstall_hosts(
                list(dict.fromkeys(args.host)), home, payload, purge=args.purge
            )
        raise SetupError(f"unsupported command: {args.command}")
    except SetupError as exc:
        print(f"windows-setup: {exc}", file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"windows-setup: operation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
