#!/usr/bin/env python3
"""Suite-level health check ("doctor") for llm-wiki-suite.

Answers one question in a single shot: **is the selected suite surface wired
up?** — skill linkage/dependencies, wiki registry, Browser reachability, and the
capture capability profiles contributed by requested/bundled skills. It is the
install/troubleshoot feedback loop, the counterpart to `agent-reach doctor`.

Self-locating: resolves the repo root from this file (following symlinks, so it
works whether run from a dev checkout or through an installed-skill symlink) and
reads registry/bootstrap.json + registry/skills.json as the source of truth.

Output: human summary or JSON. Exit 0 is ready/degraded, 1 is invalid, 2 is a
doctor invocation/configuration error, and 3 means selected capability setup
requires an explicit user action.

Scope note: this is the repo-resident version — it assumes the repo is present
(the install-time / dev-checkout case). Surfacing it to any session regardless of
the repo (packaging it into a distributed meta-skill) is a deliberate later step.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO_ROOT / "registry" / "bootstrap.json"
SKILLS_REGISTRY = REPO_ROOT / "registry" / "skills.json"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from skill_graph import SkillGraphError, resolve_selection  # noqa: E402
from install_state import content_digest, is_linklike, verified_copy  # noqa: E402
import opencli_extension as extension_stager  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "skills" / "my-llm-wiki" / "scripts"))
import preflight as capture_preflight  # noqa: E402
import tool_runtime  # noqa: E402

# install-browser.py owns the MCP registration recipes (build_mcp_commands);
# import it under a module-safe name so doctor reuses the same resolution.
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("install_browser", REPO_ROOT / "scripts" / "install-browser.py")
install_browser = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(install_browser)


def expand(p: str) -> Path:
    # Reuse the installer's expand: it normalizes Git Bash /c/... paths on
    # Windows so a shell-expanded --custom-target is found by native Python.
    return install_browser.expand(p)


def load_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001 - surface any read/parse failure
        return e


def check_repo_home(bootstrap: dict) -> dict:
    """Report where this checkout lives vs the canonical home (informational)."""
    home = bootstrap.get("default_repo_home")
    canonical = expand(home).resolve() if home else None
    at_home = canonical is not None and REPO_ROOT == canonical
    if home is None:
        return {"status": "warn", "detail": "bootstrap.json has no default_repo_home",
                "root": str(REPO_ROOT)}
    return {
        "status": "ok",
        "detail": ("at canonical home" if at_home
                   else f"dev checkout (canonical home is {home})"),
        "root": str(REPO_ROOT),
        "canonical_home": str(canonical),
        "at_canonical_home": at_home,
    }


def check_skills(
    bootstrap: dict,
    skills_registry: dict,
    selected_slugs: set[str] | None = None,
    target_dirs: list[str] | None = None,
    target_scope: str | None = None,
) -> dict:
    """Check active skill linkage plus declared runtime dependencies."""
    all_active = [s for s in skills_registry.get("skills", [])
                  if s.get("lifecycle", "active") == "active"]
    by_slug = {skill["slug"]: skill for skill in all_active}
    digest_cache: dict[str, str] = {}

    def source_for(slug: str) -> Path:
        return (REPO_ROOT / by_slug[slug]["collection_path"]).resolve()

    def source_digest(slug: str) -> str:
        if slug not in digest_cache:
            digest_cache[slug] = content_digest(source_for(slug))
        return digest_cache[slug]

    def target_has_skill(path: Path) -> bool:
        slug = path.name
        if slug not in by_slug:
            return False
        if is_linklike(path):
            try:
                return path.resolve(strict=True) == source_for(slug)
            except OSError:
                return False
        return verified_copy(
            path,
            slug,
            str(skills_registry.get("pack_version", "")),
            source_digest(slug),
        )

    active_slugs = {s["slug"] for s in all_active}
    active = [s for s in all_active
              if selected_slugs is None or s["slug"] in selected_slugs]
    configured_hosts = bootstrap.get("agent_hosts") or {}
    configured_targets = [spec["skills_dir"] for spec in configured_hosts.values()]
    configured_paths = [expand(t) for t in configured_targets]
    if target_dirs is not None:
        targets = [expand(t) for t in target_dirs]
        existing_targets = targets
        scope = target_scope or "explicit"
    else:
        # Do not turn unrelated pre-existing agent directories into a verdict
        # about this checkout. Prefer hosts that already link to this repo;
        # inspect every configured target only for fresh-install diagnostics.
        candidates = [t for t in configured_paths if t.is_dir()]
        linked_targets = []
        for t in candidates:
            for s in active:
                dest = t / s["slug"]
                if not is_linklike(dest):
                    continue
                try:
                    if dest.resolve(strict=True) == (REPO_ROOT / s["collection_path"]).resolve():
                        linked_targets.append(t)
                        break
                except OSError:
                    continue
        existing_targets = linked_targets or candidates
        scope = "auto-linked" if linked_targets else "auto-all-existing"

    skills_out = []
    worst = "ok"
    for s in active:
        slug = s["slug"]
        requires = s.get("requires", [])
        bundles = s.get("bundles", [])
        unknown_refs = sorted((set(requires) | set(bundles)) - active_slugs)
        src = source_for(slug)
        states = {}  # target -> state
        dependency_missing = {}
        for t in existing_targets:
            dest = t / slug
            if is_linklike(dest):
                try:
                    resolved = dest.resolve(strict=True)
                except OSError:
                    states[str(t)] = "broken-link"
                    continue
                states[str(t)] = "linked" if resolved == src else "foreign-link"
            elif dest.exists():
                states[str(t)] = (
                    "copy-current"
                    if verified_copy(
                        dest,
                        slug,
                        str(skills_registry.get("pack_version", "")),
                        source_digest(slug),
                    )
                    else "invalid-copy"
                )
            else:
                states[str(t)] = "missing"

            if states[str(t)] != "missing" and requires:
                missing = [dep for dep in requires
                           if not target_has_skill(t / dep)]
                if missing:
                    dependency_missing[str(t)] = missing

        vals = list(states.values())
        if unknown_refs:
            status = "error"
            note = "registry references unknown/inactive skill(s): " + ", ".join(unknown_refs)
        elif dependency_missing:
            status = "error"
            missing_names = sorted({d for deps in dependency_missing.values() for d in deps})
            note = "installed without required skill(s): " + ", ".join(missing_names)
        elif not vals:
            status = "warn"  # no agent dirs at all on this machine
            note = "no agent skill dirs present"
        elif all(v == "missing" for v in vals):
            status = "error"
            note = "not installed in any agent dir"
        elif any(v in ("foreign-link", "broken-link", "invalid-copy") for v in vals):
            status = "error"
            note = "target provenance is invalid, foreign, or broken"
        elif any(v == "missing" for v in vals):
            status = "error" if target_dirs is not None else "warn"
            note = "missing from one or more selected agent dirs"
        elif any(v == "copy-current" for v in vals):
            status = "ok"
            note = "installed with verified link/copy provenance"
        else:
            status = "ok"
            note = "linked into repo"
        worst = _worse(worst, status)
        skills_out.append({"slug": slug, "status": status, "note": note,
                           "requires": requires, "bundles": bundles,
                           "dependency_missing": dependency_missing,
                           "targets": states})

    return {"status": worst, "skills": skills_out,
            "existing_targets": [str(t) for t in existing_targets],
            "target_scope": scope}


def check_wiki(bootstrap: dict) -> dict:
    reg_path = os.environ.get("LLM_WIKI_REGISTRY") or bootstrap.get("wiki_registry_path")
    if not reg_path:
        return {"status": "warn", "detail": "bootstrap.json has no wiki_registry_path"}
    path = expand(reg_path)
    if not path.exists():
        return {"status": "warn",
                "detail": f"no wiki registry yet ({reg_path}) — init a first wiki"}
    data = load_json(path)
    if isinstance(data, Exception):
        return {"status": "warn", "detail": f"registry unreadable: {data}"}
    wikis = data.get("wikis", data) if isinstance(data, dict) else data
    if not isinstance(wikis, list):
        return {"status": "warn", "detail": "registry present but unrecognized shape"}
    default = next((w.get("name") for w in wikis
                    if isinstance(w, dict) and w.get("default")), None)
    if not wikis:
        return {"status": "warn", "detail": "registry present but empty"}
    return {"status": "ok", "count": len(wikis), "default": default,
            "detail": f"{len(wikis)} wiki(s) registered"
                      + (f", default: {default}" if default else ", no default set")}


def resolve_browser_port(fr: dict) -> tuple[int, str]:
    """Mirror the app's own resolution: pref file > $LLM_WIKI_PORT > default."""
    default = fr.get("default_port", 8800)
    pref = fr.get("port_pref_file")
    if pref:
        try:
            v = int(expand(pref).read_text().strip())
            if v >= 1024:
                return v, f"persisted ({pref})"
        except Exception:  # noqa: BLE001 - absent/unparseable → fall through
            pass
    env_name = fr.get("port_env", "LLM_WIKI_PORT")
    ev = os.environ.get(env_name, "").strip()
    if ev.isdigit() and int(ev) >= 1024:
        return int(ev), f"${env_name}"
    return default, "default"


def check_browser(bootstrap: dict) -> dict:
    install_state = install_browser.browser_install_state(bootstrap)
    fr = bootstrap.get("first_run") or {}
    health_path = fr.get("health_path")
    if not health_path:
        return {"status": "skip", "install": install_state,
                "detail": "no health_path in bootstrap.json"}
    host = fr.get("host", "127.0.0.1")
    port, port_src = resolve_browser_port(fr)
    url = f"http://{host}:{port}{health_path}"
    # Liveness only: a successful health response or auth-gated 401/403 proves
    # the server is up. We deliberately do not read the API token here.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=2) as resp:  # noqa: S310 - fixed localhost
            try:
                body = json.load(resp)
            except Exception:  # noqa: BLE001 - non-JSON body still means "up"
                body = None
        healthy = isinstance(body, dict) and body.get("ok") is True
        return {"status": "ok", "port": port, "install": install_state,
                "detail": f"{'healthy' if healthy else 'up'} at {url} (port from {port_src})"}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"status": "ok", "port": port, "install": install_state,
                    "detail": f"up at {url} — responding (HTTP {e.code}, auth-gated); port from {port_src}"}
        return {"status": "warn", "port": port, "install": install_state,
                "detail": f"unexpected HTTP {e.code} at {url}; Browser health is unknown"}
    except Exception:  # noqa: BLE001 - connection refused/timeout = not running (optional)
        installed = "installed but not running" if install_state["ok"] else "not installed"
        return {"status": "warn", "port": port, "install": install_state,
                "detail": f"not reachable at {url} (port from {port_src}) — Browser {installed}"}


def check_mcp(bootstrap: dict, browser: dict, hosts: list[str]) -> dict:
    """Browser-MCP registration state per host.

    - Browser installed but a host with a known register command has no entry →
      warn with the one-line fix.
    - Host has an entry but the Browser is gone → warn about the stale entry
      (a broken MCP config pollutes the host's startup).
    - Browser absent and nothing registered → skip: the Browser is an optional
      component, skills use the CLI fallback — not a defect.
    """
    mcp = bootstrap.get("mcp")
    if not mcp:
        return {"status": "skip", "detail": "no mcp block in bootstrap.json"}
    install_state = browser.get("install") or install_browser.browser_install_state(bootstrap)
    browser_present = bool(install_state.get("ok"))
    rows = install_browser.build_mcp_commands(bootstrap, hosts=hosts)
    hosts_out = []
    warns = []
    bridge_probe = None
    if browser_present and browser.get("status") == "ok":
        bridge_rel = mcp.get("stdio_bridge_script", "scripts/mcp-stdio-bridge.py")
        bridge_path = (REPO_ROOT / bridge_rel).resolve()
        if not bridge_path.is_file():
            warns.append(f"stdio bridge missing: {bridge_path}")
            bridge_probe = {"ok": False, "error": "bridge script missing"}
        else:
            try:
                result = subprocess.run(
                    [sys.executable, str(bridge_path), "--probe", "--json", "--timeout", "3"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    bridge_probe = json.loads(result.stdout)
                else:
                    error = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
                    bridge_probe = {"ok": False, "error": error}
                    warns.append(f"stdio bridge probe failed: {error}")
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                bridge_probe = {"ok": False, "error": str(exc)}
                warns.append(f"stdio bridge probe failed: {exc}")
    for row in rows:
        host = {
            "host": row["host"],
            "registered": row["registered"],
            "transport": row.get("transport"),
        }
        if row["registered"]:
            if not browser_present:
                host["issue"] = "stale-entry"
                warns.append(
                    f"{row['host']}: MCP entry present but Browser not installed — "
                    f"remove it: {row['unregister_command'] or 'see host config'}"
                )
            elif row.get("transport") != "stdio":
                host["issue"] = "conflicting-registration"
                remove = row["unregister_command"] or "remove it from the host config"
                warns.append(
                    f"{row['host']}: conflicting MCP registration "
                    f"({row.get('transport') or 'unknown'}); run `{remove}`, then "
                    f"`python3 scripts/install-browser.py --register-mcp --host {row['host']}`"
                )
        elif (row["command"] or row.get("manual_config")) and browser_present:
            host["issue"] = "unregistered"
            warns.append(
                f"{row['host']}: Browser installed but MCP not registered — "
                f"run: python3 scripts/install-browser.py --register-mcp --host {row['host']}"
            )
        hosts_out.append(host)
    if not rows:
        return {"status": "skip", "detail": "no MCP-capable host selected"}
    if not browser_present and not any(r["registered"] for r in rows):
        return {"status": "skip", "browser_present": False, "hosts": hosts_out,
                "install": install_state,
                "detail": "Browser not installed (optional) — skills use the CLI fallback"}
    status = "warn" if warns else "ok"
    detail = (
        "; ".join(warns)
        if warns
        else "registered: "
        + (", ".join(r["host"] for r in rows if r["registered"]) or "none needed")
    )
    return {"status": status, "browser_present": browser_present,
            "install": install_state,
            "bridge_probe": bridge_probe, "hosts": hosts_out,
            "warnings": warns, "detail": detail}


def check_toolchain(bootstrap: dict, selection: dict) -> dict:
    profiles = selection.get("profiles", [])
    base = {
        "selected_skills": [s["slug"] for s in selection.get("skills", [])],
        "feature_skills": selection.get("feature_skills", []),
        "profiles": profiles,
    }
    if not profiles:
        return {"status": "skip", "detail": "selected skills declare no capture profiles", **base}

    config = bootstrap.get("capture_toolchain") or {}
    catalog_rel = config.get("catalog")
    if not catalog_rel:
        return {"status": "error", "detail": "bootstrap has no capture toolchain catalog", **base}
    catalog_path = REPO_ROOT / catalog_rel
    try:
        report = capture_preflight.build_report(profiles, catalog_path)
    except ValueError as exc:
        return {"status": "error", "detail": str(exc), **base}
    report.update(base)
    report["catalog"] = str(catalog_path)
    return report


def _opencli_installed() -> bool:
    """Whether the opencli CLI is invocable on this machine.

    Goes through the shared tool-runtime resolution instead of a bare
    ``shutil.which``: on Windows the CLI is a Setup-managed component
    (``node.exe`` + bundled ``main.js``) that is deliberately never on PATH,
    so a PATH probe would report "not installed" even right after a
    successful Setup run. On macOS/Linux the resolution is exactly
    ``shutil.which``, so behaviour there is unchanged.
    """
    try:
        tool_runtime.resolve_command_argv("opencli")
    except tool_runtime.ToolRuntimeError:
        return False
    return True


def check_opencli_extension(dest: Path | None = None) -> dict:
    """Browser Bridge staging state for opencli.

    Staging presence is all this can see: a Chrome Web Store install leaves no
    staged folder, and only `opencli doctor` can confirm the live bridge — so
    a missing staging is a warn (an unmet offer), never action-required.
    """
    if not _opencli_installed():
        return {
            "status": "skip",
            "detail": "opencli not installed; Browser Bridge extension not applicable",
        }
    staged = extension_stager.status_report(
        dest if dest is not None else Path(extension_stager.DEFAULT_DEST).expanduser()
    )
    if staged["status"] == "staged":
        # The live-bridge verifier is the opencli CLI's own doctor; on Windows
        # that CLI is only reachable through the Setup runner, never PATH.
        verify = (
            "My-LLM-Wiki-Setup.exe tools run opencli -- doctor"
            if tool_runtime.is_windows()
            else "`opencli doctor`"
        )
        return {
            "status": "ok",
            "detail": (
                f"Browser Bridge staged v{staged['version']};"
                f" {verify} verifies the live bridge"
            ),
            "version": staged["version"],
            "path": staged["path"],
        }
    if tool_runtime.is_windows():
        detail = (
            "opencli is installed (Windows Setup managed) but its Browser Bridge"
            " extension is not staged; re-run My-LLM-Wiki-Setup.exe to repair the"
            " web component, then load the staged folder via chrome://extensions"
        )
    else:
        detail = (
            "opencli is installed but its Browser Bridge extension is not staged;"
            " re-present the required AGENTS.md §6 extension sequence: stage via"
            " `python3 scripts/opencli_extension.py`, relay its manual"
            " chrome://extensions load steps, ask whether loading is done"
            " (skippable), then verify with `opencli doctor`"
        )
    return {
        "status": "warn",
        "detail": detail,
    }


_ORDER = {"ok": 0, "skip": 0, "warn": 1, "action-required": 1, "error": 2}


def _worse(a: str, b: str) -> str:
    return a if _ORDER[a] >= _ORDER[b] else b


def build_report(
    requested_skills: list[str] | None = None,
    hosts: list[str] | None = None,
    custom_targets: list[str] | None = None,
    all_hosts: bool = False,
    check_mcp_state: bool = False,
) -> dict:
    bootstrap = load_json(BOOTSTRAP)
    skills_registry = load_json(SKILLS_REGISTRY)
    if isinstance(bootstrap, Exception):
        return {"fatal": f"cannot read {BOOTSTRAP}: {bootstrap}"}
    if isinstance(skills_registry, Exception):
        return {"fatal": f"cannot read {SKILLS_REGISTRY}: {skills_registry}"}
    if bootstrap.get("version") != 4:
        return {"fatal": f"unsupported bootstrap protocol: {bootstrap.get('version')}"}

    try:
        selection = resolve_selection(skills_registry, requested_skills)
    except SkillGraphError as exc:
        return {"fatal": f"skill graph invalid: {exc}"}
    selected_slugs = {s["slug"] for s in selection["skills"]}
    host_specs = bootstrap.get("agent_hosts") or {}
    selected_hosts = list(host_specs) if all_hosts else list(dict.fromkeys(hosts or []))
    unknown_hosts = sorted(set(selected_hosts) - set(host_specs))
    if unknown_hosts:
        return {"fatal": "unknown host(s): " + ", ".join(unknown_hosts)}
    target_dirs = [host_specs[name]["skills_dir"] for name in selected_hosts]
    target_dirs.extend(dict.fromkeys(custom_targets or []))
    if not target_dirs:
        return {"fatal": "select at least one --host or --custom-target"}
    target_scope = "all-hosts" if all_hosts else "explicit"

    try:
        browser = check_browser(bootstrap)
        components = {
            "repo_home": check_repo_home(bootstrap),
            "skills": check_skills(
                bootstrap, skills_registry, selected_slugs, target_dirs=target_dirs,
                target_scope=target_scope,
            ),
            "wiki_registry": check_wiki(bootstrap),
            "browser": browser,
        }
        if check_mcp_state:
            components["mcp"] = check_mcp(bootstrap, browser, selected_hosts)
        components["toolchain"] = check_toolchain(bootstrap, selection)
        components["opencli_extension"] = check_opencli_extension()
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"fatal": f"doctor check failed: {exc}"}
    overall = "ok"
    for c in components.values():
        overall = _worse(overall, c.get("status", "ok"))
    action_required = any(
        item.get("status") == "unavailable"
        for item in components["toolchain"].get("capabilities", {}).values()
    )
    state = (
        "failed" if overall == "error"
        else "action-required" if action_required or overall == "action-required"
        else "degraded" if overall == "warn"
        else "ready"
    )
    return {
        "state": state,
        "overall": overall,
        "repo_root": str(REPO_ROOT),
        "selected_hosts": selected_hosts,
        "components": components,
    }


_ICON = {"ok": "✓", "warn": "!", "action-required": "!", "error": "✗", "skip": "·"}


def render_human(report: dict) -> str:
    if "fatal" in report:
        return f"✗ doctor cannot run: {report['fatal']}"
    lines = []
    c = report["components"]

    rh = c["repo_home"]
    lines.append(f"{_ICON[rh['status']]} repo         {rh['detail']}")
    lines.append(f"                {rh['root']}")

    sk = c["skills"]
    lines.append(f"{_ICON[sk['status']]} skills       "
                 f"{len(sk['skills'])} selected · scope: {sk.get('target_scope', 'unknown')} · targets: "
                 f"{', '.join(os.path.basename(os.path.dirname(t)) for t in sk['existing_targets']) or 'none'}")
    for s in sk["skills"]:
        lines.append(f"     {_ICON[s['status']]} {s['slug']}: {s['note']}")

    wk = c["wiki_registry"]
    lines.append(f"{_ICON[wk['status']]} wiki         {wk['detail']}")

    br = c["browser"]
    lines.append(f"{_ICON[br['status']]} browser      {br['detail']}")

    if "mcp" in c:
        mc = c["mcp"]
        lines.append(f"{_ICON[mc['status']]} mcp          {mc['detail']}")

    tc = c["toolchain"]
    if tc["status"] == "skip":
        lines.append(f"{_ICON['skip']} toolchain    {tc['detail']}")
    elif tc["status"] == "error":
        lines.append(f"{_ICON['error']} toolchain    {tc['detail']}")
    else:
        profiles = ", ".join(tc.get("profiles", [])) or "none"
        lines.append(f"{_ICON[tc['status']]} toolchain    profiles: {profiles}")
        # Full tool inventory, present or not: consent lists are transcribed
        # from this report, and a tool that never appears cannot be missed
        # silently (a live agent run dropped required-but-missing markitdown).
        if tc.get("tools"):
            inventory = " · ".join(
                f"{name} {'✓' if info.get('status') == 'ok' else '✗ missing'}"
                for name, info in tc["tools"].items()
            )
            lines.append(f"     tools: {inventory}")
        for profile, info in tc.get("capabilities", {}).items():
            cap_icon = _ICON["ok"] if info.get("status") == "ok" else _ICON["warn"]
            detail = info.get("via") or info.get("note") or ""
            lines.append(f"     {cap_icon} {profile}: {info.get('status')}"
                         + (f" via {detail}" if detail else ""))
            if info.get("asr"):
                lines.append(f"        asr routing: {info['asr']}")
        for rec in tc.get("recommendations", []):
            reasons = "; ".join(rec.get("reasons", []))
            lines.append(f"     → {rec['tool']} [{rec['priority']}]: {reasons}")
            if rec.get("install"):
                recipe = rec["install"]
                lines.append(f"       route: {recipe.get('route', 'unavailable')}")
                if recipe.get("reason"):
                    lines.append(f"       reason: {recipe['reason']}")
                if recipe.get("env"):
                    lines.append(
                        "       env: " + json.dumps(recipe["env"], ensure_ascii=False)
                    )
                if recipe.get("runtime_env"):
                    lines.append(
                        "       runtime env: "
                        + json.dumps(recipe["runtime_env"], ensure_ascii=False)
                    )
                if recipe.get("unavailable_runtime"):
                    lines.append(
                        "       unavailable runtime ecosystems: "
                        + ", ".join(recipe["unavailable_runtime"])
                    )
                for step in recipe.get("steps", []):
                    lines.append(
                        "       argv: " + json.dumps(step, ensure_ascii=False)
                    )
                lines.append(
                    f"       step timeout: {recipe['step_timeout_seconds']}s"
                )
                if recipe.get("postcheck"):
                    lines.append(
                        "       postcheck: "
                        + json.dumps(recipe["postcheck"], ensure_ascii=False)
                    )
                    lines.append(
                        "       postcheck timeout: "
                        f"{recipe['postcheck_timeout_seconds']}s"
                    )
                if rec.get("home"):
                    lines.append(f"       home: {rec['home']}")

    oe = c.get("opencli_extension")
    if oe:
        lines.append(f"{_ICON[oe['status']]} opencli-ext  {oe['detail']}")
        if oe.get("path"):
            lines.append(f"                {oe['path']}")

    lines.append("")
    verdict = {
        "ready": "ready",
        "degraded": "installed with optional degradation",
        "action-required": "selected capability needs user action",
        "failed": "installation is invalid",
    }[report["state"]]
    lines.append(f"{_ICON[report['overall']]} state: {report['state']} — {verdict}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Suite-level health check for llm-wiki-suite.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--skills",
        nargs="+",
        metavar="SLUG",
        help="scope linkage and toolchain checks to requested skills plus graph closure",
    )
    ap.add_argument("--host", action="append", default=[], help="named agent host; repeatable")
    ap.add_argument(
        "--custom-target", action="append", default=[],
        help="explicit non-registry skills directory; repeatable",
    )
    ap.add_argument("--all-hosts", action="store_true", help="inspect every configured host")
    ap.add_argument(
        "--check-mcp",
        action="store_true",
        help="inspect MCP registration as an explicit post-install diagnostic",
    )
    args = ap.parse_args()

    if args.host and args.all_hosts:
        ap.error("--host and --all-hosts cannot be used together")
    report = build_report(
        args.skills,
        args.host,
        args.custom_target,
        args.all_hosts,
        check_mcp_state=args.check_mcp,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_human(report))

    if "fatal" in report:
        return 2
    if report["state"] == "failed":
        return 1
    if report["state"] == "action-required":
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
