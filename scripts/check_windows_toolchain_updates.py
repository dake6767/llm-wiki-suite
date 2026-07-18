#!/usr/bin/env python3
"""Report upstream candidates for the pinned Windows Setup toolchain.

This checker is deliberately read-only. It never edits the production lock,
downloads release payloads, computes replacement hashes, or promotes a
candidate. Release engineering reviews its report, updates the lock in a PR,
and lets Windows build/clean-room CI produce immutable component archives.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "registry" / "windows-toolchain.lock.json"
USER_AGENT = "llm-wiki-windows-toolchain-watch/1"


class CheckError(RuntimeError):
    pass


def request(url: str, *, timeout: int = 20) -> bytes:
    if not url.startswith("https://"):
        raise CheckError(f"refusing non-HTTPS source: {url}")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token and urllib.parse.urlparse(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=timeout
        ) as response:
            return response.read(4 * 1024 * 1024)
    except Exception as exc:
        raise CheckError(f"{url}: {exc}") from exc


def request_json(url: str) -> dict | list:
    try:
        value = json.loads(request(url).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckError(f"invalid JSON from {url}: {exc}") from exc
    if not isinstance(value, (dict, list)):
        raise CheckError(f"unexpected JSON shape from {url}")
    return value


def numeric_version(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value)
    if not parts:
        raise CheckError(f"cannot compare version: {value!r}")
    return tuple(int(part) for part in parts)


def is_newer(candidate: str, current: str) -> bool:
    return numeric_version(candidate) > numeric_version(current)


def exact_package_version(package: str) -> str:
    try:
        name, version = package.split("==", 1)
    except ValueError as exc:
        raise CheckError(f"lock package is not exact: {package}") from exc
    if not name or not version:
        raise CheckError(f"lock package is not exact: {package}")
    return version


def pypi_latest(project: str) -> str:
    value = request_json(f"https://pypi.org/pypi/{urllib.parse.quote(project)}/json")
    if not isinstance(value, dict):
        raise CheckError(f"unexpected PyPI response for {project}")
    version = (value.get("info") or {}).get("version")
    if not isinstance(version, str) or not version:
        raise CheckError(f"PyPI returned no version for {project}")
    return version


def pypi_windows_cp312_versions(project: str) -> set[str]:
    value = request_json(f"https://pypi.org/pypi/{urllib.parse.quote(project)}/json")
    if not isinstance(value, dict) or not isinstance(value.get("releases"), dict):
        raise CheckError(f"PyPI returned no release map for {project}")
    versions = set()
    for version, files in value["releases"].items():
        if not isinstance(version, str) or not isinstance(files, list):
            continue
        if any(
            isinstance(file, dict)
            and "cp312" in str(file.get("filename", ""))
            and "win_amd64" in str(file.get("filename", ""))
            for file in files
        ):
            versions.add(version)
    if not versions:
        raise CheckError(f"PyPI has no Windows CPython 3.12 wheels for {project}")
    return versions


def github_latest(owner: str, repo: str) -> dict:
    value = request_json(f"https://api.github.com/repos/{owner}/{repo}/releases/latest")
    if not isinstance(value, dict):
        raise CheckError(f"unexpected GitHub response for {owner}/{repo}")
    return value


def collect(lock: dict) -> dict:
    rows: list[dict] = []
    errors: list[dict] = []

    def add(identifier: str, current: str, source: str, resolver, **extra) -> None:
        try:
            latest = str(resolver()).lstrip("v")
            row = {
                "id": identifier,
                "current": current,
                "candidate": latest,
                "update_available": is_newer(latest, current),
                "source": source,
                **extra,
            }
            rows.append(row)
        except CheckError as exc:
            errors.append({"id": identifier, "source": source, "error": str(exc)})

    def python_312_windows_embed() -> str:
        text = request("https://www.python.org/ftp/python/").decode("utf-8", errors="replace")
        versions = re.findall(r'href="(3\.12\.\d+)/"', text)
        if not versions:
            raise CheckError("python.org directory has no Python 3.12 releases")
        for version in sorted(set(versions), key=numeric_version, reverse=True):
            filename = f"python-{version}-embeddable-amd64.zip"
            listing = request(f"https://www.python.org/ftp/python/{version}/").decode(
                "utf-8", errors="replace"
            )
            if filename in listing:
                return version
        raise CheckError("Python 3.12 has no Windows embeddable runtime")

    add(
        "python",
        lock["python"]["version"],
        "https://www.python.org/ftp/python/",
        python_312_windows_embed,
        policy="latest reviewed 3.12 patch that still ships Windows embeddable-amd64",
    )
    add(
        "pyinstaller",
        lock["build"]["pyinstaller"],
        "https://pypi.org/project/pyinstaller/",
        lambda: pypi_latest("pyinstaller"),
        scope="build-only",
    )

    web = lock["components"]["web"]

    def node_22() -> str:
        value = request_json("https://nodejs.org/dist/index.json")
        if not isinstance(value, list):
            raise CheckError("unexpected Node distribution index")
        versions = [
            str(row.get("version", "")).lstrip("v")
            for row in value
            if isinstance(row, dict)
            and str(row.get("version", "")).startswith("v22.")
            and "win-x64-zip" in (row.get("files") or [])
        ]
        if not versions:
            raise CheckError("Node index has no v22 win-x64 zip")
        return max(versions, key=numeric_version)

    add(
        "node",
        web["node"]["version"],
        "https://nodejs.org/dist/index.json",
        node_22,
        policy="latest patch in reviewed Node 22 line",
    )

    def npm_opencli() -> tuple[str, str]:
        encoded = urllib.parse.quote("@jackwener/opencli", safe="")
        value = request_json(f"https://registry.npmjs.org/{encoded}/latest")
        if not isinstance(value, dict):
            raise CheckError("unexpected npm OpenCLI response")
        version = value.get("version")
        integrity = (value.get("dist") or {}).get("integrity")
        if not isinstance(version, str) or not isinstance(integrity, str):
            raise CheckError("npm OpenCLI response has no version/integrity")
        return version, integrity

    try:
        opencli_version, opencli_integrity = npm_opencli()
        rows.append({
            "id": "opencli",
            "current": web["opencli"]["version"],
            "candidate": opencli_version,
            "update_available": is_newer(opencli_version, web["opencli"]["version"]),
            "source": "https://registry.npmjs.org/@jackwener/opencli",
            "candidate_integrity": opencli_integrity,
        })
    except CheckError as exc:
        errors.append({
            "id": "opencli",
            "source": "https://registry.npmjs.org/@jackwener/opencli",
            "error": str(exc),
        })

    def extension_version() -> str:
        release = github_latest("jackwener", "OpenCLI")
        names = [
            str(asset.get("name", ""))
            for asset in release.get("assets", [])
            if isinstance(asset, dict)
        ]
        versions = [
            match.group(1)
            for name in names
            if (match := re.fullmatch(r"opencli-extension-v(.+)\.zip", name))
        ]
        if not versions:
            raise CheckError("latest OpenCLI release has no extension zip")
        return max(versions, key=numeric_version)

    add(
        "opencli-extension",
        web["extension"]["version"],
        "https://github.com/jackwener/OpenCLI/releases/latest",
        extension_version,
    )

    video = lock["components"]["video"]
    add(
        "yt-dlp",
        video["yt_dlp"]["version"],
        "https://github.com/yt-dlp/yt-dlp/releases/latest",
        lambda: str(github_latest("yt-dlp", "yt-dlp").get("tag_name", "")),
    )
    add(
        "ffmpeg",
        video["ffmpeg"]["version"],
        "https://www.gyan.dev/ffmpeg/builds/release-version",
        lambda: request("https://www.gyan.dev/ffmpeg/builds/release-version")
        .decode("ascii")
        .strip(),
    )

    asr_zh_packages = {
        package.split("==", 1)[0]: exact_package_version(package)
        for package in lock["components"]["asr-zh"]["packages"]
    }
    torch_version = asr_zh_packages.get("torch", "")
    torchaudio_version = asr_zh_packages.get("torchaudio", "")

    def pytorch_windows_pair() -> str:
        if not torch_version or torch_version != torchaudio_version:
            raise CheckError("lock must pin torch and torchaudio to one version")
        common = pypi_windows_cp312_versions("torch") & pypi_windows_cp312_versions(
            "torchaudio"
        )
        if not common:
            raise CheckError("torch and torchaudio have no common Windows CPython 3.12 release")
        return max(common, key=numeric_version)

    add(
        "torch+torchaudio",
        torch_version,
        "https://pypi.org/project/torch/ + https://pypi.org/project/torchaudio/",
        pytorch_windows_pair,
        policy="promote only a matching pair with Windows CPython 3.12 wheels",
    )

    packages = {
        "markitdown": lock["components"]["documents"]["packages"][0],
        **{
            package.split("==", 1)[0]: package
            for component in ("asr-zh", "asr-other")
            for package in lock["components"][component]["packages"]
        },
    }
    for project, package in packages.items():
        if project in {"torch", "torchaudio"}:
            continue
        add(
            project,
            exact_package_version(package),
            f"https://pypi.org/project/{project}/",
            lambda project=project: pypi_latest(project),
        )

    return {
        "schema": 1,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lock": str(LOCK.relative_to(ROOT)),
        "updates": [row for row in rows if row["update_available"]],
        "current": [row for row in rows if not row["update_available"]],
        "errors": errors,
        "policy": {
            "promotion": "reviewed PR + Windows build/clean-room CI + new immutable release",
            "user_side_update": "forbidden",
            "hash_refresh": "required for every changed direct asset and final component archive",
        },
    }


def markdown(report: dict) -> str:
    lines = [
        "# Windows toolchain update candidates",
        "",
        f"Checked: `{report['checked_at']}`",
        "",
        "This report is advisory. It does not change the production lock or any release.",
        "",
    ]
    if report["updates"]:
        lines += ["| Tool | Locked | Candidate | Official source |", "|---|---:|---:|---|"]
        for row in report["updates"]:
            lines.append(
                f"| `{row['id']}` | `{row['current']}` | `{row['candidate']}` | {row['source']} |"
            )
    else:
        lines.append("No newer candidates were found.")
    if report["errors"]:
        lines += ["", "## Probe errors", ""]
        for row in report["errors"]:
            lines.append(f"- `{row['id']}`: {row['error']}")
    lines += [
        "",
        "## Promotion gate",
        "",
        "1. Review upstream changelogs, licenses and compatibility.",
        "2. Update exact versions plus URL hashes/npm integrity in the committed lock.",
        "3. Rebuild affected Windows components and record final archive SHA-256 values.",
        "4. Pass Setup unit tests, component postchecks and PATH-empty clean-room install.",
        "5. Merge and publish a new release; never replace an existing tag's bytes.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--fail-on-error", action="store_true")
    args = parser.parse_args(argv)
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    report = collect(lock)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    if args.markdown_output:
        args.markdown_output.write_text(markdown(report), encoding="utf-8")
    return 1 if args.fail_on_error and report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
