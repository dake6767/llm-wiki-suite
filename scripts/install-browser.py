#!/usr/bin/env python3
"""Install My LLM Wiki Browser with a release-first strategy.

Default behavior:
1. Infer the GitHub repo from --repo or git remote origin.
2. Query the latest GitHub Release.
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
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "registry" / "bootstrap.json"


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


def github_json(url: str) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "llm-wiki-suite-bootstrap",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def release_metadata(repo: str, version: str) -> dict:
    if version == "latest":
        url = f"https://api.github.com/repos/{repo}/releases/latest"
    else:
        url = f"https://api.github.com/repos/{repo}/releases/tags/{version}"
    return github_json(url)


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
    return Path(os.path.expanduser(path))


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


def host_mcp_registered(host: dict, server_name: str) -> bool | None:
    """True/False when determinable, None when the host declares no config."""
    config_path = host.get("config_path")
    if not config_path:
        return None
    path = expand(config_path)
    if not path.is_file():
        return False
    check = host.get("registered_check") or {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if check.get("format") == "json":
        try:
            node = json.loads(text)
        except json.JSONDecodeError:
            return False
        for key in check.get("pointer", []):
            if not isinstance(node, dict) or key not in node:
                return False
            node = node[key]
        return True
    marker = check.get("marker") or server_name
    return marker in text


def build_mcp_commands(config: dict) -> list[dict]:
    """One row per host the user actually has: the exact registration command
    (placeholders resolved), current registration state, and any note."""
    mcp = config.get("mcp") or {}
    hosts = mcp.get("hosts") or {}
    server_name = mcp.get("server_name", "my-llm-wiki")
    port = resolve_mcp_port(mcp)
    endpoint = mcp.get("endpoint", "http://127.0.0.1:{port}/mcp").replace("{port}", str(port))
    token = read_mcp_token(mcp)
    rows = []
    for name, host in hosts.items():
        if not expand(host.get("detect_dir", "~/.%s" % name)).is_dir():
            continue  # user does not use this host
        command = host.get("register_command")
        if command:
            command = command.replace("{endpoint}", endpoint)
            command = command.replace(
                "{token}", token or "<run the Browser once, then paste ~/.my-llm-wiki/connector/token>"
            )
        rows.append(
            {
                "host": name,
                "cli": host.get("cli"),
                "command": command,
                "unregister_command": host.get("unregister_command"),
                "note": host.get("register_note"),
                "registered": host_mcp_registered(host, server_name),
                "cli_available": bool(host.get("cli")) and shutil.which(host["cli"]) is not None,
                "token_missing": token is None,
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
        if row["registered"]:
            print(f"{label} already registered — skipping")
            continue
        if not row["command"]:
            if row["note"]:
                print(f"{label} {row['note']}")
            continue
        print(f"{label} proposed: {row['command']}")
        if row["note"]:
            print(f"          note: {row['note']}")
        if row["token_missing"]:
            print("          note: no token yet (~/.my-llm-wiki/connector/token) — start the Browser once first")
        if not row["cli_available"]:
            print(f"          `{row['cli']}` not on PATH — run the command yourself when available")
            continue
        if dry_run:
            print("          [dry-run] not executed")
            continue
        if row["token_missing"]:
            continue  # a command with a placeholder token must not be executed
        if interactive:
            answer = input(f"          register {row['host']} now? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                print("          skipped")
                continue
        elif not assume_yes:
            print("          (non-interactive: not executed — re-run with --register-mcp --yes to apply)")
            continue
        result = subprocess.run(row["command"], shell=True, check=False)
        print(f"          {'registered' if result.returncode == 0 else f'command failed (exit {result.returncode})'}")


def unregister_mcp(config: dict, dry_run: bool = False) -> None:
    """Remove Browser MCP entries so an uninstalled Browser leaves no broken
    host config behind. Uses each host's own `mcp remove`."""
    rows = build_mcp_commands(config)
    for row in rows:
        if not row["registered"] or not row["unregister_command"]:
            continue
        print(f"  [{row['host']}] {row['unregister_command']}")
        if dry_run:
            print("          [dry-run] not executed")
            continue
        if not row["cli_available"]:
            print(f"          `{row['cli']}` not on PATH — run the command yourself")
            continue
        result = subprocess.run(row["unregister_command"], shell=True, check=False)
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
    if not repo:
        print("Could not infer GitHub repo. Pass --repo owner/name.", file=sys.stderr)
        return 2

    print(f"repo: {repo}")
    print(f"version: {args.version}")
    print("strategy: download release first")

    try:
        release = release_metadata(repo, args.version)
        asset = pick_asset(release, config["browser"]["asset_patterns"])
        if not asset:
            names = ", ".join(asset.get("name", "") for asset in release.get("assets", [])) or "(none)"
            raise RuntimeError(f"No matching Browser asset for this platform. Release assets: {names}")

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
            print("Re-run with --fallback-source to build the Tauri app locally.", file=sys.stderr)
            return 1
        print("falling back to source build")
        source_build(config, args.dry_run)
        if not args.skip_mcp:
            propose_mcp_registration(config, dry_run=args.dry_run)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
