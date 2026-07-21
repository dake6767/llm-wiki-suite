#!/usr/bin/env python3
"""Deterministic skill installer for named agent hosts.

This is the only owner of target resolution, conflict handling, link/copy
creation, provenance, backups, and the post-install doctor command.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "registry" / "bootstrap.json"
SKILLS_REGISTRY = ROOT / "registry" / "skills.json"
sys.path.insert(0, str(ROOT / "scripts"))
from skill_graph import SkillGraphError, resolve_selection  # noqa: E402
from install_state import (  # noqa: E402
    MANIFEST,
    RUNTIME_NAMES,
    LockUnavailable,
    advisory_lock,
    content_digest,
    is_linklike,
    remove_path,
    verified_copy,
)

sys.path.insert(0, str(ROOT / "skills" / "my-llm-wiki" / "scripts"))
from path_compat import native_path  # noqa: E402


class InstallError(RuntimeError):
    pass


class InstallOperationError(RuntimeError):
    pass


def require_protocol(config: dict) -> None:
    if config.get("version") != 5:
        raise InstallError(f"unsupported bootstrap protocol: {config.get('version')}")
    if not isinstance(config.get("agent_hosts"), dict):
        raise InstallError("bootstrap.json has no agent_hosts map")


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"expected a JSON object: {path}")
    return value


def require_python(config: dict) -> None:
    raw = str((config.get("python") or {}).get("minimum", "3.10"))
    try:
        required = tuple(int(part) for part in raw.split(".")[:2])
    except ValueError as exc:
        raise InstallError(f"invalid python.minimum: {raw}") from exc
    if sys.version_info[:2] < required:
        raise InstallError(
            f"Python {raw}+ is required; current interpreter is "
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )


def expand(path: str) -> Path:
    return native_path(os.path.expandvars(os.path.expanduser(path))).resolve()


def resolve_targets(config: dict, hosts: list[str], custom_targets: list[str]) -> list[dict]:
    host_config = config.get("agent_hosts")
    if not isinstance(host_config, dict):
        raise InstallError("bootstrap.json has no agent_hosts map")
    if not hosts and not custom_targets:
        raise InstallError("select at least one --host or --custom-target")

    unknown = sorted(set(hosts) - set(host_config))
    if unknown:
        raise InstallError(
            "unknown host(s): " + ", ".join(unknown)
            + "; choose from: " + ", ".join(host_config)
        )

    targets: list[dict] = []
    seen: set[Path] = set()
    for host in hosts:
        spec = host_config[host]
        if not isinstance(spec, dict) or not isinstance(spec.get("skills_dir"), str):
            raise InstallError(f"invalid agent host definition: {host}")
        path = expand(spec["skills_dir"])
        if path not in seen:
            targets.append({"host": host, "path": path, "custom": False})
            seen.add(path)
    for raw in custom_targets:
        path = expand(raw)
        if (
            path == Path(path.anchor)
            or path == Path.home().resolve()
            or path == ROOT
            or ROOT in path.parents
        ):
            raise InstallError(f"unsafe custom target: {path}")
        if path not in seen:
            targets.append({"host": "custom", "path": path, "custom": True})
            seen.add(path)
    return targets


def links_to(path: Path, source: Path) -> bool:
    if not is_linklike(path):
        return False
    try:
        return path.resolve(strict=True) == source.resolve(strict=True)
    except OSError:
        return False


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def build_plan(
    config: dict,
    registry: dict,
    hosts: list[str],
    custom_targets: list[str],
    requested: list[str],
    mode: str,
    replace: bool,
    replace_destinations: set[str] | None = None,
) -> dict:
    targets = resolve_targets(config, hosts, custom_targets)
    try:
        selection = resolve_selection(registry, requested)
    except SkillGraphError as exc:
        raise InstallError(str(exc)) from exc

    pack_version = str(registry.get("pack_version", ""))
    sources = {}
    for skill in selection["skills"]:
        slug = skill["slug"]
        source = (ROOT / skill["collection_path"]).resolve()
        if not source.is_dir() or not (source / "SKILL.md").is_file():
            raise InstallError(f"invalid skill source for {slug}: {source}")
        sources[slug] = (source, content_digest(source))

    # Replacement authority belongs to the destination name, not to whatever
    # an existing symlink happens to resolve to.  Normalise without following
    # the final path component so a foreign link cannot redirect consent.
    def replacement_name(raw: str) -> str:
        candidate = Path(
            native_path(
                os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
            )
        )
        return str(candidate.parent.resolve() / candidate.name)

    exact_replacements = {
        replacement_name(raw) for raw in (replace_destinations or set())
    }
    actions = []
    conflicts = []
    for target in targets:
        for skill in selection["skills"]:
            slug = skill["slug"]
            source, source_digest = sources[slug]
            dest = target["path"] / slug
            if mode == "link" and links_to(dest, source):
                state = "current"
            elif mode == "copy" and verified_copy(
                dest, slug, pack_version, source_digest
            ):
                state = "current"
            elif dest.exists() or is_linklike(dest):
                state = (
                    "replace"
                    if replace or str(dest.absolute()) in exact_replacements
                    else "conflict"
                )
            else:
                state = "create"
            item = {
                "host": target["host"],
                "target": str(target["path"]),
                "slug": slug,
                "role": skill["role"],
                "source": str(source),
                "source_digest": source_digest,
                "destination": str(dest),
                "mode": mode,
                "state": state,
            }
            actions.append(item)
            if state == "conflict":
                conflicts.append(item)

    if conflicts:
        details = "\n".join(f"  - {item['destination']}" for item in conflicts)
        raise InstallError(
            "existing paths require explicit --replace; no destination changes were made:\n"
            + details
        )
    return {
        "protocol": config.get("version"),
        "pack_version": pack_version,
        "repo_root": str(ROOT),
        "commit": git_commit(),
        "hosts": hosts,
        "custom_targets": custom_targets,
        "requested": requested,
        "mode": mode,
        "replace": replace,
        "replace_destinations": sorted(exact_replacements),
        "selection": selection,
        "actions": actions,
    }


def make_link(source: Path, dest: Path) -> None:
    if os.name != "nt":
        dest.symlink_to(source, target_is_directory=True)
        return
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not powershell:
        raise InstallError("PowerShell is required to create a Windows junction")
    env = os.environ.copy()
    env["LLM_WIKI_JUNCTION_SOURCE"] = str(source)
    env["LLM_WIKI_JUNCTION_DESTINATION"] = str(dest)
    subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop'; "
            "$s=$env:LLM_WIKI_JUNCTION_SOURCE; $d=$env:LLM_WIKI_JUNCTION_DESTINATION; "
            "New-Item -ItemType Junction -Path $d -Target $s | Out-Null",
        ],
        check=True,
        env=env,
        timeout=30,
    )


def ignore_runtime(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in RUNTIME_NAMES}


def install_copy(source: Path, dest: Path, manifest: dict) -> None:
    temp = dest.parent / f".{dest.name}.install-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, temp, symlinks=True, ignore=ignore_runtime)
        (temp / MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, dest)
    finally:
        if temp.exists():
            shutil.rmtree(temp)


@contextlib.contextmanager
def install_lock(config: dict):
    lock = expand((config.get("install") or {}).get("lock_file", "~/.my-llm-wiki/install.lock"))
    try:
        with advisory_lock(lock):
            yield
    except LockUnavailable as exc:
        raise InstallError(str(exc)) from exc


def apply_plan(config: dict, plan: dict, journal_hook=None) -> None:
    backup_name = (config.get("install") or {}).get(
        "backup_dir_name", ".llm-wiki-backups"
    )
    if (
        not isinstance(backup_name, str)
        or not backup_name
        or Path(backup_name).name != backup_name
        or backup_name in {".", ".."}
    ):
        raise InstallError("invalid install.backup_dir_name")
    changed: list[dict] = []
    created_dirs: list[Path] = []

    def ensure_directory(path: Path) -> None:
        missing = []
        cursor = path
        while not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        for created in reversed(missing):
            if created not in created_dirs:
                created_dirs.append(created)
        path.mkdir(parents=True, exist_ok=True)

    try:
        for item in plan["actions"]:
            if item["state"] == "current":
                continue
            source = Path(item["source"])
            dest = Path(item["destination"])
            ensure_directory(dest.parent)
            if item["state"] == "replace":
                folder = dest.parent / backup_name
                ensure_directory(folder)
                backup = folder / (
                    f"{item['slug']}-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
                )
                item["backup"] = str(backup)
                item["state"] = "activating"
                if journal_hook:
                    journal_hook(plan)
                os.replace(dest, backup)
            else:
                item["state"] = "activating"
                if journal_hook:
                    journal_hook(plan)
            changed.append(item)
            if item["mode"] == "link":
                make_link(source, dest)
            else:
                copy_manifest = {
                    "schema": 1,
                    "slug": item["slug"],
                    "pack_version": plan["pack_version"],
                    "source_digest": item["source_digest"],
                    "source_commit": plan["commit"],
                    "source_repo": plan["repo_root"],
                }
                extra = plan.get("copy_manifest") or {}
                if not isinstance(extra, dict) or any(
                    not isinstance(key, str) for key in extra
                ):
                    raise InstallError("invalid copy_manifest metadata")
                copy_manifest.update(extra)
                install_copy(
                    source,
                    dest,
                    copy_manifest,
                )
            item["state"] = "installed"
            if journal_hook:
                journal_hook(plan)
    except BaseException as exc:
        rollback_errors = []
        for item in reversed(changed):
            dest = Path(item["destination"])
            try:
                remove_path(dest)
                if item.get("backup"):
                    os.replace(item["backup"], dest)
                item["state"] = "rolled-back"
            except Exception as rollback_exc:  # noqa: BLE001 - report every failed restore
                rollback_errors.append(f"{dest}: {rollback_exc}")
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        if rollback_errors:
            raise InstallOperationError(
                f"installation failed ({exc}); rollback incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        raise InstallOperationError(f"installation failed; all changes rolled back: {exc}") from exc


def rollback_applied_plan(plan: dict) -> None:
    """Undo a successfully applied skill plan.

    Protocol 5 treats skill activation and required Wiki verification as one
    core transaction.  ``apply_plan`` already rolls back failures that happen
    while destinations are changing; this companion handles a later failure
    (for example Wiki initialization or core doctor) without guessing which
    paths belong to the run.
    """
    errors: list[str] = []
    touched_parents: set[Path] = set()
    backup_parents: set[Path] = set()
    for item in reversed(plan.get("actions", [])):
        if item.get("state") != "installed":
            continue
        dest = Path(item["destination"])
        touched_parents.add(dest.parent)
        try:
            remove_path(dest)
            backup = item.get("backup")
            if backup:
                backup_parents.add(Path(backup).parent)
                os.replace(backup, dest)
            item["state"] = "rolled-back"
        except Exception as exc:  # noqa: BLE001 - aggregate every restore error
            errors.append(f"{dest}: {exc}")

    # Backups live below the target directory.  Remove only empty installer
    # directories; never recurse over a directory that may contain another
    # operation's recoverable data.
    for backup_dir in backup_parents:
        try:
            backup_dir.rmdir()
        except OSError:
            pass
    for parent in touched_parents:
        try:
            parent.rmdir()
        except OSError:
            pass

    if errors:
        raise InstallOperationError(
            "rollback incomplete: " + "; ".join(errors)
        )


def doctor_argv(args: argparse.Namespace) -> list[str]:
    argv = [sys.executable, str(ROOT / "scripts" / "doctor.py")]
    for host in args.host:
        argv += ["--host", host]
    for target in args.custom_target:
        argv += ["--custom-target", target]
    if args.slugs:
        argv += ["--skills", *args.slugs]
    return argv


def render(plan: dict, args: argparse.Namespace) -> str:
    lines = [
        f"mode: {'copy' if args.copy else 'link'}",
        f"pack: {plan['pack_version']}",
        "actions:",
    ]
    for item in plan["actions"]:
        lines.append(
            f"  - {item['host']} · {item['slug']} [{item['role']}]: "
            f"{item['state']} -> {item['destination']}"
        )
        if item.get("backup"):
            lines.append(f"      backup: {item['backup']}")
    lines.append("doctor: " + shlex.join(doctor_argv(args)))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slugs", nargs="*", help="skills to install; default is every active skill")
    parser.add_argument("--host", action="append", default=[], help="named agent host; repeatable")
    parser.add_argument(
        "--custom-target",
        action="append",
        default=[],
        help="explicit non-registry skills directory; repeatable",
    )
    parser.add_argument("--copy", action="store_true", help="install immutable copies with provenance")
    parser.add_argument("--replace", action="store_true", help="back up and replace every conflict")
    parser.add_argument("--dry-run", action="store_true", help="print a local plan without writing")
    parser.add_argument("--json", action="store_true", help="emit the plan/result as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.host = list(dict.fromkeys(args.host))
    args.custom_target = list(dict.fromkeys(args.custom_target))
    try:
        config = load_json(BOOTSTRAP)
        registry = load_json(SKILLS_REGISTRY)
        require_protocol(config)
        require_python(config)
        def plan_install() -> dict:
            return build_plan(
                config,
                registry,
                args.host,
                args.custom_target,
                args.slugs,
                "copy" if args.copy else "link",
                args.replace,
            )

        if args.dry_run:
            plan = plan_install()
        else:
            # Planning and writes share one lock, so a waiting installer never
            # applies a plan computed against stale destinations.
            with install_lock(config):
                plan = plan_install()
                apply_plan(config, plan)
        plan["status"] = "planned" if args.dry_run else "installed"
    except InstallOperationError as exc:
        print(f"install: {exc}", file=sys.stderr)
        return 1
    except InstallError as exc:
        print(f"install: {exc}", file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"install: operating-system error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print(render(plan, args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
