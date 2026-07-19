#!/usr/bin/env python3
"""Install My LLM Wiki Browser with a release-first strategy.

Default behavior:
1. Read the project-owned Tauri release manifest (htmlgo).
2. Fall back to the latest GitHub Release if the first-party source fails.
3. Download the exact Browser asset for this OS/arch. Windows setup.exe/MSI
   installers are launched and handed back to the user without monitoring.
4. Atomically write an installation receipt only for an installation completed
   by this process, such as a Windows portable archive or macOS app archive.
5. Optionally launch an application whose installation completed synchronously.

If no release or matching asset exists, the script exits with a clear message.
Use --fallback-source to build the Tauri app from source as a developer fallback.
"""
from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install_state import LockUnavailable, advisory_lock, remove_path  # noqa: E402
from minisign_verify import verify_tauri_minisign  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "registry" / "bootstrap.json"
NETWORK_PROBE = ROOT / "skills" / "cn-mirrors" / "scripts" / "net_probe.py"
TAURI_CONFIG = (
    ROOT / "apps" / "my-llm-wiki-browser" / "desktop" / "src-tauri" / "tauri.conf.json"
)
GITHUB_AUTH_HOSTS = {
    "github.com",
    "api.github.com",
    "uploads.github.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
}

# Shared shell-path normalizer (Git Bash /c/... -> C:/... on Windows only;
# POSIX systems keep such paths untouched). Same module init_wiki.py uses.
_PC_SPEC = importlib.util.spec_from_file_location(
    "path_compat", ROOT / "skills" / "my-llm-wiki" / "scripts" / "path_compat.py"
)
path_compat = importlib.util.module_from_spec(_PC_SPEC)
_PC_SPEC.loader.exec_module(path_compat)


class ReleaseSourcesUnavailable(RuntimeError):
    """Every configured Browser release source failed or was unreachable."""


class InstallerCompletionError(RuntimeError):
    """A downloaded artifact did not produce a completed installation."""


class InstallerLaunch:
    """A native installer was started and now owns the remaining UI flow."""

    def __init__(self, artifact: Path):
        self.artifact = artifact


def load_bootstrap() -> dict:
    config = json.loads(BOOTSTRAP.read_text(encoding="utf-8"))
    if config.get("version") != 4:
        raise RuntimeError(f"unsupported bootstrap protocol: {config.get('version')}")
    browser = config.get("browser")
    if not isinstance(browser, dict) or not browser.get("release_sources"):
        raise RuntimeError("bootstrap browser.release_sources is empty")
    if not isinstance(browser.get("operation_lock_file"), str):
        raise RuntimeError("bootstrap browser.operation_lock_file is missing")
    policy = browser.get("download_policy")
    if not isinstance(policy, dict) or any(
        not isinstance(policy.get(key), (int, float)) or policy[key] <= 0
        for key in ("socket_timeout_seconds", "total_timeout_seconds", "max_bytes")
    ):
        raise RuntimeError("invalid browser.download_policy")
    locations = browser.get("install_locations") or {}
    if not isinstance(locations.get("darwin_app_dir"), str):
        raise RuntimeError("browser.install_locations.darwin_app_dir is missing")
    receipt = browser.get("install_receipt") or {}
    required_receipt = {
        "path": str,
        "schema": int,
        "windows_product_name": str,
        "windows_uninstall_key": str,
        "windows_main_executable": str,
        "windows_installer_timeout_seconds": int,
        "windows_postcheck_timeout_seconds": int,
    }
    if any(
        not isinstance(receipt.get(key), expected)
        or (expected is int and receipt[key] <= 0)
        or (expected is str and not receipt[key])
        for key, expected in required_receipt.items()
    ):
        raise RuntimeError("invalid browser.install_receipt")
    source_steps = (browser.get("source_build") or {}).get("steps")
    if not isinstance(source_steps, list) or not source_steps:
        raise RuntimeError("bootstrap browser.source_build.steps is empty")
    for step in source_steps:
        if (
            not isinstance(step, dict)
            or not isinstance(step.get("argv"), list)
            or not step["argv"]
            or any(not isinstance(arg, str) or not arg for arg in step["argv"])
            or not isinstance(step.get("cwd"), str)
            or not isinstance(step.get("timeout_seconds"), int)
            or step["timeout_seconds"] <= 0
        ):
            raise RuntimeError("invalid browser source-build step")
        cwd = (ROOT / step["cwd"]).resolve()
        if cwd != ROOT and ROOT not in cwd.parents:
            raise RuntimeError(f"source-build cwd escapes repository: {step['cwd']}")
    agent_hosts = config.get("agent_hosts") or {}
    mcp_hosts = (config.get("mcp") or {}).get("hosts") or {}
    if set(mcp_hosts) != set(agent_hosts):
        raise RuntimeError("mcp host ids must exactly match agent_hosts")
    for name, host in mcp_hosts.items():
        for key in ("register_argv", "unregister_argv"):
            argv = host.get(key)
            if argv is not None and (
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(arg, str) for arg in argv)
            ):
                raise RuntimeError(f"invalid {name}.{key}")
    return config


def run(cmd: list[str], cwd: Path | None = None) -> str:
    return subprocess.check_output(cmd, cwd=cwd, text=True, timeout=5).strip()


def infer_repo() -> str | None:
    try:
        remote = run(["git", "remote", "get-url", "origin"], cwd=ROOT)
    except Exception:
        return None
    patterns = [
        r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$",
        r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$",
        r"gitee\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$",
        r"https://gitee\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$",
    ]
    for pattern in patterns:
        match = re.search(pattern, remote)
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    return None


def platform_keys() -> list[str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        if machine in {"arm64", "aarch64"}:
            return ["darwin-arm64", "darwin-universal"]
        return ["darwin-x64", "darwin-universal"]
    if system == "windows":
        return ["windows-x64"] if machine in {"amd64", "x86_64", "x64"} else []
    if system == "linux":
        return ["linux-x64"] if machine in {"amd64", "x86_64", "x64"} else []
    return []


def json_url(url: str, *, timeout: float = 30, headers: dict[str, str] | None = None) -> dict:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "llm-wiki-suite-bootstrap",
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    opener = urllib.request.build_opener(SafeRedirectHandler())
    started = time.monotonic()
    chunks = []
    size = 0
    with opener.open(request, timeout=timeout) as response:
        while True:
            if time.monotonic() - started > timeout:
                raise TimeoutError(f"JSON request exceeded {timeout:.0f}s: {url}")
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 1024 * 1024:
                raise RuntimeError(f"JSON response exceeds 1 MiB: {url}")
            chunks.append(chunk)
    return json.loads(b"".join(chunks).decode("utf-8"))


def github_json(url: str) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "llm-wiki-suite-bootstrap",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return json_url(url, headers=headers)


def release_metadata(repo: str, version: str) -> dict:
    if version == "latest":
        url = f"https://api.github.com/repos/{repo}/releases/latest"
    else:
        url = f"https://api.github.com/repos/{repo}/releases/tags/{version}"
    return github_json(url)


def tauri_platform_keys() -> list[str]:
    """Tauri updater platform keys in preferred install-artifact order."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        return (["darwin-aarch64", "darwin-aarch64-app"] if machine in {"arm64", "aarch64"}
                else ["darwin-x86_64", "darwin-x86_64-app"])
    if system == "windows":
        # NSIS is the closest equivalent to GitHub's setup.exe; MSI is a
        # fallback when a release omitted NSIS.
        return (["windows-x86_64-nsis", "windows-x86_64", "windows-x86_64-msi"]
                if machine in {"amd64", "x86_64", "x64"} else [])
    if system == "linux":
        return (["linux-x86_64-appimage", "linux-x86_64", "linux-x86_64-deb", "linux-x86_64-rpm"]
                if machine in {"amd64", "x86_64", "x64"} else [])
    return []


def normalize_manifest_asset_url(asset_url: str, manifest_url: str) -> str:
    """Keep first-party asset URLs HTTPS when the manifest itself was HTTPS."""
    asset = urlparse(asset_url)
    manifest = urlparse(manifest_url)
    if (manifest.scheme == "https" and asset.scheme == "http"
            and asset.hostname and asset.hostname == manifest.hostname):
        return urlunparse(asset._replace(scheme="https"))
    return asset_url


def tauri_manifest_asset(source: dict) -> dict:
    """Resolve this OS's installer from a Tauri ``latest.json`` manifest."""
    url = source.get("url")
    if not isinstance(url, str) or not url:
        raise RuntimeError(f"{source.get('name', 'tauri')} release source has no manifest URL")
    release = json_url(url, timeout=10)
    platforms = release.get("platforms")
    if not isinstance(platforms, dict):
        raise RuntimeError(f"{source.get('name', 'tauri')} manifest has no platforms map")
    for key in tauri_platform_keys():
        entry = platforms.get(key)
        asset_url = entry.get("url") if isinstance(entry, dict) else None
        if isinstance(asset_url, str) and asset_url:
            signature = entry.get("signature")
            if not isinstance(signature, str) or not signature:
                raise RuntimeError(f"{source.get('name', 'tauri')} asset is unsigned")
            normalized = normalize_manifest_asset_url(asset_url, url)
            return {
                "name": Path(unquote(urlparse(normalized).path)).name,
                "browser_download_url": normalized,
                "version": release.get("version", ""),
                "source": source.get("name", "tauri"),
                "signature": signature,
                "public_key": tauri_updater_public_key(),
            }
    raise RuntimeError(
        f"{source.get('name', 'tauri')} has no installable asset for "
        f"{platform.system()} {platform.machine()}"
    )


def tauri_updater_public_key() -> str:
    try:
        config = json.loads(TAURI_CONFIG.read_text(encoding="utf-8"))
        value = config["plugins"]["updater"]["pubkey"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"cannot read Tauri updater public key: {exc}") from exc
    if not isinstance(value, str) or not value:
        raise RuntimeError("Tauri updater public key is empty")
    return value


def unavailable_github_release_hosts(timeout: float = 4.0) -> list[str]:
    """Return unavailable/slow GitHub hosts needed for a release install.

    The suite's cn-mirrors probe is intentionally dependency-free and runs its
    requests concurrently. Do not invent a network verdict when the probe
    itself is absent or fails; let the bounded release request decide.
    """
    if not NETWORK_PROBE.is_file():
        return []
    try:
        proc = subprocess.run(
            [sys.executable, str(NETWORK_PROBE), "--json", "--timeout", str(timeout)],
            capture_output=True,
            text=True,
            timeout=timeout + 4,
            check=True,
        )
        report = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    statuses = {row.get("host"): row.get("status") for row in report.get("dev", [])}
    return [host for host in ("api.github.com", "objects.githubusercontent.com")
            if statuses.get(host) not in {None, "ok"}]


def resolve_source_asset(
    browser: dict,
    source: dict,
    repo: str | None,
    version: str,
    *,
    skip_network_probe: bool = False,
) -> dict:
    name = str(source.get("name") or source.get("format") or "unknown")
    format_name = source.get("format")
    if format_name == "tauri-latest":
        if version != "latest":
            raise RuntimeError("only supports --version latest")
        return tauri_manifest_asset(source)
    if format_name == "github-release":
        if not repo:
            raise RuntimeError("no GitHub repo available for fallback")
        if not skip_network_probe:
            unavailable = unavailable_github_release_hosts()
            if unavailable:
                raise RuntimeError(f"unavailable or slow ({', '.join(unavailable)})")
        release = release_metadata(repo, version)
        asset = pick_asset(release, browser["asset_patterns"])
        if not asset:
            names = ", ".join(item.get("name", "") for item in release.get("assets", [])) or "(none)"
            raise RuntimeError(f"no matching Browser asset. Release assets: {names}")
        asset["source"] = name
        return asset
    raise RuntimeError(f"unsupported release source format {format_name!r}")


def install_release(
    config: dict,
    repo: str | None,
    version: str,
    dest_dir: Path,
    *,
    dry_run: bool = False,
    skip_network_probe: bool = False,
    prepare: Callable[[Path], Path | InstallerLaunch] | None = None,
) -> tuple[Path, Path | InstallerLaunch, str]:
    """Resolve, verify, and prepare a source before accepting it as successful."""
    browser = config.get("browser") or {}
    policy = browser.get("download_policy") or {}
    errors: list[str] = []
    for source in browser.get("release_sources") or []:
        if not isinstance(source, dict):
            errors.append("invalid release source entry")
            continue
        name = str(source.get("name") or source.get("format") or "unknown")
        try:
            asset = resolve_source_asset(
                browser,
                source,
                repo,
                version,
                skip_network_probe=skip_network_probe,
            )
            downloaded = download_asset(
                asset,
                dest_dir,
                dry_run,
                socket_timeout=float(policy.get("socket_timeout_seconds", 20)),
                total_timeout=float(policy.get("total_timeout_seconds", 300)),
                max_bytes=int(policy.get("max_bytes", 2 * 1024 * 1024 * 1024)),
            )
            installed = prepare(downloaded) if prepare is not None else downloaded
            return downloaded, installed, name
        except InstallerCompletionError:
            # Preparation crossed an installation boundary. Starting another
            # source could race or overwrite the partially prepared target.
            raise
        except (
            OSError,
            TimeoutError,
            urllib.error.HTTPError,
            urllib.error.URLError,
            RuntimeError,
            ValueError,
        ) as exc:
            errors.append(f"{name}: {exc}")
    raise ReleaseSourcesUnavailable(
        "all configured release sources unavailable: " + "; ".join(errors)
    )


def pick_asset(release: dict, patterns_by_key: dict[str, list[str]]) -> dict | None:
    assets = release.get("assets", [])
    for key in platform_keys():
        patterns = patterns_by_key.get(key, [])
        for pattern in patterns:
            for asset in assets:
                name = asset.get("name", "")
                if fnmatch.fnmatchcase(name, pattern):
                    return asset
    return None


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != "https":
            raise urllib.error.HTTPError(
                newurl, code, "refusing non-HTTPS redirect", headers, fp
            )
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        old_host = urlparse(req.full_url).hostname
        new_host = urlparse(newurl).hostname
        if old_host != new_host and new_host not in GITHUB_AUTH_HOSTS:
            redirected.remove_header("Authorization")
        return redirected


def download_asset(
    asset: dict,
    dest_dir: Path,
    dry_run: bool = False,
    *,
    socket_timeout: float = 20,
    total_timeout: float = 300,
    max_bytes: int = 2 * 1024 * 1024 * 1024,
) -> Path:
    name = asset.get("name")
    if (
        not isinstance(name, str)
        or name in {"", ".", ".."}
        or Path(name).name != name
    ):
        raise RuntimeError(f"unsafe Browser asset name: {name!r}")
    dest = dest_dir / name
    url = asset["browser_download_url"]
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError(f"refusing non-HTTPS Browser asset URL: {url}")
    if dry_run:
        print(f"[dry-run] download {url} -> {dest}")
        return dest

    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "llm-wiki-suite-bootstrap"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and parsed.hostname in GITHUB_AUTH_HOSTS:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(SafeRedirectHandler())
    partial = dest.with_name(dest.name + ".part")
    started = time.monotonic()
    copied = 0
    try:
        with opener.open(request, timeout=socket_timeout) as response, partial.open("wb") as out:
            announced = response.headers.get("Content-Length")
            if announced and int(announced) > max_bytes:
                raise RuntimeError(f"Browser asset exceeds size limit: {announced} bytes")
            while True:
                if time.monotonic() - started > total_timeout:
                    raise TimeoutError(f"Browser download exceeded {total_timeout:.0f}s")
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    raise RuntimeError(f"Browser asset exceeds size limit: {max_bytes} bytes")
                out.write(chunk)
        signature = asset.get("signature")
        if signature:
            public_key = asset.get("public_key")
            if not isinstance(public_key, str) or not public_key:
                raise RuntimeError("signed Browser asset has no updater public key")
            try:
                verify_tauri_minisign(partial, public_key, signature)
            except ValueError as exc:
                raise RuntimeError(f"Browser signature verification failed: {exc}") from exc
            print(f"signature verified: {asset['name']}")
        os.replace(partial, dest)
    finally:
        if partial.exists():
            partial.unlink()
    print(f"downloaded: {dest}")
    return dest


def maybe_extract_zip(path: Path, dry_run: bool) -> Path | None:
    """Extract a portable zip next to itself; returns the extraction dir.

    Windows portable builds ship as `*-portable.zip` (exe + frontend/ inside a
    top-level app folder). Extracting is the whole install — no setup.exe run.
    """
    if path.suffix.lower() != ".zip":
        return None
    dest = path.with_suffix("")
    if dry_run:
        print(f"[dry-run] extract {path} -> {dest}")
        return dest
    import zipfile

    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{dest.name}.extracting-", dir=dest.parent)
    )
    try:
        with zipfile.ZipFile(path) as archive:
            total_size = 0
            root = staging.resolve()
            for member in archive.infolist():
                total_size += member.file_size
                if total_size > 4 * 1024 * 1024 * 1024:
                    raise RuntimeError("portable archive exceeds extraction size limit")
                target = (staging / member.filename).resolve()
                if target != root and root not in target.parents:
                    raise RuntimeError(f"unsafe archive member: {member.filename}")
            archive.extractall(staging)
        backup = None
        if dest.exists():
            backup = dest.with_name(f".{dest.name}.backup-{uuid.uuid4().hex}")
            os.replace(dest, backup)
        try:
            os.replace(staging, dest)
        except Exception:
            if backup is not None:
                os.replace(backup, dest)
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    print(f"extracted: {dest}")
    return dest


def install_macos_app_archive(
    path: Path, install_dir: Path, dry_run: bool = False
) -> Path | None:
    """Safely install the single app bundle in a signed Tauri tarball."""
    if not path.name.lower().endswith(".app.tar.gz"):
        return None
    if dry_run:
        planned = install_dir / path.name[:-7]
        print(f"[dry-run] extract and install {path} -> {planned}")
        return planned

    import tarfile

    install_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".llm-wiki-browser-", dir=install_dir))
    try:
        root = staging.resolve()
        with tarfile.open(path, mode="r:gz") as archive:
            total_size = 0
            members = archive.getmembers()
            targets: set[Path] = set()
            for member in members:
                total_size += member.size
                if total_size > 4 * 1024 * 1024 * 1024:
                    raise RuntimeError("macOS app archive exceeds extraction size limit")
                target = (staging / member.name).resolve()
                if target != root and root not in target.parents:
                    raise RuntimeError(f"unsafe archive member: {member.name}")
                if not (member.isdir() or member.isfile()):
                    raise RuntimeError(f"unsupported archive member: {member.name}")
                if target in targets:
                    raise RuntimeError(f"duplicate archive member: {member.name}")
                targets.add(target)
            for member in members:
                target = (staging / member.name).resolve()
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"cannot read archive member: {member.name}")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                target.chmod(member.mode & 0o777)

        apps = [
            candidate
            for candidate in staging.rglob("*.app")
            if candidate.is_dir()
            and not any(parent.suffix.lower() == ".app" for parent in candidate.parents)
        ]
        if len(apps) != 1:
            raise RuntimeError(f"macOS archive must contain exactly one app bundle; found {len(apps)}")
        app = apps[0]
        destination = install_dir / app.name
        backup = install_dir / f".{app.name}.backup-{uuid.uuid4().hex}"
        if destination.exists() or destination.is_symlink():
            os.replace(destination, backup)
        try:
            os.replace(app, destination)
        except Exception:
            if backup.exists() or backup.is_symlink():
                os.replace(backup, destination)
            raise
        remove_path(backup)
    finally:
        remove_path(staging)
    print(f"installed app: {destination}")
    return destination


def _windows_registry_install_location(config: dict) -> Path:
    """Resolve the installed executable from Windows uninstall registration."""
    try:
        import winreg
    except ImportError as exc:  # pragma: no cover - only available on Windows
        raise InstallerCompletionError("Python winreg is unavailable") from exc

    receipt = config["browser"]["install_receipt"]
    product_name = receipt["windows_product_name"]
    exact_key = receipt["windows_uninstall_key"]
    executable_name = receipt["windows_main_executable"]
    roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    views = tuple(
        dict.fromkeys(
            flag
            for flag in (
                getattr(winreg, "KEY_WOW64_64KEY", 0),
                getattr(winreg, "KEY_WOW64_32KEY", 0),
                0,
            )
        )
    )

    def executable_from_key(root, key_name: str, view: int) -> Path | None:
        try:
            with winreg.OpenKey(root, key_name, 0, winreg.KEY_READ | view) as key:
                install_location = winreg.QueryValueEx(key, "InstallLocation")[0]
        except OSError:
            return None
        if not isinstance(install_location, str) or not install_location.strip():
            return None
        executable = Path(install_location) / executable_name
        return executable if executable.is_file() else None

    for root in roots:
        for view in views:
            executable = executable_from_key(root, exact_key, view)
            if executable is not None:
                return executable

    # MSI uninstall entries are keyed by product GUID. Match the exact product
    # name, then require InstallLocation and the exact executable name.
    uninstall_root = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for root in roots:
        for view in views:
            try:
                parent = winreg.OpenKey(root, uninstall_root, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with parent:
                index = 0
                while True:
                    try:
                        child = winreg.EnumKey(parent, index)
                    except OSError:
                        break
                    index += 1
                    key_name = uninstall_root + "\\" + child
                    try:
                        with winreg.OpenKey(root, key_name, 0, winreg.KEY_READ | view) as key:
                            display_name = winreg.QueryValueEx(key, "DisplayName")[0]
                    except OSError:
                        continue
                    if display_name != product_name:
                        continue
                    executable = executable_from_key(root, key_name, view)
                    if executable is not None:
                        return executable
    raise InstallerCompletionError(
        f"Windows installer exited but {product_name!r} is not registered with "
        f"an existing {executable_name}"
    )


def _windows_portable_executable(config: dict, directory: Path) -> Path:
    executable_name = config["browser"]["install_receipt"]["windows_main_executable"]
    candidates = [
        candidate
        for candidate in directory.rglob(executable_name)
        if candidate.is_file()
    ]
    if len(candidates) != 1:
        raise InstallerCompletionError(
            f"portable archive must contain exactly one {executable_name}; "
            f"found {len(candidates)}"
        )
    return candidates[0]


def install_windows_artifact(
    config: dict, path: Path, dry_run: bool = False, silent: bool = False
) -> Path | InstallerLaunch:
    """Install a portable build or launch a native Windows installer."""
    if path.suffix.lower() == ".zip":
        extracted = maybe_extract_zip(path, dry_run)
        if extracted is None:
            raise RuntimeError(f"cannot extract Windows portable archive: {path}")
        if dry_run:
            planned = extracted / config["browser"]["install_receipt"][
                "windows_main_executable"
            ]
            print(f"[dry-run] verify portable executable: {planned}")
            return planned
        executable = _windows_portable_executable(config, extracted)
        print(f"verified portable Browser: {executable}")
        return executable

    suffix = path.suffix.lower()
    if suffix not in {".exe", ".msi"}:
        raise RuntimeError(f"unsupported Windows Browser artifact: {path.name}")
    if silent:
        argv = (
            [str(path), "/S"]
            if suffix == ".exe"
            else ["msiexec.exe", "/i", str(path), "/qn"]
        )
        if dry_run:
            print(f"[dry-run] run silently and wait: {display_argv(argv, 'windows')}")
            return InstallerLaunch(path)
        print("running Windows installer silently; waiting for completion")
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=900,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"silent Windows installer failed with exit code {result.returncode}"
            )
        executable = _windows_registry_install_location(config)
        print(f"verified installed Browser: {executable}")
        return executable
    argv = [str(path)] if suffix == ".exe" else ["msiexec.exe", "/i", str(path)]
    if dry_run:
        print(f"[dry-run] launch and return: {display_argv(argv, 'windows')}")
        return InstallerLaunch(path)
    print("starting Windows installer; returning without monitoring completion")
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise RuntimeError(f"could not launch Windows installer: {exc}") from exc
    return InstallerLaunch(path)


def _receipt_path(config: dict) -> Path:
    return expand(config["browser"]["install_receipt"]["path"])


def _install_kind(artifact: Path) -> str:
    system = platform.system().lower()
    if system == "windows":
        return "windows-portable" if artifact.suffix.lower() == ".zip" else "windows-installer"
    if system == "darwin":
        return "macos-app"
    if system == "linux":
        return "linux-appimage"
    raise InstallerCompletionError(f"unsupported Browser platform: {system}")


def write_install_receipt(
    config: dict,
    *,
    artifact: Path,
    target: Path,
    source: str,
    requested_version: str,
) -> Path:
    path = _receipt_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": config["browser"]["install_receipt"]["schema"],
        "platform": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "kind": _install_kind(artifact),
        "source": source,
        "requested_version": requested_version,
        "artifact": str(artifact.resolve()),
        "target": str(target.resolve()),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    temp = path.with_name(f".{path.name}.writing-{uuid.uuid4().hex}")
    try:
        temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    print(f"installation receipt: {path}")
    return path


def browser_install_state(config: dict) -> dict:
    """Validate the receipt and the installed target it attests to."""
    try:
        receipt_config = config["browser"]["install_receipt"]
        path = _receipt_path(config)
    except (KeyError, TypeError):
        return {"ok": False, "detail": "Browser install receipt is not configured"}
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "ok": False,
            "receipt": str(path),
            "detail": "Browser install receipt is missing",
        }
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "receipt": str(path),
            "detail": f"Browser install receipt is invalid: {exc}",
        }
    if receipt.get("schema") != receipt_config["schema"]:
        return {
            "ok": False,
            "receipt": str(path),
            "detail": "Browser install receipt schema mismatch",
        }
    current_platform = platform.system().lower()
    if receipt.get("platform") != current_platform:
        return {
            "ok": False,
            "receipt": str(path),
            "detail": "Browser install receipt belongs to another platform",
        }
    if receipt.get("architecture") != platform.machine().lower():
        return {
            "ok": False,
            "receipt": str(path),
            "detail": "Browser install receipt belongs to another architecture",
        }
    target_value = receipt.get("target")
    if not isinstance(target_value, str) or not target_value:
        return {
            "ok": False,
            "receipt": str(path),
            "detail": "Browser install receipt has no target",
        }
    target = Path(target_value)
    kind = receipt.get("kind")
    try:
        if kind == "windows-installer":
            registered = _windows_registry_install_location(config)
            if registered.resolve() != target.resolve():
                raise InstallerCompletionError(
                    "Windows registered executable differs from receipt"
                )
        elif kind == "windows-portable":
            expected = receipt_config["windows_main_executable"]
            if target.name.lower() != expected.lower() or not target.is_file():
                raise InstallerCompletionError("portable Browser executable is missing")
        elif kind == "macos-app":
            if target.suffix.lower() != ".app" or not target.is_dir():
                raise InstallerCompletionError("installed macOS app bundle is missing")
        elif kind == "linux-appimage":
            if not target.is_file() or not os.access(target, os.X_OK):
                raise InstallerCompletionError("installed AppImage is missing or not executable")
        else:
            raise InstallerCompletionError(f"unknown Browser install kind: {kind!r}")
    except (OSError, InstallerCompletionError) as exc:
        return {"ok": False, "receipt": str(path), "detail": str(exc)}
    return {
        "ok": True,
        "receipt": str(path),
        "target": str(target),
        "kind": kind,
        "source": receipt.get("source"),
        "detail": f"verified {kind} at {target}",
    }


def install_downloaded_artifact(
    config: dict, path: Path, dry_run: bool = False, windows_silent: bool = False
) -> Path | InstallerLaunch:
    system = platform.system().lower()
    if system == "windows":
        return install_windows_artifact(config, path, dry_run, silent=windows_silent)
    if system == "darwin":
        app_dir = expand(config["browser"]["install_locations"]["darwin_app_dir"])
        installed = install_macos_app_archive(path, app_dir, dry_run)
        if installed is not None:
            return installed
        raise RuntimeError(
            f"Browser artifact is not an installable app archive: {path.name}"
        )
    if system == "linux":
        return prepare_linux_artifact(path, dry_run)
    raise RuntimeError(f"unsupported Browser platform: {system}")


def maybe_launch_installed(config: dict, path: Path, dry_run: bool) -> None:
    """Launch an already verified installation; never open an installer."""
    system = platform.system().lower()
    if system == "darwin":
        if path.suffix.lower() != ".app":
            raise RuntimeError(f"verified macOS target is not an app bundle: {path}")
        if dry_run:
            print(f"[dry-run] open {path}")
            return
        subprocess.run(
            ["open", str(path)],
            stdin=subprocess.DEVNULL,
            timeout=10,
            check=True,
        )
        return

    if system == "linux":
        suffix = path.suffix.lower()
        if suffix != ".appimage":
            raise RuntimeError(f"verified Linux target is not an AppImage: {path}")
        if dry_run:
            print(f"[dry-run] launch {path}")
            return
        subprocess.Popen(
            [str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return

    if system != "windows":
        raise RuntimeError(f"unsupported Browser platform: {system}")

    target = path
    if target.name.lower() != config["browser"]["install_receipt"][
        "windows_main_executable"
    ].lower():
        raise RuntimeError(f"verified Windows target has wrong executable name: {target}")
    if dry_run:
        print(f"[dry-run] launch installed Browser: {target}")
        return
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise OSError("Windows os.startfile is unavailable")
    startfile(str(target))


def prepare_linux_artifact(path: Path, dry_run: bool) -> Path:
    if platform.system().lower() != "linux":
        raise RuntimeError(f"unsupported Browser artifact: {path.name}")
    if path.suffix.lower() == ".appimage":
        if dry_run:
            print(f"[dry-run] chmod +x {path}")
        else:
            path.chmod(path.stat().st_mode | 0o111)
        print(f"{'planned AppImage' if dry_run else 'installed AppImage'}: {path}")
        return path
    elif path.suffix.lower() in {".deb", ".rpm"}:
        raise RuntimeError(
            f"{path.suffix} package download is not a verifiable installation; "
            "publish an AppImage for this protocol"
        )
    raise RuntimeError(f"unsupported Linux Browser artifact: {path.name}")


def expand(path: str) -> Path:
    # Git Bash pre-expands ~ into an MSYS /c/... path that native Windows
    # Python would resolve as C:\c\...; normalize before touching the filesystem.
    return Path(path_compat.native_path_text(os.path.expandvars(os.path.expanduser(path))))


def resolve_mcp_port(mcp: dict) -> int:
    res = mcp.get("port_resolution") or {}
    pref = res.get("pref_file")
    if pref:
        try:
            value = int(expand(pref).read_text(encoding="utf-8").strip())
            if value >= 1024:
                return value
        except (OSError, ValueError):
            pass
    env_name = res.get("env", "LLM_WIKI_PORT")
    env_value = os.environ.get(env_name, "").strip()
    if env_value.isdigit() and int(env_value) >= 1024:
        return int(env_value)
    return res.get("default", 8800)


def read_mcp_token(mcp: dict) -> str | None:
    token_file = mcp.get("token_file")
    if not token_file:
        return None
    try:
        value = expand(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _config_node(host: dict, server_name: str) -> tuple[bool | None, object | None, str]:
    """Return registration state, isolated config node when possible, raw text."""
    config_path = host.get("config_path")
    if not config_path:
        return None, None, ""
    path = expand(config_path)
    if not path.is_file():
        return False, None, ""
    check = host.get("registered_check") or {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False, None, ""
    if check.get("format") == "json":
        try:
            node = json.loads(text)
        except json.JSONDecodeError:
            return False, None, text
        for key in check.get("pointer", []):
            if not isinstance(node, dict) or key not in node:
                return False, None, text
            node = node[key]
        return True, node, text
    marker = check.get("marker") or server_name
    return marker in text, None, text


def host_mcp_registered(host: dict, server_name: str) -> bool | None:
    return _config_node(host, server_name)[0]


def host_mcp_transport(host: dict, server_name: str) -> str | None:
    """Best-effort transport classification for exact-state validation."""
    registered, node, text = _config_node(host, server_name)
    if not registered:
        return None
    sample = json.dumps(node, ensure_ascii=False) if node is not None else text
    lowered = sample.lower()
    if "mcp-stdio-bridge.py" in lowered:
        return "stdio"
    if (isinstance(node, dict) and node.get("command")) or "mcp-remote" in lowered:
        return "foreign-stdio"
    if "127.0.0.1" in lowered or "localhost" in lowered or "[::1]" in lowered:
        return "http-loopback"
    if "http://" in lowered or "https://" in lowered or 'type = "http"' in lowered:
        return "http-remote"
    return "unknown"


def _replace_placeholders(value, replacements: dict[str, str]):
    if isinstance(value, str):
        for name, replacement in replacements.items():
            value = value.replace("{" + name + "}", replacement)
        return value
    if isinstance(value, list):
        return [_replace_placeholders(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_placeholders(item, replacements) for key, item in value.items()}
    return value


def display_argv(argv: list[str], system: str | None = None) -> str:
    """Render only for the user; execution always uses the argv list directly."""
    if (system or platform.system()).lower() == "windows":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def preferred_python_executable(system: str | None = None) -> str:
    """Choose a host-stable absolute Python path for long-lived MCP config.

    On Unix, sys.executable may resolve inside a versioned Homebrew/pyenv
    directory.  Preserve the stable python3 shim instead.  On Windows the native
    executable is preferable because MCP hosts do not execute through Git Bash
    and therefore need a directly launchable .exe path.
    """
    if (system or platform.system()).lower() == "windows":
        return os.path.abspath(sys.executable)
    candidate = shutil.which("python3") or sys.executable
    return os.path.abspath(candidate)


def build_mcp_commands(
    config: dict,
    *,
    hosts: list[str],
    root: Path = ROOT,
    python_executable: str | None = None,
    system: str | None = None,
) -> list[dict]:
    """Build exact MCP actions for explicitly selected host ids."""
    mcp = config.get("mcp") or {}
    host_catalog = mcp.get("hosts") or {}
    unknown = sorted(set(hosts) - set(host_catalog))
    if unknown:
        raise ValueError(f"unknown MCP host(s): {', '.join(unknown)}")
    server_name = mcp.get("server_name", "my-llm-wiki")
    port = resolve_mcp_port(mcp)
    endpoint = mcp.get("endpoint", "http://127.0.0.1:{port}/mcp").replace("{port}", str(port))
    token = read_mcp_token(mcp)
    bridge_rel = mcp.get("stdio_bridge_script", "scripts/mcp-stdio-bridge.py")
    bridge_script = (root / bridge_rel).resolve()
    # An explicit executable is already an exact host recipe value. Preserve it
    # byte-for-byte; only auto-discovered executables need normalization.
    python_path = python_executable or preferred_python_executable(system)
    replacements = {
        "endpoint": endpoint,
        "token": token or "<Browser token>",
        "python": python_path,
        "bridge_script": str(bridge_script),
    }
    rows = []
    for name in dict.fromkeys(hosts):
        host = host_catalog[name]
        argv = _replace_placeholders(host.get("register_argv"), replacements)
        unregister_argv = _replace_placeholders(host.get("unregister_argv"), replacements)
        manual_config = _replace_placeholders(host.get("manual_config"), replacements)
        rows.append(
            {
                "host": name,
                "cli": host.get("cli"),
                "argv": argv,
                "command": display_argv(argv, system) if argv else None,
                "unregister_argv": unregister_argv,
                "unregister_command": display_argv(unregister_argv, system) if unregister_argv else None,
                "manual_config": manual_config,
                "config_path": str(expand(host["config_path"])) if host.get("config_path") else None,
                "note": host.get("register_note"),
                "registered": host_mcp_registered(host, server_name),
                "transport": host_mcp_transport(host, server_name),
                "cli_available": bool(host.get("cli")) and shutil.which(host["cli"]) is not None,
                "bridge_script": str(bridge_script),
                "bridge_available": bridge_script.is_file(),
            }
        )
    return rows


def _run_host_command(argv: list[str]) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update({"CI": "1", "NO_COLOR": "1"})
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )


def _config_snapshot(row: dict) -> tuple[Path | None, bytes | None, int | None]:
    if not row.get("config_path"):
        return None, None, None
    path = Path(row["config_path"])
    try:
        if not path.is_file():
            return path, None, None
        return path, path.read_bytes(), path.stat().st_mode
    except OSError as exc:
        raise RuntimeError(f"cannot snapshot {path}: {exc}") from exc


def _restore_snapshot(snapshot: tuple[Path | None, bytes | None, int | None]) -> None:
    path, content, mode = snapshot
    if path is None:
        return
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.restore-{uuid.uuid4().hex}")
    temp.write_bytes(content)
    if mode is not None:
        temp.chmod(mode)
    os.replace(temp, path)


def register_mcp(config: dict, hosts: list[str], dry_run: bool = False) -> int:
    """Register selected hosts without prompts; host selection is consent."""
    install_state = browser_install_state(config)
    if not install_state["ok"]:
        print(
            "MCP registration refused: " + install_state["detail"] + ". "
            "Complete `python3 scripts/install-browser.py` first.",
            file=sys.stderr,
        )
        return 1
    rows = build_mcp_commands(config, hosts=hosts)
    failures = 0
    actions_required = 0
    for row in rows:
        label = f"  [{row['host']}]"
        if row["registered"]:
            if row["transport"] == "stdio":
                print(f"{label} already registered (stdio) — skipping")
            else:
                remove = row["unregister_command"] or f"remove the entry from {row['config_path']}"
                print(
                    f"{label} conflicting MCP registration ({row['transport'] or 'unknown'}); "
                    f"remove it explicitly first: {remove}",
                    file=sys.stderr,
                )
                failures += 1
            continue
        if not row["command"]:
            if row["manual_config"]:
                destination = f" in {row['config_path']}" if row["config_path"] else ""
                print(f"{label} apply this config manually{destination}:")
                print(json.dumps(row["manual_config"], ensure_ascii=False, indent=2))
                if row["note"]:
                    print(f"{label} {row['note']}")
                actions_required += 1
            else:
                print(f"{label} MCP registration is unsupported for this host", file=sys.stderr)
                failures += 1
            continue
        print(f"{label} proposed: {row['command']}")
        if row["note"]:
            print(f"          note: {row['note']}")
        if not row["bridge_available"]:
            print(f"          bridge missing: {row['bridge_script']}")
            failures += 1
            continue
        if not row["cli_available"]:
            print(f"          `{row['cli']}` not on PATH", file=sys.stderr)
            failures += 1
            continue
        if dry_run:
            print("          [dry-run] not executed")
            continue
        snapshot = _config_snapshot(row)
        try:
            result = _run_host_command(row["argv"])
            if result.returncode != 0:
                raise RuntimeError(
                    f"register failed ({result.returncode}): {result.stderr.strip()}"
                )
            print("          registered")
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            _restore_snapshot(snapshot)
            failures += 1
            print(f"          failed; config restored: {exc}", file=sys.stderr)
    if failures:
        return 1
    if actions_required:
        return 3
    return 0


def unregister_mcp(config: dict, hosts: list[str], dry_run: bool = False) -> int:
    """Remove Browser MCP entries so an uninstalled Browser leaves no broken
    host config behind. Uses each host's own `mcp remove`."""
    rows = build_mcp_commands(config, hosts=hosts)
    failures = 0
    actions_required = 0
    for row in rows:
        if not row["registered"]:
            continue
        if not row["unregister_command"]:
            location = row["config_path"] or "the host MCP config"
            print(f"  [{row['host']}] remove the my-llm-wiki entry manually from {location}")
            actions_required += 1
            continue
        print(f"  [{row['host']}] {row['unregister_command']}")
        if dry_run:
            print("          [dry-run] not executed")
            continue
        if not row["cli_available"]:
            print(f"          `{row['cli']}` not on PATH", file=sys.stderr)
            failures += 1
            continue
        try:
            result = _run_host_command(row["unregister_argv"])
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"          command failed: {exc}", file=sys.stderr)
            failures += 1
            continue
        if result.returncode != 0:
            print(
                f"          command failed ({result.returncode}): {result.stderr.strip()}",
                file=sys.stderr,
            )
            failures += 1
        else:
            print("          removed")
    if failures:
        return 1
    if actions_required:
        return 3
    return 0


def source_build(config: dict, dry_run: bool) -> None:
    source = config["browser"]["source_build"]
    for step in source.get("steps", []):
        cmd = step["argv"]
        cwd = ROOT / step["cwd"]
        timeout = int(step["timeout_seconds"])
        if dry_run:
            print(f"[dry-run] ({cwd}) {' '.join(cmd)}")
            continue
        subprocess.run(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )


def perform_browser_install(config: dict, args: argparse.Namespace) -> int:
    repo = args.repo or infer_repo()
    if repo:
        print(f"GitHub fallback repo: {repo}")
    else:
        print("GitHub fallback repo: unavailable (project-owned release source will still be tried)")
    print(f"version: {args.version}")
    print("strategy: download release first")
    if args.dry_run:
        keys = tauri_platform_keys()
        if not keys:
            print(
                f"unsupported Browser platform: {platform.system()} {platform.machine()}",
                file=sys.stderr,
            )
            return 1
        print("status: planned")
        print("release sources: " + ", ".join(
            str(source.get("name")) for source in config["browser"]["release_sources"]
        ))
        print(f"platform candidates: {', '.join(keys)}")
        print(f"destination: {expand(args.download_dir)}")
        if args.fallback_source:
            print("source fallback: enabled by explicit flag")
        return 0

    try:
        dest, open_target, source = install_release(
            config,
            repo,
            args.version,
            expand(args.download_dir),
            dry_run=args.dry_run,
            skip_network_probe=args.dry_run,
            prepare=lambda artifact: install_downloaded_artifact(
                config, artifact, args.dry_run,
                windows_silent=getattr(args, "windows_silent", False),
            ),
        )
        print(f"release source: {source}")
        if isinstance(open_target, InstallerLaunch):
            print(f"installer: {open_target.artifact}")
            print("status: installer-launched")
            print(
                "Complete the installation in the Windows UI. "
                "This process will not wait, poll the registry, or launch Browser afterward."
            )
            return 0
        try:
            write_install_receipt(
                config,
                artifact=dest,
                target=open_target,
                source=source,
                requested_version=args.version,
            )
        except OSError as exc:
            raise InstallerCompletionError(
                f"Browser is installed but its receipt could not be written: {exc}"
            ) from exc
        if args.open:
            try:
                maybe_launch_installed(config, open_target, args.dry_run)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                print(
                    f"Browser is installed and verified, but launch failed: {exc}",
                    file=sys.stderr,
                )
                return 3
        return 0
    except (
        OSError,
        TimeoutError,
        urllib.error.HTTPError,
        urllib.error.URLError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as err:
        print(f"release install unavailable: {err}", file=sys.stderr)
        if isinstance(err, InstallerCompletionError):
            print(
                "No installation receipt was written.",
                file=sys.stderr,
            )
            return 1
        if not args.fallback_source:
            if isinstance(err, ReleaseSourcesUnavailable):
                print(
                    "Browser is optional: continue with wiki_ops.py local-search. "
                    "The project-owned htmlgo release source was tried before "
                    "GitHub; do not use a third-party relay by default.",
                    file=sys.stderr,
                )
            else:
                print("Re-run with --fallback-source to build the Tauri app locally.", file=sys.stderr)
            return 1
        print("falling back to source build")
        try:
            source_build(config, args.dry_run)
        except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError) as exc:
            print(f"source build failed: {exc}", file=sys.stderr)
            return 1
        print(
            "development build completed, but it is not an installed release; "
            "no installation receipt was written",
            file=sys.stderr,
        )
        return 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="GitHub repo in owner/name form. Defaults to git origin.")
    parser.add_argument("--version", default="latest", help="Release tag to install, or latest.")
    parser.add_argument(
        "--download-dir",
        help="Override the registry's permanent release artifact directory.",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help=(
            "Launch Browser after a synchronous install. Windows setup.exe/MSI "
            "launches the installer and returns without monitoring it."
        ),
    )
    parser.add_argument(
        "--fallback-source",
        action="store_true",
        help="Build from source if release download is unavailable.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without changing files.")
    parser.add_argument(
        "--windows-silent",
        action="store_true",
        help=(
            "Windows only: run the .exe/.msi installer silently, wait for it, "
            "and verify the registered install (for Setup orchestration)."
        ),
    )
    mcp_group = parser.add_mutually_exclusive_group()
    mcp_group.add_argument(
        "--register-mcp",
        action="store_true",
        help="Register Browser MCP for each explicit --host (no download).",
    )
    mcp_group.add_argument(
        "--unregister-mcp",
        action="store_true",
        help="Remove Browser MCP for each explicit --host (no download).",
    )
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        help="Named MCP host from registry/bootstrap.json; repeatable.",
    )
    args = parser.parse_args()

    try:
        config = load_bootstrap()
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"install-browser: invalid bootstrap config: {exc}", file=sys.stderr)
        return 2
    if args.download_dir is None:
        args.download_dir = config["browser"]["download_policy"]["directory"]

    if args.register_mcp or args.unregister_mcp:
        if not args.host:
            parser.error("--register-mcp/--unregister-mcp requires at least one --host")
        try:
            def run_mcp_action() -> int:
                if args.unregister_mcp:
                    return unregister_mcp(config, args.host, dry_run=args.dry_run)
                return register_mcp(config, args.host, dry_run=args.dry_run)
            if args.dry_run:
                return run_mcp_action()
            lock_path = expand(config["browser"]["operation_lock_file"])
            with advisory_lock(lock_path):
                return run_mcp_action()
        except LockUnavailable as exc:
            print(f"install-browser: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            parser.error(str(exc))
    if args.host:
        parser.error("--host is only valid with --register-mcp or --unregister-mcp")

    if args.dry_run:
        return perform_browser_install(config, args)
    try:
        with advisory_lock(expand(config["browser"]["operation_lock_file"])):
            return perform_browser_install(config, args)
    except LockUnavailable as exc:
        print(f"install-browser: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
