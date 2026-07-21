#!/usr/bin/env python3
"""Protocol 5 agent-driven, plan-bound installation core.

The command is intentionally non-interactive.  ``inspect`` is read-only;
``plan`` accepts the user's single selection; ``apply`` executes only the
frozen plan and records a durable session result; ``status`` reads it back.

macOS and Linux install immutable receipt-managed release components. Windows
exposes the same command vocabulary from ``My-LLM-Wiki-Setup.exe``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "registry" / "bootstrap.json"
SKILLS_REGISTRY = ROOT / "registry" / "skills.json"
PROTOCOL = 5
SCHEMA = 1
MAX_CAPTURE_CHARS = 8_000

sys.path.insert(0, str(ROOT / "scripts"))
import doctor  # noqa: E402
import initialize_wiki  # noqa: E402
import install  # noqa: E402
import managed_components  # noqa: E402
from skill_graph import SkillGraphError, resolve_selection  # noqa: E402

sys.path.insert(0, str(ROOT / "skills" / "my-llm-wiki" / "scripts"))
import preflight as capture_preflight  # noqa: E402


COMPONENT_TOOLS = {
    "documents": ["markitdown"],
    "web": ["opencli"],
    "video": ["yt-dlp", "ffmpeg"],
    "asr-zh": ["sensevoice"],
    "asr-other": ["faster-whisper"],
}
COMPONENT_LABELS = {
    "documents": "Documents",
    "web": "Web and authenticated social capture",
    "video": "Video base",
    "asr-zh": "Chinese ASR",
    "asr-other": "Non-Chinese ASR",
}
SELECTION_KEYS = {
    "schema",
    "inspection_id",
    "hosts",
    "custom_targets",
    "skills",
    "mode",
    "replace_destinations",
    "components",
    "browser",
    "host_configuration",
    "failure_policy",
}
PLAN_KEYS = {
    "schema",
    "protocol",
    "plan_id",
    "install_id",
    "plan_hash",
    "created_at",
    "inspection_id",
    "platform",
    "source",
    "selection",
    "skills",
    "components",
    "browser",
    "extension",
    "wiki",
    "expected_manual_actions",
}


class ProtocolError(RuntimeError):
    pass


class PlanningError(ProtocolError):
    pass


class ApplyError(ProtocolError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def platform_info() -> dict[str, str]:
    return {
        "os": platform.system().lower(),
        "arch": platform.machine().lower(),
    }


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def plan_hash(plan: dict) -> str:
    unsigned = dict(plan)
    unsigned.pop("plan_hash", None)
    return digest(unsigned)


def read_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError(f"{label} must be a JSON object: {path}")
    return value


def atomic_write_json(path: Path, value: dict) -> None:
    atomic_write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )


def atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_bytes(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install_home(config: dict) -> Path:
    raw = os.environ.get("LLM_WIKI_INSTALL_HOME") or config.get(
        "home", "~/.my-llm-wiki"
    )
    if not isinstance(raw, str) or not raw:
        raise ProtocolError("bootstrap home is missing")
    return install.expand(raw)


def session_root(config: dict) -> Path:
    override = os.environ.get("LLM_WIKI_INSTALL_SESSION_ROOT")
    return (
        Path(override).expanduser().resolve()
        if override
        else install_home(config) / "install-sessions"
    )


def receipt_path(config: dict) -> Path:
    return install_home(config) / "install-state.json"


def read_receipt(config: dict) -> dict | None:
    path = receipt_path(config)
    if not path.is_file():
        return None
    value = read_object(path, "install receipt")
    if value.get("schema") != SCHEMA or value.get("protocol") != PROTOCOL:
        raise ProtocolError(f"unsupported install receipt: {path}")
    declared = value.get("home")
    if declared and Path(declared).expanduser().resolve() != install_home(config):
        raise ProtocolError(f"install receipt home does not match its location: {path}")
    return value


def load_sources() -> tuple[dict, dict]:
    config = install.load_json(BOOTSTRAP)
    registry = install.load_json(SKILLS_REGISTRY)
    if config.get("version") != PROTOCOL:
        raise ProtocolError(f"unsupported bootstrap protocol: {config.get('version')}")
    install.require_python(config)
    return config, registry


def _normalise_exact_path(raw: str) -> str:
    candidate = Path(
        os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
    )
    # Resolve parent aliases such as macOS /var -> /private/var, but never
    # follow the final component: consent names the destination link itself.
    return str(candidate.parent.resolve() / candidate.name)


def build_inspection(
    config: dict,
    registry: dict,
    requested_skills: list[str] | None = None,
) -> dict:
    try:
        selected = resolve_selection(registry, requested_skills)
    except SkillGraphError as exc:
        raise PlanningError(str(exc)) from exc

    hosts_config = config.get("agent_hosts") or {}
    host_ids = list(hosts_config)
    try:
        preview = install.build_plan(
            config,
            registry,
            host_ids,
            [],
            requested_skills or [],
            "copy",
            True,
        )
    except install.InstallError as exc:
        raise PlanningError(str(exc)) from exc

    actions_by_host: dict[str, list[dict]] = {name: [] for name in host_ids}
    for action in preview["actions"]:
        actions_by_host[action["host"]].append(
            {
                "slug": action["slug"],
                "destination": action["destination"],
                "state": action["state"],
            }
        )

    hosts = []
    for host_id, spec in hosts_config.items():
        detect_dir = install.expand(spec["detect_dir"])
        skills_dir = install.expand(spec["skills_dir"])
        actions = actions_by_host[host_id]
        hosts.append(
            {
                "id": host_id,
                "detect_dir": str(detect_dir),
                "skills_dir": str(skills_dir),
                "detected": detect_dir.exists(),
                "selected_by_default": False,
                "actions": actions,
                "conflicts": [
                    action["destination"]
                    for action in actions
                    if action["state"] == "replace"
                ],
                "configuration_offer": (
                    {
                        "id": "hermes_hardening",
                        "settings": {
                            "approvals.mode": "smart",
                            "approvals.cron_mode": "deny",
                            "security.redact_secrets": True,
                            "security.tirith_enabled": True,
                        },
                    }
                    if host_id == "hermes"
                    else None
                ),
            }
        )

    receipt = read_receipt(config)
    components, component_manifest, component_error = managed_components.catalog(
        config, receipt
    )
    # Network routing remains read-only inspection data. Global executable
    # probes are inventory only and never satisfy a managed component.
    toolchain = capture_preflight.build_report(None)
    browser = doctor.check_browser(config)
    browser_offer = doctor.browser_recommendation(browser, config)
    browser_presentation = doctor.browser_offer_presentation(config)
    browser_installable = browser_offer is not None
    extension = doctor.check_opencli_extension()
    inspection = {
        "schema": SCHEMA,
        "protocol": PROTOCOL,
        "inspection_id": "",
        "created_at": now(),
        "platform": platform_info(),
        "source": {
            "repo_root": str(ROOT),
            "bootstrap_version": config.get("version"),
            "pack_version": registry.get("pack_version"),
        },
        "requested_skills": requested_skills or [],
        "resolved_skills": selected,
        "hosts": hosts,
        "components": components,
        "component_manifest": component_manifest,
        "toolchain": toolchain,
        "browser": {
            "status": browser.get("status"),
            "detail": browser.get("detail"),
            "installable": browser_installable,
            "already_satisfied": not browser_installable,
            "optional": True,
            "recommended": browser_presentation["priority"] == "recommended",
            "default_selected": (
                browser_installable and browser_presentation["default_selected"]
            ),
            "presentation": browser_presentation,
            "after_install": {
                "launch": True,
                "open_initialized_wiki": True,
                "automatic_updates": True,
            },
        },
        "opencli_extension": extension,
        "wiki": doctor.check_wiki(config),
        "network": toolchain.get("network", {}),
        "warnings": ([component_error] if component_error else []),
        "blockers": [],
    }
    identity = dict(inspection)
    identity.pop("inspection_id", None)
    inspection["inspection_id"] = digest(identity)
    return inspection


def validate_selection(selection: dict, inspection: dict) -> dict:
    unknown = sorted(set(selection) - SELECTION_KEYS)
    if unknown:
        raise PlanningError("selection contains unknown field(s): " + ", ".join(unknown))
    if selection.get("schema") != SCHEMA:
        raise PlanningError(f"selection schema must be {SCHEMA}")
    if selection.get("inspection_id") != inspection.get("inspection_id"):
        raise PlanningError("selection does not belong to this inspection")

    def string_list(name: str) -> list[str]:
        value = selection.get(name, [])
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise PlanningError(f"selection.{name} must be a list of non-empty strings")
        if len(value) != len(set(value)):
            raise PlanningError(f"selection.{name} contains duplicates")
        return value

    hosts = string_list("hosts")
    custom_targets = string_list("custom_targets")
    skills = string_list("skills")
    replacements = [_normalise_exact_path(path) for path in string_list("replace_destinations")]
    components = string_list("components")
    known_hosts = {row["id"] for row in inspection.get("hosts", [])}
    unknown_hosts = sorted(set(hosts) - known_hosts)
    if unknown_hosts:
        raise PlanningError("unknown host(s): " + ", ".join(unknown_hosts))
    if not hosts and not custom_targets:
        raise PlanningError("select at least one host or custom target")
    unknown_components = sorted(set(components) - set(COMPONENT_TOOLS))
    if unknown_components:
        raise PlanningError("unknown component(s): " + ", ".join(unknown_components))
    if selection.get("mode", "copy") != "copy":
        raise PlanningError("Protocol 5 managed installation requires copy mode")
    browser = selection.get("browser", False)
    if not isinstance(browser, bool):
        raise PlanningError("selection.browser must be boolean")

    host_configuration = selection.get("host_configuration", {})
    if not isinstance(host_configuration, dict):
        raise PlanningError("selection.host_configuration must be an object")
    unknown_config = sorted(set(host_configuration) - {"hermes_hardening"})
    if unknown_config:
        raise PlanningError(
            "unknown host configuration field(s): " + ", ".join(unknown_config)
        )
    hermes_hardening = host_configuration.get("hermes_hardening", False)
    if not isinstance(hermes_hardening, bool):
        raise PlanningError("hermes_hardening must be boolean")
    if hermes_hardening:
        if "hermes" not in hosts:
            raise PlanningError("Hermes hardening requires the hermes host")

    failure_policy = selection.get("failure_policy", {})
    if not isinstance(failure_policy, dict):
        raise PlanningError("selection.failure_policy must be an object")
    unknown_policy = sorted(
        set(failure_policy) - {"optional_components", "browser"}
    )
    if unknown_policy:
        raise PlanningError("unknown failure policy: " + ", ".join(unknown_policy))
    optional_policy = failure_policy.get("optional_components", "continue")
    browser_policy = failure_policy.get("browser", "continue")
    if optional_policy not in {"continue", "stop"}:
        raise PlanningError("optional_components policy must be continue or stop")
    if browser_policy != "continue":
        raise PlanningError("Browser is optional and its policy must be continue")

    return {
        "schema": SCHEMA,
        "inspection_id": inspection["inspection_id"],
        "hosts": hosts,
        "custom_targets": custom_targets,
        "skills": skills,
        "mode": "copy",
        "replace_destinations": sorted(replacements),
        "components": components,
        "browser": browser,
        "host_configuration": {"hermes_hardening": hermes_hardening},
        "failure_policy": {
            "optional_components": optional_policy,
            "browser": "continue",
        },
    }


def build_plan_document(
    config: dict,
    registry: dict,
    inspection: dict,
    raw_selection: dict,
) -> dict:
    selection = validate_selection(raw_selection, inspection)
    requested = selection["skills"]
    try:
        resolved = resolve_selection(registry, requested)
        skill_plan = install.build_plan(
            config,
            registry,
            selection["hosts"],
            selection["custom_targets"],
            requested,
            "copy",
            False,
            set(selection["replace_destinations"]),
        )
    except (SkillGraphError, install.InstallError) as exc:
        raise PlanningError(str(exc)) from exc

    used_replacements = sorted(
        action["destination"]
        for action in skill_plan["actions"]
        if action["state"] == "replace"
    )
    unused_authority = sorted(
        set(selection["replace_destinations"]) - set(used_replacements)
    )
    if unused_authority:
        raise PlanningError(
            "replacement authority does not match a current conflict: "
            + ", ".join(unused_authority)
        )

    manifest = inspection.get("component_manifest")
    if selection["components"] and not isinstance(manifest, dict):
        raise PlanningError("selected managed components are unavailable in this inspection")
    try:
        components = (
            managed_components.freeze_plan(
                manifest, selection["components"], read_receipt(config)
            )
            if selection["components"]
            else {
                "release_tag": (config.get("agent_installer") or {}).get("release_tag"),
                "platform": platform_info()["os"],
                "architecture": managed_components.platform_id()[1],
                "sources": [],
                "runtime": None,
                "items": [],
            }
        )
        components["route"] = (
            ((inspection.get("network") or {}).get("ecosystems") or {}).get("huggingface")
            or "global"
        )
    except managed_components.ComponentError as exc:
        raise PlanningError(str(exc)) from exc
    browser_state = doctor.check_browser(config)
    browser_action: dict[str, Any] = {
        "selected": selection["browser"],
        "state": "not-selected",
    }
    if selection["browser"]:
        recommendation = doctor.browser_recommendation(browser_state, config)
        if recommendation is None:
            browser_action = {"selected": True, "state": "satisfied"}
        else:
            browser_action = {
                "selected": True,
                "state": "install",
                "recipe": recommendation["install"],
            }

    extension_action: dict[str, Any] = {
        "selected": "web" in selection["components"],
        "state": "managed-by-web" if "web" in selection["components"] else "not-selected",
    }
    manual_actions = []
    if "web" in selection["components"]:
        manual_actions.append(
            {
                "id": "opencli-browser-bridge-load",
                "component": "web",
                "severity": "required",
                "state": "pending-after-install",
                "detail": "Load the released Browser Bridge after automated installation.",
            }
        )

    wiki_registry = initialize_wiki.registry_path(config)
    wiki_root = initialize_wiki.default_wiki_root(config)
    plan = {
        "schema": SCHEMA,
        "protocol": PROTOCOL,
        "plan_id": str(uuid.uuid4()),
        "install_id": (read_receipt(config) or {}).get("install_id") or uuid.uuid4().hex,
        "plan_hash": "",
        "created_at": now(),
        "inspection_id": inspection["inspection_id"],
        "platform": platform_info(),
        "source": {
            "repo_root": str(ROOT),
            "bootstrap_version": config.get("version"),
            "pack_version": registry.get("pack_version"),
            "commit": skill_plan.get("commit", ""),
        },
        "selection": selection,
        "skills": {
            "resolved": resolved,
            "actions": skill_plan["actions"],
            "used_replacements": used_replacements,
        },
        "components": components,
        "browser": browser_action,
        "extension": extension_action,
        "wiki": {
            "registry": str(wiki_registry),
            "default_root": str(wiki_root),
        },
        "expected_manual_actions": manual_actions,
    }
    plan["plan_hash"] = plan_hash(plan)
    return plan


def validate_plan(plan: dict) -> None:
    unknown = sorted(set(plan) - PLAN_KEYS)
    if unknown:
        raise ApplyError("plan contains unknown field(s): " + ", ".join(unknown))
    if plan.get("schema") != SCHEMA or plan.get("protocol") != PROTOCOL:
        raise ApplyError("unsupported plan schema or protocol")
    if plan.get("plan_hash") != plan_hash(plan):
        raise ApplyError("plan hash mismatch")
    if plan.get("platform") != platform_info():
        raise ApplyError("plan platform does not match this machine")
    source = plan.get("source") or {}
    if source.get("repo_root") != str(ROOT):
        raise ApplyError("plan belongs to a different checkout")
    if not isinstance(plan.get("selection"), dict):
        raise ApplyError("plan selection is missing")


def _tail(value: str) -> str:
    value = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+",
        r"\1<redacted>",
        value,
    )
    value = re.sub(
        r"(?i)([?&](?:token|access_token|auth_token)=)[^&\s\"']+",
        r"\1<redacted>",
        value,
    )
    return value[-MAX_CAPTURE_CHARS:]


def run_argv(
    argv: list[str],
    *,
    timeout: int,
    env_overlay: dict[str, str] | None = None,
    cwd: Path = ROOT,
) -> dict:
    if not argv or any(not isinstance(arg, str) or not arg for arg in argv):
        raise ApplyError("invalid argv in plan")
    if timeout <= 0:
        raise ApplyError("invalid command timeout in plan")
    env = os.environ.copy()
    for name, value in (env_overlay or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ApplyError("invalid environment overlay in plan")
        env[name] = value
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ApplyError(f"command timed out after {timeout}s: {argv[0]}") from exc
    except OSError as exc:
        raise ApplyError(f"cannot execute {argv[0]}: {exc}") from exc
    report = {
        "argv": argv,
        "returncode": result.returncode,
        "stdout": _tail(result.stdout),
        "stderr": _tail(result.stderr),
    }
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ApplyError(f"command failed: {argv[0]}: {_tail(detail)}")
    return report


def _replace_yaml_section(text: str, section: str, values: dict[str, str]) -> str:
    if "\t" in text:
        raise ApplyError("Hermes config contains tabs and cannot be updated safely")
    lines = text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if re.fullmatch(rf"{re.escape(section)}:\s*", line)),
        None,
    )
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{section}:")
        lines.extend(f"  {key}: {value}" for key, value in values.items())
        return "\n".join(lines).rstrip() + "\n"
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line[0].isspace() and not line.lstrip().startswith("#"):
            end = index
            break
    block = lines[start + 1 : end]
    for key, value in values.items():
        matches = [
            index
            for index, line in enumerate(block)
            if re.fullmatch(rf"  {re.escape(key)}:\s*.*", line)
        ]
        if len(matches) > 1:
            raise ApplyError(f"Hermes config contains duplicate {section}.{key}")
        replacement = f"  {key}: {value}"
        if matches:
            block[matches[0]] = replacement
        else:
            block.append(replacement)
    return "\n".join([*lines[: start + 1], *block, *lines[end:]]).rstrip() + "\n"


def _apply_hermes_hardening(
    config: dict, install_id: str, existing_state: dict | None = None
) -> dict | None:
    spec = (config.get("agent_hosts") or {}).get("hermes") or {}
    detect = spec.get("detect_dir")
    if not isinstance(detect, str):
        raise ApplyError("Hermes host registry entry is invalid")
    path = install.expand(detect) / "config.yaml"
    original = path.read_bytes() if path.is_file() else None
    if (
        isinstance(existing_state, dict)
        and Path(str(existing_state.get("path", ""))) == path
        and original is not None
        and hashlib.sha256(original).hexdigest() == existing_state.get("sha256")
    ):
        return {**existing_state, "changed_in_session": False}
    try:
        text = original.decode("utf-8") if original is not None else ""
    except UnicodeDecodeError as exc:
        raise ApplyError(f"Hermes config is not UTF-8: {path}") from exc
    updated = _replace_yaml_section(
        text, "approvals", {"mode": "smart", "cron_mode": "deny"}
    )
    updated = _replace_yaml_section(
        updated, "security", {"redact_secrets": "true", "tirith_enabled": "true"}
    )
    backup = None
    if original is not None:
        backup_dir = path.parent / ".llm-wiki-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"config.yaml-{install_id}-{uuid.uuid4().hex[:8]}"
        backup.write_bytes(original)
    atomic_write_bytes(path, updated.encode("utf-8"))
    return {
        "id": "hermes_hardening",
        "path": str(path),
        "backup": str(backup) if backup else None,
        "created": original is None,
        "sha256": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
        "changed_in_session": True,
    }


def _rollback_host_configuration(state: dict | None) -> None:
    if not state or not state.get("changed_in_session", True):
        return
    path = Path(state["path"])
    backup = Path(state["backup"]) if state.get("backup") else None
    if backup and backup.is_file():
        os.replace(backup, path)
    elif state.get("created"):
        path.unlink(missing_ok=True)


class Session:
    def __init__(self, config: dict, plan: dict, json_events: bool = False):
        self.session_id = str(uuid.uuid4())
        self.root = session_root(config) / self.session_id
        self.root.mkdir(parents=True, exist_ok=False)
        self.events_path = self.root / "events.jsonl"
        self.journal_path = self.root / "journal.json"
        self.result_path = self.root / "result.json"
        self.sequence = 0
        self.json_events = json_events
        self.journal = {
            "schema": SCHEMA,
            "protocol": PROTOCOL,
            "session_id": self.session_id,
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "state": "applying",
            "phase": "starting",
            "started_at": now(),
            "updated_at": now(),
        }
        atomic_write_json(self.root / "plan.json", plan)
        atomic_write_json(self.journal_path, self.journal)

    def emit(self, event: str, **values: Any) -> None:
        self.sequence += 1
        row = {
            "schema": SCHEMA,
            "protocol": PROTOCOL,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "time": now(),
            "event": event,
            **values,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        if self.json_events:
            print(json.dumps(row, ensure_ascii=False, sort_keys=True), flush=True)

    def phase(self, phase: str) -> None:
        self.journal.update(phase=phase, updated_at=now())
        atomic_write_json(self.journal_path, self.journal)
        self.emit("phase-start", phase=phase)

    def checkpoint_skills(self, skill_plan: dict) -> None:
        atomic_write_json(self.root / "skill-execution.json", skill_plan)

    def finish(self, result: dict) -> None:
        atomic_write_json(self.result_path, result)
        self.journal.update(
            state=result["state"], phase="finished", updated_at=now(), finished_at=now()
        )
        atomic_write_json(self.journal_path, self.journal)
        self.emit("complete", state=result["state"], result=str(self.result_path))


def _recover_interrupted_sessions(config: dict, active_session_id: str) -> list[dict]:
    recovered = []
    root = session_root(config)
    if not root.is_dir():
        return recovered
    receipt = read_receipt(config)
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name == active_session_id:
            continue
        journal_path = directory / "journal.json"
        if not journal_path.is_file() or (directory / "result.json").is_file():
            continue
        journal = read_object(journal_path, "interrupted session journal")
        if journal.get("state") != "applying":
            continue
        session_id = str(journal.get("session_id") or directory.name)
        if receipt and receipt.get("active_session_id") == session_id:
            result = {
                "schema": SCHEMA,
                "protocol": PROTOCOL,
                "session_id": session_id,
                "plan_id": journal.get("plan_id"),
                "plan_hash": journal.get("plan_hash"),
                "state": "complete",
                "started_at": journal.get("started_at"),
                "finished_at": now(),
                "recovered_from_receipt": True,
            }
            atomic_write_json(directory / "result.json", result)
            journal.update(state="complete", phase="finished", updated_at=now())
            atomic_write_json(journal_path, journal)
            recovered.append({"session_id": session_id, "state": "complete"})
            continue

        execution = directory / "skill-execution.json"
        rollback_errors = []
        if execution.is_file():
            skill_plan = read_object(execution, "skill execution journal")
            install_id = str((skill_plan.get("copy_manifest") or {}).get("install_id", ""))
            for row in reversed(skill_plan.get("actions", [])):
                if row.get("state") not in {"activating", "installed"}:
                    continue
                destination = Path(str(row.get("destination", "")))
                backup = Path(str(row["backup"])) if row.get("backup") else None
                try:
                    if destination.exists() or destination.is_symlink():
                        if _owned_skill_copy(destination, install_id):
                            install.remove_path(destination)
                        elif backup and backup.exists():
                            raise ProtocolError(
                                f"destination changed before recovery: {destination}"
                            )
                    if backup and backup.exists():
                        os.replace(backup, destination)
                except Exception as exc:  # noqa: BLE001
                    rollback_errors.append(f"{destination}: {exc}")
        managed_components.cleanup_staging(install_home(config))
        state = "failed" if rollback_errors else "rolled-back"
        result = {
            "schema": SCHEMA,
            "protocol": PROTOCOL,
            "session_id": session_id,
            "plan_id": journal.get("plan_id"),
            "plan_hash": journal.get("plan_hash"),
            "state": state,
            "started_at": journal.get("started_at"),
            "finished_at": now(),
            "error": "previous apply process ended before a terminal result",
            "rollback_errors": rollback_errors,
        }
        atomic_write_json(directory / "result.json", result)
        journal.update(state=state, phase="finished", updated_at=now())
        atomic_write_json(journal_path, journal)
        recovered.append({"session_id": session_id, "state": state})
    return recovered


def _rebuild_skill_plan(config: dict, registry: dict, plan: dict) -> dict:
    selection = plan["selection"]
    try:
        current = install.build_plan(
            config,
            registry,
            selection["hosts"],
            selection["custom_targets"],
            selection["skills"],
            "copy",
            False,
            set(selection["replace_destinations"]),
        )
    except install.InstallError as exc:
        raise ApplyError(str(exc)) from exc

    frozen = [
        {
            "destination": row["destination"],
            "source_digest": row["source_digest"],
            "state": row["state"],
            "slug": row["slug"],
        }
        for row in plan["skills"]["actions"]
    ]
    observed = [
        {
            "destination": row["destination"],
            "source_digest": row["source_digest"],
            "state": row["state"],
            "slug": row["slug"],
        }
        for row in current["actions"]
    ]
    if observed != frozen:
        raise ApplyError("skill destinations changed after planning; no writes made")
    current["copy_manifest"] = {
        "protocol": PROTOCOL,
        "installer": "agent-install-protocol-5",
        "install_id": plan["install_id"],
        "distribution": "managed-pack",
    }
    return current


def _doctor_argv(plan: dict) -> list[str]:
    selection = plan["selection"]
    argv = [sys.executable, str(ROOT / "scripts" / "doctor.py")]
    for host in selection["hosts"]:
        argv += ["--host", host]
    for target in selection["custom_targets"]:
        argv += ["--custom-target", target]
    if selection["skills"]:
        argv += ["--skills", *selection["skills"]]
    argv += ["--json"]
    return argv


def _run_doctor(plan: dict) -> dict:
    argv = _doctor_argv(plan)
    try:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApplyError(f"doctor could not run: {exc}") from exc
    if result.returncode not in {0, 3}:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ApplyError(f"doctor rejected the installation: {_tail(detail)}")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ApplyError("doctor returned invalid JSON") from exc
    return {"returncode": result.returncode, "report": report}


def _run_components(
    session: Session, config: dict, plan: dict
) -> tuple[list[dict], dict | None, dict, dict, dict, bool, list[dict]]:
    try:
        return managed_components.install_selected(
            plan["components"],
            install_home(config),
            route=plan["components"].get("route") or "global",
            emit=session.emit,
            stop_on_failure=(
                plan["selection"]["failure_policy"]["optional_components"] == "stop"
            ),
        )
    except managed_components.ComponentError as exc:
        raise ApplyError(str(exc)) from exc


def _run_browser(session: Session, plan: dict) -> dict:
    action = plan["browser"]
    if action["state"] in {"not-selected", "satisfied"}:
        return {"state": action["state"]}
    recipe = action["recipe"]
    try:
        steps = [
            run_argv(
                argv,
                timeout=int(recipe["step_timeout_seconds"]),
                env_overlay=recipe.get("env") or {},
            )
            for argv in recipe.get("steps", [])
        ]
        postcheck = run_argv(
            recipe["postcheck"],
            timeout=int(recipe["postcheck_timeout_seconds"]),
        )
        session.emit("browser-complete", state="installed")
        return {"state": "installed", "steps": steps, "postcheck": postcheck}
    except ApplyError as exc:
        session.emit("browser-complete", state="failed", error=str(exc))
        return {"state": "failed", "error": str(exc)}


def apply_plan_document(
    config: dict,
    registry: dict,
    plan: dict,
    *,
    json_events: bool = False,
) -> tuple[dict, Path]:
    validate_plan(plan)
    session = Session(config, plan, json_events=json_events)
    skill_plan: dict | None = None
    rollback_error = ""
    receipt_target = receipt_path(config)
    receipt_activated = False
    old_receipt: bytes | None = None
    old_receipt_object: dict = {}
    non_core_mutations_started = False
    host_configuration_state: dict | None = None
    try:
        with install.install_lock(config):
            recovered = _recover_interrupted_sessions(config, session.session_id)
            for row in recovered:
                session.emit("session-recovered", **row)
            old_receipt_object = read_receipt(config) or {}
            old_receipt = receipt_target.read_bytes() if receipt_target.is_file() else None
            try:
                # Revalidation and every mutation share the machine install
                # lock.  A destination cannot change in the gap between the
                # frozen-plan comparison and activation.
                session.phase("revalidating")
                skill_plan = _rebuild_skill_plan(config, registry, plan)
                session.phase("core")
                install.apply_plan(
                    config, skill_plan, journal_hook=session.checkpoint_skills
                )
                wiki = initialize_wiki.ensure_wiki(config)
                if plan["selection"]["host_configuration"].get("hermes_hardening"):
                    host_configuration_state = _apply_hermes_hardening(
                        config,
                        plan["install_id"],
                        (old_receipt_object.get("host_configuration") or {}).get(
                            "hermes_hardening"
                        ),
                    )
                session.emit("core-complete", wiki=str(wiki))

                session.phase("components")
                non_core_mutations_started = any(
                    item.get("state") == "install"
                    for item in plan["components"].get("items", [])
                )
                (
                    component_results,
                    runtime_state,
                    managed_tools,
                    python_profiles,
                    runtime_env,
                    component_failed,
                    manual_actions,
                ) = _run_components(session, config, plan)
                extension_result = {
                    "state": "staged" if manual_actions else plan["extension"]["state"]
                }

                session.phase("browser")
                non_core_mutations_started = (
                    non_core_mutations_started
                    or plan["browser"].get("state") == "install"
                )
                browser_result = _run_browser(session, plan)

                receipt_components = dict(old_receipt_object.get("components") or {})
                for row in component_results:
                    if row.get("state") == "complete":
                        receipt_components[row["id"]] = {
                            key: row[key]
                            for key in (
                                "version",
                                "path",
                                "asset",
                                "sha256",
                                "size",
                                "installed_size",
                            )
                        }
                receipt_tools = dict(old_receipt_object.get("tools") or {})
                receipt_tools.update(managed_tools)
                receipt_profiles = dict(old_receipt_object.get("python_profiles") or {})
                receipt_profiles.update(python_profiles)
                receipt_runtime_env = dict(old_receipt_object.get("runtime_env") or {})
                receipt_runtime_env.update(runtime_env)
                receipt = {
                    "schema": SCHEMA,
                    "protocol": PROTOCOL,
                    "install_id": plan["install_id"],
                    "active_session_id": session.session_id,
                    "activated_at": now(),
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "platform": platform_info()["os"],
                    "architecture": platform_info()["arch"],
                    "home": str(install_home(config)),
                    "suite": str(ROOT),
                    "runtime": runtime_state or old_receipt_object.get("runtime"),
                    "pack_version": plan["source"].get("pack_version"),
                    "hosts": plan["selection"]["hosts"],
                    "custom_targets": plan["selection"]["custom_targets"],
                    "skills": plan["skills"]["resolved"],
                    "skill_actions": plan["skills"]["actions"],
                    "components": receipt_components,
                    "tools": receipt_tools,
                    "python_profiles": receipt_profiles,
                    "runtime_env": receipt_runtime_env,
                    "browser": browser_result,
                    "pending_manual_actions": manual_actions,
                    "host_configuration": (
                        {
                            "hermes_hardening": {
                                key: value
                                for key, value in host_configuration_state.items()
                                if key != "changed_in_session"
                            }
                        }
                        if host_configuration_state
                        else old_receipt_object.get("host_configuration", {})
                    ),
                }
                atomic_write_json(receipt_target, receipt)
                receipt_activated = True

                session.phase("verifying")
                final_doctor = _run_doctor(plan)
                receipt["doctor"] = final_doctor
                atomic_write_json(receipt_target, receipt)
                browser_failed = browser_result.get("state") == "failed"
                if manual_actions or final_doctor["returncode"] == 3:
                    state = "action-required"
                elif component_failed or browser_failed:
                    state = "degraded"
                else:
                    state = "complete"
                result = {
                    "schema": SCHEMA,
                    "protocol": PROTOCOL,
                    "session_id": session.session_id,
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "state": state,
                    "started_at": session.journal["started_at"],
                    "finished_at": now(),
                    "wiki": str(wiki),
                    "components": component_results,
                    "extension": extension_result,
                    "browser": browser_result,
                    "manual_actions": manual_actions,
                    "doctor": final_doctor,
                }
                session.finish(result)
            except Exception:
                if receipt_activated:
                    if old_receipt is None:
                        receipt_target.unlink(missing_ok=True)
                    else:
                        atomic_write_bytes(receipt_target, old_receipt)
                if skill_plan is not None and any(
                    row.get("state") == "installed"
                    for row in skill_plan.get("actions", [])
                ):
                    try:
                        install.rollback_applied_plan(skill_plan)
                    except install.InstallOperationError as rollback_exc:
                        rollback_error = str(rollback_exc)
                try:
                    _rollback_host_configuration(host_configuration_state)
                except OSError as rollback_exc:
                    rollback_error = "; ".join(
                        row for row in (rollback_error, str(rollback_exc)) if row
                    )
                raise
        return result, session.result_path
    except Exception as exc:  # noqa: BLE001 - always persist a terminal result
        state = (
            "failed"
            if rollback_error or non_core_mutations_started
            else "rolled-back"
        )
        result = {
            "schema": SCHEMA,
            "protocol": PROTOCOL,
            "session_id": session.session_id,
            "plan_id": plan.get("plan_id"),
            "plan_hash": plan.get("plan_hash"),
            "state": state,
            "started_at": session.journal["started_at"],
            "finished_at": now(),
            "error": str(exc),
            "rollback_error": rollback_error,
            "non_core_mutations_may_remain": non_core_mutations_started,
        }
        session.finish(result)
        return result, session.result_path


def command_inspect(args: argparse.Namespace) -> int:
    config, registry = load_sources()
    inspection = build_inspection(config, registry, args.skills)
    text = json.dumps(inspection, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        atomic_write_json(args.out, inspection)
    print(text)
    return 0


def command_plan(args: argparse.Namespace) -> int:
    config, registry = load_sources()
    inspection = read_object(args.inspection, "inspection")
    selection = read_object(args.selection, "selection")
    plan = build_plan_document(config, registry, inspection, selection)
    if args.out:
        atomic_write_json(args.out, plan)
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_apply(args: argparse.Namespace) -> int:
    config, registry = load_sources()
    plan = read_object(args.plan, "plan")
    result, result_path = apply_plan_document(
        config, registry, plan, json_events=args.json_events
    )
    if not args.json_events:
        print(json.dumps({**result, "result_path": str(result_path)}, ensure_ascii=False, indent=2))
    if result["state"] == "action-required":
        return 3
    return 0 if result["state"] in {"complete", "degraded"} else 1


def command_status(args: argparse.Namespace) -> int:
    config, _ = load_sources()
    if args.session:
        root = session_root(config) / args.session
        result = root / "result.json"
        journal = root / "journal.json"
        if result.is_file():
            report = read_object(result, "result")
        elif journal.is_file():
            report = read_object(journal, "journal")
        else:
            raise ProtocolError(f"unknown install session: {args.session}")
    else:
        sessions = []
        root = session_root(config)
        if root.is_dir():
            for path in sorted(root.iterdir(), reverse=True):
                source = path / "result.json"
                if not source.is_file():
                    source = path / "journal.json"
                if source.is_file():
                    value = read_object(source, "session state")
                    sessions.append(
                        {
                            "session_id": value.get("session_id", path.name),
                            "state": value.get("state"),
                            "phase": value.get("phase"),
                            "updated_at": value.get("finished_at") or value.get("updated_at"),
                        }
                    )
        report = {
            "schema": SCHEMA,
            "protocol": PROTOCOL,
            "receipt": read_receipt(config),
            "sessions": sessions,
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _receipt_skill_slugs(receipt: dict) -> list[str]:
    return sorted(
        {
            row["slug"]
            for row in receipt.get("skill_actions", [])
            if isinstance(row, dict) and isinstance(row.get("slug"), str)
        }
    )


def _repair_selection(inspection: dict, receipt: dict, override: dict | None) -> dict:
    if override:
        selection = dict(override)
        selection["inspection_id"] = inspection["inspection_id"]
    else:
        selection = {
            "schema": SCHEMA,
            "inspection_id": inspection["inspection_id"],
            "hosts": list(receipt.get("hosts") or []),
            "custom_targets": list(receipt.get("custom_targets") or []),
            "skills": _receipt_skill_slugs(receipt),
            "mode": "copy",
            "replace_destinations": [],
            "components": sorted((receipt.get("components") or {}).keys()),
            "browser": False,
            "host_configuration": {"hermes_hardening": False},
            "failure_policy": {"optional_components": "continue", "browser": "continue"},
        }
    owned = {
        _normalise_exact_path(row["destination"]): row
        for row in receipt.get("skill_actions", [])
        if isinstance(row, dict) and isinstance(row.get("destination"), str)
    }
    authorized = []
    selected_host_set = set(selection.get("hosts") or [])
    selected_target_set = {
        _normalise_exact_path(row) for row in selection.get("custom_targets") or []
    }
    for path, row in sorted(owned.items()):
        if not (
            row.get("host") in selected_host_set
            or _normalise_exact_path(str(row.get("target", ""))) in selected_target_set
        ):
            continue
        destination = Path(path)
        if not (destination.exists() or destination.is_symlink()):
            continue
        if install.verified_copy(
            destination,
            str(row.get("slug")),
            str(receipt.get("pack_version")),
            str(row.get("source_digest")),
        ):
            continue
        marker = Path(path) / ".llm-wiki-install.json"
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if value.get("install_id") == receipt.get("install_id"):
            authorized.append(path)
    selection["replace_destinations"] = authorized
    return selection


def command_repair(args: argparse.Namespace) -> int:
    config, registry = load_sources()
    receipt = read_receipt(config)
    if not receipt:
        raise ProtocolError("no Protocol 5 installation exists to repair")
    override = read_object(args.selection, "repair selection") if args.selection else None
    inspection = build_inspection(config, registry, _receipt_skill_slugs(receipt))
    selection = _repair_selection(inspection, receipt, override)
    plan = build_plan_document(config, registry, inspection, selection)
    result, result_path = apply_plan_document(
        config, registry, plan, json_events=args.json_events
    )
    if not args.json_events:
        print(json.dumps({**result, "result_path": str(result_path)}, ensure_ascii=False, indent=2))
    if result["state"] == "action-required":
        return 3
    return 0 if result["state"] in {"complete", "degraded"} else 1


def _owned_skill_copy(path: Path, install_id: str) -> bool:
    try:
        value = json.loads((path / ".llm-wiki-install.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return value.get("protocol") == PROTOCOL and value.get("install_id") == install_id


def _remove_host_configuration(receipt: dict, remove_all: bool) -> list[dict]:
    results = []
    if not remove_all:
        return results
    state = (receipt.get("host_configuration") or {}).get("hermes_hardening")
    if not isinstance(state, dict):
        return results
    path = Path(str(state.get("path", "")))
    try:
        current = path.read_bytes()
    except OSError:
        current = None
    if current is not None and hashlib.sha256(current).hexdigest() != state.get("sha256"):
        return [{"id": "hermes_hardening", "state": "preserved-modified", "path": str(path)}]
    backup = Path(str(state["backup"])) if state.get("backup") else None
    if backup and backup.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(backup, path)
        results.append({"id": "hermes_hardening", "state": "restored", "path": str(path)})
    elif state.get("created"):
        path.unlink(missing_ok=True)
        results.append({"id": "hermes_hardening", "state": "removed", "path": str(path)})
    return results


def command_uninstall(args: argparse.Namespace) -> int:
    config, _ = load_sources()
    receipt = read_receipt(config)
    if not receipt:
        raise ProtocolError("no Protocol 5 installation exists to uninstall")
    selection = read_object(args.selection, "uninstall selection") if args.selection else {}
    if not args.all and not selection:
        raise ProtocolError("uninstall requires --all or --selection")
    selected_hosts = set(selection.get("hosts") or [])
    selected_targets = {
        _normalise_exact_path(row) for row in selection.get("custom_targets", [])
    }
    selected_components = set(selection.get("components") or [])
    removed_skills = []
    kept_actions = []
    with install.install_lock(config):
        host_configuration = _remove_host_configuration(receipt, args.all)
        for row in receipt.get("skill_actions", []):
            target_selected = (
                args.all
                or row.get("host") in selected_hosts
                or _normalise_exact_path(str(row.get("target", ""))) in selected_targets
            )
            path = Path(str(row.get("destination", "")))
            if target_selected:
                if not _owned_skill_copy(path, str(receipt["install_id"])):
                    raise ProtocolError(f"refusing to remove unverified skill path: {path}")
                shutil.rmtree(path)
                removed_skills.append(str(path))
            else:
                kept_actions.append(row)
        removed_components = managed_components.remove_owned(
            receipt,
            install_home(config),
            None if args.all else selected_components,
        )
        if args.all:
            receipt_path(config).unlink(missing_ok=True)
        else:
            receipt["skill_actions"] = kept_actions
            receipt["hosts"] = [row for row in receipt.get("hosts", []) if row not in selected_hosts]
            receipt["custom_targets"] = [
                row
                for row in receipt.get("custom_targets", [])
                if _normalise_exact_path(row) not in selected_targets
            ]
            for component in selected_components:
                (receipt.get("components") or {}).pop(component, None)
            receipt["tools"] = {
                name: row
                for name, row in (receipt.get("tools") or {}).items()
                if row.get("component") not in selected_components
            }
            for component in selected_components:
                (receipt.get("python_profiles") or {}).pop(component, None)
                (receipt.get("runtime_env") or {}).pop(component, None)
            atomic_write_json(receipt_path(config), receipt)
    report = {
        "schema": SCHEMA,
        "protocol": PROTOCOL,
        "state": "complete",
        "removed_skills": removed_skills,
        "removed_components": removed_components,
        "host_configuration": host_configuration,
        "wiki_preserved": True,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_parser = sub.add_parser("inspect", help="read-only machine inspection")
    inspect_parser.add_argument("--skills", nargs="+", default=None)
    inspect_parser.add_argument("--out", type=Path)
    inspect_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    inspect_parser.set_defaults(func=command_inspect)

    plan_parser = sub.add_parser("plan", help="freeze one user selection")
    plan_parser.add_argument("--inspection", type=Path, required=True)
    plan_parser.add_argument("--selection", type=Path, required=True)
    plan_parser.add_argument("--out", type=Path)
    plan_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    plan_parser.set_defaults(func=command_plan)

    apply_parser = sub.add_parser("apply", help="apply a frozen plan without prompts")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--json-events", action="store_true")
    apply_parser.set_defaults(func=command_apply)

    status_parser = sub.add_parser("status", help="read a session result or journal")
    status_parser.add_argument("--session")
    status_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    status_parser.set_defaults(func=command_status)

    repair_parser = sub.add_parser("repair", help="reapply the receipt-owned selection")
    repair_parser.add_argument("--selection", type=Path)
    repair_parser.add_argument("--json-events", action="store_true")
    repair_parser.set_defaults(func=command_repair)

    uninstall_parser = sub.add_parser("uninstall", help="remove only receipt-owned paths")
    uninstall_parser.add_argument("--selection", type=Path)
    uninstall_parser.add_argument("--all", action="store_true")
    uninstall_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    uninstall_parser.set_defaults(func=command_uninstall)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ProtocolError, install.InstallError, OSError, ValueError) as exc:
        print(f"agent-install: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
