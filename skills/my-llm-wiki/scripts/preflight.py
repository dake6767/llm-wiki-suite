#!/usr/bin/env python3
"""Profile-aware capture toolchain probe.

Skills declare capability profile ids in ``registry/skills.json``. This script
evaluates only the requested profiles, using the sibling
``references/toolchain.json`` as the single source for tool descriptions,
project homes, and normal/mainland-China install commands.

Examples:

    python3 <skill>/scripts/preflight.py
    python3 <skill>/scripts/preflight.py --profile capture.video
    python3 <skill>/scripts/preflight.py --profile capture.x.single --json

The probe never installs anything. A recommendation is an opt-in handoff to the
user, not permission to mutate the machine.
"""

from __future__ import annotations

import argparse
import functools
import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = SKILL_DIR / "references" / "toolchain.json"
PROFILE_ALIASES = {
    "web": "capture.web",
    "doc": "capture.doc",
    "note": "capture.note",
    "x": "capture.x.single",
    "x-single": "capture.x.single",
    "x-bookmarks": "capture.x.bookmarks",
    "video": "capture.video",
}
PRIORITY_ORDER = {"optional": 0, "recommended": 1, "required": 2}


def load_catalog(path: Path | str | None = None) -> dict:
    catalog_path = Path(path) if path else DEFAULT_CATALOG
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface read/parse failures
        raise ValueError(f"cannot read toolchain catalog {catalog_path}: {exc}") from exc
    if not isinstance(data.get("profiles"), dict) or not isinstance(data.get("tools"), dict):
        raise ValueError(f"invalid toolchain catalog shape: {catalog_path}")
    for profile, spec in data["profiles"].items():
        if not isinstance(spec, dict) or not isinstance(spec.get("tools", []), list):
            raise ValueError(f"invalid profile definition: {profile}")
        unknown = sorted(set(spec.get("tools", [])) - set(data["tools"]))
        if unknown:
            raise ValueError(f"{profile} references unknown tool(s): {', '.join(unknown)}")
    return data


def normalize_profiles(catalog: dict, profiles: list[str] | None = None) -> list[str]:
    if not profiles:
        return list(catalog["profiles"])
    out = []
    for raw in profiles:
        profile = PROFILE_ALIASES.get(raw, raw)
        if profile not in catalog["profiles"]:
            raise ValueError(f"unknown capture profile: {raw}")
        if profile not in out:
            out.append(profile)
    return out


def profile_tool_names(catalog: dict, profiles: list[str]) -> list[str]:
    names = []
    for profile in profiles:
        for name in catalog["profiles"][profile].get("tools", []):
            if name not in names:
                names.append(name)
    return names


def _candidate_dirs() -> list[str]:
    home = str(Path.home())
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", f"{home}/AppData/Local")
        roaming = os.environ.get("APPDATA", f"{home}/AppData/Roaming")
        dirs = [
            f"{local}/Microsoft/WinGet/Links",
            f"{local}/Microsoft/WinGet/Packages/*",
            f"{local}/Microsoft/WinGet/Packages/*/*/bin",
            f"{home}/scoop/shims",
            "C:/ProgramData/chocolatey/bin",
            f"{roaming}/npm",
            f"{roaming}/Python/Python3*/Scripts",
            f"{local}/Programs/Python/Python3*/Scripts",
        ]
    else:
        dirs = glob.glob(f"{home}/.nvm/versions/node/*/bin")
        dirs += [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            f"{home}/.local/bin",
            f"{home}/Library/Python/3.*/bin",
        ]
    out = []
    for directory in dirs:
        out += glob.glob(directory) if "*" in directory else [directory]
    return out


_EXTS = ("", ".exe", ".cmd", ".bat") if os.name == "nt" else ("",)


def find_tool(name: str) -> str | None:
    path = shutil.which(name)
    if path:
        return path
    for directory in _candidate_dirs():
        for ext in _EXTS:
            candidate = Path(directory) / (name + ext)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


_ASR_VENV = Path.home() / ".local" / "share" / "llm-wiki" / "asr-venv"
ASR_VENV_PYTHON = _ASR_VENV / (
    "Scripts/python.exe" if os.name == "nt" else "bin/python"
)


@functools.lru_cache(maxsize=None)
def _module_python(module: str) -> str:
    """Return an interpreter that can import ``module``, or ""."""
    candidates = [
        os.environ.get("LLM_WIKI_ASR_PYTHON", ""),
        sys.executable,
        "python3",
        str(ASR_VENV_PYTHON),
    ]
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            result = subprocess.run(
                [candidate, "-c", "import importlib.util as u,sys; "
                 f"sys.exit(0 if u.find_spec('{module}') else 1)"],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0:
                return candidate
        except Exception:  # noqa: BLE001 - a candidate interpreter may be unusable
            pass
    return ""


def _torch_version(python: str) -> str:
    if not python:
        return ""
    try:
        result = subprocess.run(
            [python, "-c", "import torch; print(torch.__version__)"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:  # noqa: BLE001 - diagnostic only
        return ""


@functools.lru_cache(maxsize=1)
def github_reachable(timeout: float = 3.0) -> bool:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        "https://api.github.com/",
        method="HEAD",
        headers={"User-Agent": "llm-wiki-preflight"},
    )
    try:
        urllib.request.urlopen(request, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001 - timeout/offline/restricted all mean unreachable
        return False


def probe(catalog: dict, profiles: list[str]) -> dict:
    """Probe only tools relevant to the requested profiles."""
    tools = {}
    for name in profile_tool_names(catalog, profiles):
        spec = catalog["tools"][name]
        probe_name = spec.get("probe", name)
        if probe_name.startswith("python:"):
            tools[name] = _module_python(probe_name.split(":", 1)[1])
            if name == "sensevoice":
                tools["_sensevoice_torch"] = _torch_version(tools[name])
        else:
            tools[name] = find_tool(probe_name) or ""
    return tools


def _add_recommendation(
    recommendations: list[dict],
    catalog: dict,
    tool: str,
    profile: str,
    reason: str,
    priority: str,
    github_ok: bool,
) -> None:
    spec = catalog["tools"][tool]
    existing = next((r for r in recommendations if r["tool"] == tool), None)
    install_key = "install" if github_ok else "install_cn"
    install = spec.get(install_key) or spec.get("install", "")
    if existing is None:
        recommendations.append({
            "tool": tool,
            "priority": priority,
            "profiles": [profile],
            "reasons": [reason],
            "install": install,
            "home": spec.get("home", ""),
        })
        return
    if PRIORITY_ORDER[priority] > PRIORITY_ORDER[existing["priority"]]:
        existing["priority"] = priority
    if profile not in existing["profiles"]:
        existing["profiles"].append(profile)
    if reason not in existing["reasons"]:
        existing["reasons"].append(reason)


def assess_profiles(
    tools: dict,
    catalog: dict,
    profiles: list[str],
    github_ok: bool = True,
) -> tuple[dict, list[dict]]:
    """Return profile results and structured, deduplicated recommendations."""
    have = lambda name: bool(tools.get(name))  # noqa: E731 - compact capability checks
    capabilities: dict[str, dict] = {}
    recommendations: list[dict] = []

    for profile in profiles:
        if profile == "capture.web":
            if have("opencli"):
                capabilities[profile] = {
                    "status": "ok",
                    "via": "opencli web read",
                    "note": "browser render + image localization",
                }
            elif have("agent-reach"):
                capabilities[profile] = {
                    "status": "degraded",
                    "via": "agent-reach / agent WebFetch",
                    "note": "text works; media localization needs extra work",
                }
            else:
                capabilities[profile] = {
                    "status": "degraded",
                    "via": "agent WebFetch / tavily-extract",
                    "note": "text-mostly; JS/auth pages and images are weaker",
                }
            if not have("opencli"):
                _add_recommendation(
                    recommendations, catalog, "opencli", profile,
                    "cleaner Web/WeChat/Xiaohongshu capture with localized images",
                    "recommended", github_ok,
                )

        elif profile == "capture.doc":
            if have("markitdown"):
                capabilities[profile] = {"status": "ok", "via": "markitdown"}
            else:
                capabilities[profile] = {
                    "status": "unavailable",
                    "missing": ["markitdown"],
                    "note": "local documents cannot be converted",
                }
                _add_recommendation(
                    recommendations, catalog, "markitdown", profile,
                    "required for local PDF/docx/pptx/xlsx/epub capture",
                    "required", github_ok,
                )

        elif profile == "capture.note":
            capabilities[profile] = {"status": "ok", "via": "no external tool"}

        elif profile == "capture.x.single":
            if have("opencli"):
                capabilities[profile] = {"status": "ok", "via": "opencli twitter/web"}
            elif have("agent-reach"):
                capabilities[profile] = {"status": "ok", "via": "agent-reach twitter"}
            else:
                capabilities[profile] = {
                    "status": "ok",
                    "via": "fxtwitter fallback",
                    "note": "browser-free via my-llm-wiki-x scripts/fx_capture.py; login-gated/protected content remains limited",
                }
                _add_recommendation(
                    recommendations, catalog, "opencli", profile,
                    "preferred for logged-in X pages and automatic media localization",
                    "optional", github_ok,
                )

        elif profile == "capture.x.bookmarks":
            if have("opencli"):
                capabilities[profile] = {
                    "status": "ok",
                    "via": "opencli twitter bookmarks",
                    "note": "a one-time platform login may still be required",
                }
            else:
                capabilities[profile] = {
                    "status": "unavailable",
                    "missing": ["opencli"],
                    "note": "no adapter can list the logged-in user's bookmarks",
                }
                _add_recommendation(
                    recommendations, catalog, "opencli", profile,
                    "required to list and incrementally sync logged-in X bookmarks",
                    "required", github_ok,
                )

        elif profile == "capture.video":
            asr = (
                "faster-whisper" if have("faster-whisper")
                else "whisper" if have("whisper") else ""
            )
            asr_any = bool(asr) or have("sensevoice")
            if asr and have("sensevoice"):
                asr_desc = f"zh→SenseVoice, else {asr}"
            elif have("sensevoice"):
                asr_desc = "SenseVoice (Chinese only; add Whisper for other languages)"
            elif asr:
                asr_desc = f"{asr} (Chinese routes to SenseVoice — not installed)"
            else:
                asr_desc = "none"

            caption_path = have("opencli") or have("yt-dlp")
            audio_fallback = have("yt-dlp") and have("ffmpeg") and asr_any
            caption_detail = {
                "status": "ok" if caption_path else "unavailable",
                "via": (
                    "opencli captions" if have("opencli")
                    else "yt-dlp subtitles" if have("yt-dlp") else ""
                ),
            }
            missing = []
            if not have("yt-dlp"):
                missing.append("yt-dlp")
            if not have("ffmpeg"):
                missing.append("ffmpeg")
            if not asr_any:
                missing.append("ASR backend")
            no_caption_detail = {
                "status": "ok" if audio_fallback else "unavailable",
                "via": f"yt-dlp + ffmpeg + {asr_desc}" if audio_fallback else "",
                "missing": missing,
            }

            if not caption_path:
                status = "unavailable"
                note = "no caption retrieval path; video capture cannot start"
            elif not audio_fallback:
                status = "degraded"
                note = "captioned videos work; videos without captions fail"
            else:
                status = "ok"
                note = "captioned and no-caption paths available"
            capabilities[profile] = {
                "status": status,
                "via": "captions first, audio/ASR fallback",
                "asr": asr_desc,
                "note": note,
                "details": {
                    "captioned": caption_detail,
                    "no_captions": no_caption_detail,
                },
            }

            if not have("yt-dlp"):
                _add_recommendation(
                    recommendations, catalog, "yt-dlp", profile,
                    "caption retrieval and the audio-only fallback",
                    "required" if not caption_path else "recommended", github_ok,
                )
            if not have("ffmpeg"):
                _add_recommendation(
                    recommendations, catalog, "ffmpeg", profile,
                    "audio conversion for videos without captions",
                    "recommended", github_ok,
                )
            if not have("sensevoice"):
                _add_recommendation(
                    recommendations, catalog, "sensevoice", profile,
                    "preferred local ASR for Chinese videos without captions",
                    "recommended", github_ok,
                )
            if not have("faster-whisper"):
                _add_recommendation(
                    recommendations, catalog, "faster-whisper", profile,
                    "preferred local ASR for non-Chinese videos without captions",
                    "recommended", github_ok,
                )
        else:  # guarded by normalize_profiles; protects direct callers too
            raise ValueError(f"unsupported capture profile: {profile}")

    return capabilities, recommendations


def build_report(
    profiles: list[str] | None = None,
    catalog_path: Path | str | None = None,
    *,
    tools: dict | None = None,
    github_ok: bool | None = None,
) -> dict:
    catalog = load_catalog(catalog_path)
    profiles = normalize_profiles(catalog, profiles)
    tools = probe(catalog, profiles) if tools is None else tools
    if github_ok is None:
        capabilities, recommendations = assess_profiles(tools, catalog, profiles, True)
        if recommendations:
            github_ok = github_reachable()
            if not github_ok:
                capabilities, recommendations = assess_profiles(
                    tools, catalog, profiles, False
                )
    else:
        capabilities, recommendations = assess_profiles(tools, catalog, profiles, github_ok)

    relevant_tools = profile_tool_names(catalog, profiles)
    tool_report = {}
    for name in relevant_tools:
        spec = catalog["tools"][name]
        item = {
            "status": "ok" if tools.get(name) else "missing",
            "path": tools.get(name, ""),
            "description": spec.get("description", ""),
            "home": spec.get("home", ""),
        }
        if name == "sensevoice" and tools.get(name):
            item["torch"] = tools.get("_sensevoice_torch", "")
        tool_report[name] = item

    status = "ok"
    if any(c.get("status") in ("degraded", "unavailable") for c in capabilities.values()):
        status = "warn"
    return {
        "status": status,
        "network": {
            "github.com": (
                "not-probed" if github_ok is None
                else "reachable" if github_ok else "unreachable"
            ),
            "install_variant": (
                "none" if github_ok is None
                else "install" if github_ok else "install_cn"
            ),
        },
        "profiles": profiles,
        "tools": tool_report,
        "capabilities": capabilities,
        "recommendations": recommendations,
    }


def emit_human(report: dict) -> None:
    print("network:")
    for key, value in report["network"].items():
        print(f"  {key}: {value}")
    print("profiles:")
    for profile in report["profiles"]:
        print(f"  - {profile}")
    print("tools:")
    if report["tools"]:
        for name, info in report["tools"].items():
            value = info["path"] if info["path"] else f"missing  # {info['description']}"
            print(f"  {name}: {value}")
    else:
        print("  {}")
    print("capabilities:")
    for profile, info in report["capabilities"].items():
        print(f"  {profile}:")
        for key, value in info.items():
            if isinstance(value, (dict, list)):
                print(f"    {key}: {json.dumps(value, ensure_ascii=False)}")
            else:
                print(f"    {key}: {value}")
    print("recommendations:")
    if report["recommendations"]:
        for rec in report["recommendations"]:
            reasons = "; ".join(rec["reasons"])
            print(f"  - {rec['tool']} [{rec['priority']}]: {reasons}")
            if rec["install"]:
                print(f"    install: {rec['install']}")
            if rec["home"]:
                print(f"    home: {rec['home']}")
    else:
        print("  []")


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe capture capability profiles.")
    ap.add_argument(
        "--profile",
        action="append",
        default=[],
        help="profile id or alias; repeat to select several (default: all)",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    ap.add_argument("--quiet", action="store_true", help="deprecated; retained for compatibility")
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    args = ap.parse_args()

    try:
        report = build_report(args.profile, args.catalog)
    except ValueError as exc:
        print(f"preflight error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        emit_human(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
