#!/usr/bin/env python3
"""Suite-level health check ("doctor") for llm-wiki-suite.

Answers one question in a single shot: **is the whole suite wired up?** — skills
linked into the agent skill dirs, the wiki registry present, My LLM Wiki Browser
reachable, and the capture adapters available. It is the install/troubleshoot
feedback loop, the counterpart to `agent-reach doctor`.

Self-locating: resolves the repo root from this file (following symlinks, so it
works whether run from a dev checkout or through an installed-skill symlink) and
reads registry/bootstrap.json + registry/skills.json as the source of truth.

Output: a human summary by default, or machine-readable JSON with --json
(mirrors `agent-reach doctor --json`). Each component reports one of:
  ok    — good to go
  warn  — works with a caveat / optional piece missing
  error — a critical piece is broken (drives a non-zero exit)
  skip  — not applicable on this machine

Exit code: 0 unless a component is in "error" (today: skills linkage — the one
thing install must get right). Everything else degrades to warn/skip.

Scope note: this is the repo-resident version — it assumes the repo is present
(the install-time / dev-checkout case). Surfacing it to any session regardless of
the repo (packaging it into a distributed meta-skill) is a deliberate later step.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO_ROOT / "registry" / "bootstrap.json"
SKILLS_REGISTRY = REPO_ROOT / "registry" / "skills.json"

# Capture adapters my-llm-wiki's scenario SOPs can use. All optional — every
# scenario has recipes per available tool (see references/sources.md), so
# absence is warn, never error.
ADAPTERS = [
    ("opencli", "default web/social fetch adapter"),
    ("yt-dlp", "video download fallback (no-caption path)"),
    ("markitdown", "local document (PDF/docx) conversion"),
]


def expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


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


def check_skills(bootstrap: dict, skills_registry: dict) -> dict:
    """Per active skill: is it linked into this repo across the existing targets?"""
    active = [s for s in skills_registry.get("skills", [])
              if s.get("lifecycle", "active") == "active"]
    targets = [expand(t) for t in bootstrap.get("default_skill_targets", [])]
    existing_targets = [t for t in targets if t.is_dir()]

    skills_out = []
    worst = "ok"
    for s in active:
        slug = s["slug"]
        src = (REPO_ROOT / s["collection_path"]).resolve()
        states = {}  # target -> state
        for t in existing_targets:
            dest = t / slug
            if dest.is_symlink():
                try:
                    resolved = dest.resolve()
                except OSError:
                    states[str(t)] = "broken-link"
                    continue
                states[str(t)] = "linked" if resolved == src else "foreign-link"
            elif dest.exists():
                states[str(t)] = "copy"
            else:
                states[str(t)] = "missing"

        vals = list(states.values())
        if not vals:
            status = "warn"  # no agent dirs at all on this machine
            note = "no agent skill dirs present"
        elif all(v == "missing" for v in vals):
            status = "error"
            note = "not installed in any agent dir"
        elif any(v in ("foreign-link", "broken-link") for v in vals):
            status = "warn"
            note = "a target points outside this repo (or is broken)"
        elif any(v == "copy" for v in vals):
            status = "warn"
            note = "installed as a copy (--copy): won't track repo edits"
        elif any(v == "missing" for v in vals):
            status = "warn"
            note = "linked in some agent dirs but missing in others"
        else:
            status = "ok"
            note = "linked into repo"
        worst = _worse(worst, status)
        skills_out.append({"slug": slug, "status": status, "note": note, "targets": states})

    return {"status": worst, "skills": skills_out,
            "existing_targets": [str(t) for t in existing_targets]}


def check_wiki(bootstrap: dict) -> dict:
    reg_path = bootstrap.get("wiki_registry_path")
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
    """Mirror the app's own resolution (main.rs): pref file > $PORT > default."""
    default = fr.get("default_port", 8800)
    pref = fr.get("port_pref_file")
    if pref:
        try:
            v = int(expand(pref).read_text().strip())
            if v >= 1024:
                return v, f"persisted ({pref})"
        except Exception:  # noqa: BLE001 - absent/unparseable → fall through
            pass
    env_name = fr.get("port_env", "PORT")
    ev = os.environ.get(env_name, "").strip()
    if ev.isdigit() and int(ev) >= 1024:
        return int(ev), f"${env_name}"
    return default, "default"


def check_browser(bootstrap: dict) -> dict:
    fr = bootstrap.get("first_run") or {}
    health_path = fr.get("health_path")
    if not health_path:
        return {"status": "skip", "detail": "no health_path in bootstrap.json"}
    host = fr.get("host", "127.0.0.1")
    port, port_src = resolve_browser_port(fr)
    url = f"http://{host}:{port}{health_path}"
    # Liveness only — any HTTP response (even an auth-gated 401) proves the server
    # is up. We deliberately do NOT read the API token; reachability != auth.
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 - localhost
            try:
                body = json.load(resp)
            except Exception:  # noqa: BLE001 - non-JSON body still means "up"
                body = None
        healthy = isinstance(body, dict) and body.get("ok") is True
        return {"status": "ok", "port": port,
                "detail": f"{'healthy' if healthy else 'up'} at {url} (port from {port_src})"}
    except urllib.error.HTTPError as e:
        return {"status": "ok", "port": port,
                "detail": f"up at {url} — responding (HTTP {e.code}, auth-gated); port from {port_src}"}
    except Exception:  # noqa: BLE001 - connection refused/timeout = not running (optional)
        return {"status": "warn", "port": port,
                "detail": f"not reachable at {url} (port from {port_src}) — Browser optional / not running"}


def check_adapters() -> dict:
    out = []
    for name, desc in ADAPTERS:
        found = shutil.which(name)
        out.append({"name": name, "status": "ok" if found else "warn",
                    "detail": (found or f"not found — {desc} (optional; skill degrades)")})
    # Adapters never gate the exit code; report the best of them for the summary line.
    status = "ok" if any(a["status"] == "ok" for a in out) else "warn"
    return {"status": status, "adapters": out}


_ORDER = {"ok": 0, "skip": 0, "warn": 1, "error": 2}


def _worse(a: str, b: str) -> str:
    return a if _ORDER[a] >= _ORDER[b] else b


def build_report() -> dict:
    bootstrap = load_json(BOOTSTRAP)
    skills_registry = load_json(SKILLS_REGISTRY)
    if isinstance(bootstrap, Exception):
        return {"fatal": f"cannot read {BOOTSTRAP}: {bootstrap}"}
    if isinstance(skills_registry, Exception):
        return {"fatal": f"cannot read {SKILLS_REGISTRY}: {skills_registry}"}

    components = {
        "repo_home": check_repo_home(bootstrap),
        "skills": check_skills(bootstrap, skills_registry),
        "wiki_registry": check_wiki(bootstrap),
        "browser": check_browser(bootstrap),
        "adapters": check_adapters(),
    }
    overall = "ok"
    for c in components.values():
        overall = _worse(overall, c.get("status", "ok"))
    return {"overall": overall, "repo_root": str(REPO_ROOT), "components": components}


_ICON = {"ok": "✓", "warn": "!", "error": "✗", "skip": "·"}


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
                 f"{len(sk['skills'])} active · targets: "
                 f"{', '.join(os.path.basename(os.path.dirname(t)) for t in sk['existing_targets']) or 'none'}")
    for s in sk["skills"]:
        lines.append(f"     {_ICON[s['status']]} {s['slug']}: {s['note']}")

    wk = c["wiki_registry"]
    lines.append(f"{_ICON[wk['status']]} wiki         {wk['detail']}")

    br = c["browser"]
    lines.append(f"{_ICON[br['status']]} browser      {br['detail']}")

    ad = c["adapters"]
    lines.append(f"{_ICON[ad['status']]} adapters     "
                 + " · ".join(f"{_ICON[a['status']]}{a['name']}" for a in ad["adapters"]))
    for a in ad["adapters"]:
        if a["status"] != "ok":
            lines.append(f"     {_ICON[a['status']]} {a['name']}: {a['detail']}")

    lines.append("")
    verdict = {"ok": "all systems go",
               "warn": "usable, with caveats above",
               "error": "critical issue above — fix before use"}[report["overall"]]
    lines.append(f"{_ICON[report['overall']]} overall: {verdict}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Suite-level health check for llm-wiki-suite.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    report = build_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_human(report))

    if "fatal" in report:
        return 2
    return 1 if report["overall"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
