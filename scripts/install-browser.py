#!/usr/bin/env python3
"""Install My LLM Wiki Browser with a release-first strategy.

Default behavior:
1. Read the project-owned Tauri release manifest (htmlgo).
2. Fall back to the latest GitHub Release if the first-party source fails.
3. Download the best matching Browser asset for this OS/arch. On Windows the
   portable zip is preferred over setup.exe and auto-extracted next to the
   download — extraction is the whole install.
4. Optionally open the downloaded installer on macOS.

If no release or matching asset exists, the script exits with a clear message.
Use --fallback-source to build the Tauri app from source as a developer fallback.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse


ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "registry" / "bootstrap.json"
NETWORK_PROBE = ROOT / "skills" / "cn-mirrors" / "scripts" / "net_probe.py"


class ReleaseSourcesUnavailable(RuntimeError):
    """Every configured Browser release source failed or was unreachable."""


def load_bootstrap() -> dict:
    return json.loads(BOOTSTRAP.read_text(encoding="utf-8"))


def run(cmd: list[str], cwd: Path | None = None) -> str:
    return subprocess.check_output(cmd, cwd=cwd, text=True).strip()


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
        return ["windows-x64"]
    if system == "linux":
        return ["linux-x64"]
    return []


def json_url(url: str, *, timeout: float = 30, headers: dict[str, str] | None = None) -> dict:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "llm-wiki-suite-bootstrap",
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


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
        return ["windows-x86_64-nsis", "windows-x86_64", "windows-x86_64-msi"]
    if system == "linux":
        return ["linux-x86_64-appimage", "linux-x86_64", "linux-x86_64-deb", "linux-x86_64-rpm"]
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
            normalized = normalize_manifest_asset_url(asset_url, url)
            return {
                "name": Path(unquote(urlparse(normalized).path)).name,
                "browser_download_url": normalized,
                "version": release.get("version", ""),
                "source": source.get("name", "tauri"),
            }
    raise RuntimeError(
        f"{source.get('name', 'tauri')} has no installable asset for "
        f"{platform.system()} {platform.machine()}"
    )


def blocked_github_release_hosts(timeout: float = 4.0) -> list[str]:
    """Return blocked GitHub hosts needed for a release install, if probeable.

    The suite's cn-mirrors probe is intentionally dependency-free and runs its
    requests concurrently.  Do not mistake its own absence/failure for a
    network failure: the legacy download path remains the fallback in that case.
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
            if statuses.get(host) == "blocked"]


def resolve_release_asset(
    config: dict,
    repo: str | None,
    version: str,
    *,
    skip_network_probe: bool = False,
) -> tuple[dict, str]:
    """Try project-owned release sources first, then canonical GitHub."""
    browser = config.get("browser") or {}
    sources = browser.get("release_sources") or [{"name": "github", "format": "github-release"}]
    errors: list[str] = []
    github_blocked: list[str] | None = None

    for source in sources:
        if not isinstance(source, dict):
            errors.append("invalid release source entry")
            continue
        name = str(source.get("name") or source.get("format") or "unknown")
        format_name = source.get("format")
        try:
            if format_name == "tauri-latest":
                if version != "latest":
                    errors.append(f"{name}: only supports --version latest")
                    continue
                return tauri_manifest_asset(source), name
            if format_name == "github-release":
                if not repo:
                    errors.append(f"{name}: no GitHub repo available for fallback")
                    continue
                if not skip_network_probe:
                    if github_blocked is None:
                        github_blocked = blocked_github_release_hosts()
                    if github_blocked:
                        errors.append(f"{name}: blocked ({', '.join(github_blocked)})")
                        continue
                release = release_metadata(repo, version)
                asset = pick_asset(release, browser["asset_patterns"])
                if not asset:
                    names = ", ".join(item.get("name", "") for item in release.get("assets", [])) or "(none)"
                    raise RuntimeError(f"no matching Browser asset. Release assets: {names}")
                asset["source"] = name
                return asset, name
            errors.append(f"{name}: unsupported release source format {format_name!r}")
        except (OSError, urllib.error.HTTPError, urllib.error.URLError, RuntimeError, ValueError) as exc:
            errors.append(f"{name}: {exc}")

    raise ReleaseSourcesUnavailable("all configured release sources unavailable: " + "; ".join(errors))


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


def download_asset(asset: dict, dest_dir: Path, dry_run: bool = False) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / asset["name"]
    url = asset["browser_download_url"]
    if dry_run:
        print(f"[dry-run] download {url} -> {dest}")
        return dest

    headers = {"User-Agent": "llm-wiki-suite-bootstrap"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out)
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

    with zipfile.ZipFile(path) as archive:
        archive.extractall(dest)
    print(f"extracted: {dest}")
    for exe in sorted(dest.rglob("*.exe")):
        print(f"run this to start the app: {exe}")
        break
    return dest


def maybe_open(path: Path, dry_run: bool) -> None:
    if platform.system().lower() != "darwin":
        return
    if not path.name.lower().endswith(".dmg"):
        return
    if dry_run:
        print(f"[dry-run] open {path}")
        return
    subprocess.run(["open", str(path)], check=False)


def expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


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
    env_name = res.get("env", "PORT")
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
    """Best-effort transport classification for doctor/migration guidance."""
    registered, node, text = _config_node(host, server_name)
    if not registered:
        return None
    sample = json.dumps(node, ensure_ascii=False) if node is not None else text
    lowered = sample.lower()
    if "mcp-stdio-bridge.py" in lowered:
        return "stdio"
    if (isinstance(node, dict) and node.get("command")) or "mcp-remote" in lowered:
        return "stdio-external"
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
    root: Path = ROOT,
    python_executable: str | None = None,
    system: str | None = None,
) -> list[dict]:
    """One row per host the user actually has: the exact registration command
    (placeholders resolved), current registration state, and any note."""
    mcp = config.get("mcp") or {}
    hosts = mcp.get("hosts") or {}
    server_name = mcp.get("server_name", "my-llm-wiki")
    port = resolve_mcp_port(mcp)
    endpoint = mcp.get("endpoint", "http://127.0.0.1:{port}/mcp").replace("{port}", str(port))
    token = read_mcp_token(mcp)
    bridge_rel = mcp.get("stdio_bridge_script", "scripts/mcp-stdio-bridge.py")
    bridge_script = (root / bridge_rel).resolve()
    python_path = os.path.abspath(python_executable or preferred_python_executable(system))
    replacements = {
        "endpoint": endpoint,
        "token": token or "<Browser token>",
        "python": python_path,
        "bridge_script": str(bridge_script),
    }
    rows = []
    for name, host in hosts.items():
        if not expand(host.get("detect_dir", "~/.%s" % name)).is_dir():
            continue  # user does not use this host
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


def propose_mcp_registration(config: dict, assume_yes: bool = False, dry_run: bool = False) -> None:
    """Offer (never force) MCP registration for each detected host.

    Consent contract: show the exact host-native `mcp add` command, run it only
    on explicit confirmation (or --yes), skip silently otherwise. We never edit
    a host's config file ourselves — the host CLI owns its format.
    """
    rows = build_mcp_commands(config)
    if not rows:
        return
    print("\nMCP registration (optional — skills work without it via the CLI fallback):")
    interactive = sys.stdin.isatty() and not assume_yes
    for row in rows:
        label = f"  [{row['host']}]"
        migrating = row["registered"] and row["transport"] in {"http-loopback", "stdio-external"}
        if row["registered"] and not migrating:
            suffix = f" ({row['transport']})" if row["transport"] else ""
            print(f"{label} already registered{suffix} — skipping")
            continue
        if migrating:
            reason = (
                "legacy loopback HTTP"
                if row["transport"] == "http-loopback"
                else "external stdio bridge"
            )
            print(f"{label} {reason} registration detected — replace with suite stdio bridge")
        if not row["command"]:
            if row["manual_config"]:
                action = "replace the existing entry with" if migrating else "apply this config manually"
                destination = f" in {row['config_path']}" if row["config_path"] else ""
                print(f"{label} {action}{destination}:")
                print(json.dumps(row["manual_config"], ensure_ascii=False, indent=2))
            if row["note"]:
                print(f"{label} {row['note']}")
            continue
        print(f"{label} proposed: {row['command']}")
        if row["note"]:
            print(f"          note: {row['note']}")
        if not row["bridge_available"]:
            print(f"          bridge missing: {row['bridge_script']}")
            continue
        if not row["cli_available"]:
            print(f"          `{row['cli']}` not on PATH — run the command yourself when available")
            continue
        if dry_run:
            print("          [dry-run] not executed")
            continue
        if interactive:
            answer = input(f"          register {row['host']} now? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                print("          skipped")
                continue
        elif not assume_yes:
            print("          (non-interactive: not executed — re-run with --register-mcp --yes to apply)")
            continue
        if migrating and row["unregister_argv"]:
            removed = subprocess.run(row["unregister_argv"], check=False)
            if removed.returncode != 0:
                print(f"          remove failed (exit {removed.returncode}); keeping existing registration")
                continue
        result = subprocess.run(row["argv"], check=False)
        print(f"          {'registered' if result.returncode == 0 else f'command failed (exit {result.returncode})'}")


def unregister_mcp(config: dict, dry_run: bool = False) -> None:
    """Remove Browser MCP entries so an uninstalled Browser leaves no broken
    host config behind. Uses each host's own `mcp remove`."""
    rows = build_mcp_commands(config)
    for row in rows:
        if not row["registered"]:
            continue
        if not row["unregister_command"]:
            location = row["config_path"] or "the host MCP config"
            print(f"  [{row['host']}] remove the my-llm-wiki entry manually from {location}")
            continue
        print(f"  [{row['host']}] {row['unregister_command']}")
        if dry_run:
            print("          [dry-run] not executed")
            continue
        if not row["cli_available"]:
            print(f"          `{row['cli']}` not on PATH — run the command yourself")
            continue
        result = subprocess.run(row["unregister_argv"], check=False)
        print(f"          {'removed' if result.returncode == 0 else f'command failed (exit {result.returncode})'}")


def source_build(config: dict, dry_run: bool) -> None:
    source = config["browser"]["source_build"]
    frontend_dir = ROOT / source["frontend_dir"]
    tauri_dir = ROOT / source["tauri_dir"]

    steps = [
        (["npm", "ci"], frontend_dir),
        (["npm", "run", "build"], frontend_dir),
        (["cargo", "tauri", "build"], tauri_dir),
    ]
    for cmd, cwd in steps:
        if dry_run:
            print(f"[dry-run] ({cwd}) {' '.join(cmd)}")
            continue
        subprocess.run(cmd, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="GitHub repo in owner/name form. Defaults to git origin.")
    parser.add_argument("--version", default="latest", help="Release tag to install, or latest.")
    parser.add_argument(
        "--download-dir",
        default="~/Downloads",
        help="Where to save the release asset. Default: ~/Downloads.",
    )
    parser.add_argument("--open", action="store_true", help="Open the downloaded macOS DMG.")
    parser.add_argument(
        "--fallback-source",
        action="store_true",
        help="Build from source if release download is unavailable.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without changing files.")
    parser.add_argument(
        "--skip-network-probe",
        action="store_true",
        help="Skip the cn-mirrors GitHub-release reachability check.",
    )
    parser.add_argument(
        "--register-mcp",
        action="store_true",
        help="Only propose/apply Browser MCP registration for detected hosts (no download).",
    )
    parser.add_argument(
        "--unregister-mcp",
        action="store_true",
        help="Remove Browser MCP entries from detected hosts (cleanup before/after uninstall).",
    )
    parser.add_argument(
        "--skip-mcp",
        action="store_true",
        help="Do not propose MCP registration after install.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="With --register-mcp: apply without interactive confirmation.",
    )
    args = parser.parse_args()

    config = load_bootstrap()

    if args.unregister_mcp:
        unregister_mcp(config, dry_run=args.dry_run)
        return 0
    if args.register_mcp:
        propose_mcp_registration(config, assume_yes=args.yes, dry_run=args.dry_run)
        return 0

    repo = args.repo or infer_repo()
    if repo:
        print(f"GitHub fallback repo: {repo}")
    else:
        print("GitHub fallback repo: unavailable (project-owned release source will still be tried)")
    print(f"version: {args.version}")
    print("strategy: download release first")

    try:
        asset, source = resolve_release_asset(
            config,
            repo,
            args.version,
            skip_network_probe=args.skip_network_probe or args.dry_run,
        )
        print(f"release source: {source}")

        dest = download_asset(asset, Path(args.download_dir).expanduser(), args.dry_run)
        maybe_extract_zip(dest, args.dry_run)
        if args.open:
            maybe_open(dest, args.dry_run)
        if not args.skip_mcp:
            propose_mcp_registration(config, dry_run=args.dry_run)
        return 0
    except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as err:
        print(f"release install unavailable: {err}", file=sys.stderr)
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
        source_build(config, args.dry_run)
        if not args.skip_mcp:
            propose_mcp_registration(config, dry_run=args.dry_run)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
