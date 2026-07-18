#!/usr/bin/env python3
"""Profile-aware capture toolchain probe.

Skills declare capability profile ids in ``registry/skills.json``. This script
evaluates only the requested profiles, using the sibling
``references/toolchain.json`` as the single source for tool descriptions,
project homes, and structured OS/network-route install argv.

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
import json
import os
import platform
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = SKILL_DIR / "references" / "toolchain.json"
NETWORK_PROBE = SKILL_DIR.parents[1] / "skills" / "cn-mirrors" / "scripts" / "net_probe.py"
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
    for name, spec in data["tools"].items():
        if not isinstance(spec, dict):
            raise ValueError(f"invalid tool definition: {name}")
        probe_spec = spec.get("probe")
        if not isinstance(probe_spec, dict) or probe_spec.get("kind") not in {
            "command", "python-module"
        } or not isinstance(probe_spec.get("name"), str):
            raise ValueError(f"invalid probe definition: {name}")
        extra_paths = probe_spec.get("extra_paths", [])
        if not isinstance(extra_paths, list) or any(
            not isinstance(path, str) or not path for path in extra_paths
        ):
            raise ValueError(f"invalid probe extra_paths: {name}")
        installs = spec.get("install")
        if installs is None:
            continue
        if not isinstance(installs, dict):
            raise ValueError(f"invalid install recipes: {name}")
        for field in ("step_timeout_seconds", "postcheck_timeout_seconds"):
            if not isinstance(spec.get(field), int) or spec[field] <= 0:
                raise ValueError(f"invalid {field}: {name}")
        postcheck = spec.get("postcheck")
        if (
            not isinstance(postcheck, list)
            or not postcheck
            or any(not isinstance(arg, str) or not arg for arg in postcheck)
        ):
            raise ValueError(f"invalid postcheck: {name}")
        valid_platforms = {
            "darwin", "darwin-brew", "linux", "linux-apt", "linux-apt-root",
            "linux-dnf", "linux-dnf-root", "linux-pacman", "linux-pacman-root",
            "windows", "windows-winget",
        }
        route_ecosystem = spec.get("route_ecosystem", {})
        if not isinstance(route_ecosystem, dict) or any(
            key not in valid_platforms
            or value not in {"github", "pypi", "npm", "huggingface", "system"}
            for key, value in route_ecosystem.items()
        ):
            raise ValueError(f"invalid route_ecosystem: {name}")
        for os_name, routes in installs.items():
            if os_name not in valid_platforms or not isinstance(routes, dict):
                raise ValueError(f"invalid install platform for {name}: {os_name}")
            for route, recipe in routes.items():
                if route not in {"global", "cn"} or not isinstance(recipe, dict):
                    raise ValueError(f"invalid install route for {name}: {os_name}/{route}")
                steps = recipe.get("steps")
                if not isinstance(steps, list) or not steps or any(
                    not isinstance(step, list)
                    or not step
                    or any(not isinstance(arg, str) or not arg for arg in step)
                    for step in steps
                ):
                    raise ValueError(f"invalid argv steps for {name}: {os_name}/{route}")
                env = recipe.get("env", {})
                if not isinstance(env, dict) or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in env.items()
                ):
                    raise ValueError(f"invalid install env for {name}: {os_name}/{route}")
                recipe_postcheck = recipe.get("postcheck")
                if recipe_postcheck is not None and (
                    not isinstance(recipe_postcheck, list)
                    or not recipe_postcheck
                    or any(not isinstance(arg, str) or not arg for arg in recipe_postcheck)
                ):
                    raise ValueError(
                        f"invalid recipe postcheck for {name}: {os_name}/{route}"
                    )
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


def command_tool(spec: dict, name: str) -> str:
    """Return the executable only after its declared postcheck succeeds."""
    path = shutil.which(name)
    if not path:
        # Portable installs (e.g. fetch_ffmpeg.py) live in declared well-known
        # directories rather than on PATH.
        extra_dirs = [
            str(Path(entry).expanduser())
            for entry in (spec.get("probe") or {}).get("extra_paths", [])
        ]
        extra_dirs = [entry for entry in extra_dirs if Path(entry).is_dir()]
        if extra_dirs:
            path = shutil.which(name, path=os.pathsep.join(extra_dirs))
    if not path:
        return ""
    postcheck = spec.get("postcheck") or [name, "--help"]
    if not isinstance(postcheck, list) or not postcheck:
        raise ValueError(f"invalid command postcheck for {name}")
    argv = [path, *postcheck[1:]]
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return path if result.returncode == 0 else ""


_ASR_VENV = Path.home() / ".local" / "share" / "llm-wiki" / "asr-venv"
ASR_VENV_PYTHON = _ASR_VENV / (
    "Scripts/python.exe" if os.name == "nt" else "bin/python"
)


@functools.lru_cache(maxsize=None)
def _module_python(module: str) -> str:
    """Return an interpreter that can import ``module``, or ""."""
    candidates = [os.environ.get("LLM_WIKI_ASR_PYTHON", ""), str(ASR_VENV_PYTHON), sys.executable]
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        resolved = str(Path(candidate).expanduser().resolve()) if Path(candidate).exists() else candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            result = subprocess.run(
                [candidate, "-c", "import importlib.util as u,sys; "
                 f"sys.exit(0 if u.find_spec('{module}') else 1)"],
                capture_output=True,
                timeout=2,
            )
            if result.returncode == 0:
                return candidate
        except Exception:  # noqa: BLE001 - a candidate interpreter may be unusable
            pass
    return ""


@functools.lru_cache(maxsize=1)
def network_routes(timeout: float = 3.0) -> dict[str, str]:
    defaults = {name: "unavailable" for name in ("github", "pypi", "npm", "huggingface")}
    defaults["system"] = "global"
    try:
        result = subprocess.run(
            [sys.executable, str(NETWORK_PROBE), "--json", "--timeout", str(timeout)],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout + 4,
        )
        report = json.loads(result.stdout)
        routes = report.get("ecosystems")
        if isinstance(routes, dict):
            return {**defaults, **{str(k): str(v) for k, v in routes.items()}}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    return defaults


def platform_keys() -> list[str]:
    system = platform.system().lower()
    if system == "darwin":
        return (["darwin-brew", "darwin"] if shutil.which("brew") else ["darwin"])
    if system == "windows":
        return (["windows-winget", "windows"] if shutil.which("winget") else ["windows"])
    if system == "linux":
        root = hasattr(os, "geteuid") and os.geteuid() == 0
        sudo = shutil.which("sudo") is not None
        for manager in ("apt-get", "dnf", "pacman"):
            if shutil.which(manager) and (root or sudo):
                suffix = manager.removesuffix("-get")
                key = f"linux-{suffix}{'-root' if root else ''}"
                return [key, "linux"]
        return ["linux"]
    return []


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


def install_recipe(spec: dict, routes: dict[str, str]) -> dict | None:
    installs = spec.get("install")
    if not isinstance(installs, dict):
        return None
    replacements = {
        "python": sys.executable,
        "asr_venv": str(_ASR_VENV),
        "asr_python": str(ASR_VENV_PYTHON),
        "suite": str(SKILL_DIR.parents[1]),
    }
    execution = {
        "step_timeout_seconds": spec["step_timeout_seconds"],
        "postcheck_timeout_seconds": spec["postcheck_timeout_seconds"],
    }
    missing_prerequisites = [
        command for command in spec.get("install_requires", [])
        if shutil.which(command) is None
    ]
    if missing_prerequisites:
        return {
            **execution,
            "route": "unavailable",
            "reason": "missing install prerequisite(s): " + ", ".join(missing_prerequisites),
            "steps": [],
            "env": {},
            "runtime_env": {},
            "unavailable_runtime": [],
            "postcheck": _replace_placeholders(spec.get("postcheck", []), replacements),
        }
    selected_platform = next(
        (key for key in platform_keys() if isinstance(installs.get(key), dict)), None
    )
    if selected_platform is None:
        return {
            **execution,
            "route": "unavailable",
            "reason": f"no install recipe for {platform.system()} on this machine",
            "steps": [],
            "env": {},
            "runtime_env": {},
            "unavailable_runtime": [],
            "postcheck": _replace_placeholders(spec.get("postcheck", []), replacements),
        }
    # A platform recipe may depend on a different ecosystem than the tool's
    # own (winget's ffmpeg payload downloads from GitHub Releases); route by
    # the declared override so a restricted network selects the cn recipe.
    ecosystem = (spec.get("route_ecosystem") or {}).get(
        selected_platform, spec.get("ecosystem", "system")
    )
    route = routes.get(ecosystem, "unavailable")
    if route == "unavailable":
        return {
            **execution,
            "route": route,
            "reason": f"{ecosystem} has no reachable global or cn route",
            "steps": [],
            "env": {},
            "runtime_env": {},
            "unavailable_runtime": [],
            "postcheck": _replace_placeholders(spec.get("postcheck", []), replacements),
        }
    platform_recipes = installs[selected_platform]
    if route not in platform_recipes:
        raise ValueError(
            f"no {selected_platform}/{route} install recipe for {spec.get('description', 'tool')}"
        )
    recipe = _replace_placeholders(platform_recipes[route], replacements)
    env = dict(recipe.get("env", {}))
    selected_runtime_env = {}
    unavailable_runtime = []
    runtime_env = spec.get("runtime_env") or {}
    for runtime_ecosystem in spec.get("runtime_ecosystems", []):
        runtime_route = routes.get(runtime_ecosystem, "unavailable")
        if runtime_route == "unavailable":
            unavailable_runtime.append(runtime_ecosystem)
            continue
        overlay = (runtime_env.get(runtime_ecosystem) or {}).get(runtime_route) or {}
        selected_runtime_env.update(_replace_placeholders(overlay, replacements))
    return {
        **execution,
        "route": route,
        "platform": selected_platform,
        "steps": recipe.get("steps", []),
        "env": env,
        "runtime_env": selected_runtime_env,
        "unavailable_runtime": unavailable_runtime,
        "postcheck": recipe.get("postcheck")
        or _replace_placeholders(spec.get("postcheck", []), replacements),
    }


def probe(catalog: dict, profiles: list[str]) -> dict:
    """Probe only tools relevant to the requested profiles."""
    names = profile_tool_names(catalog, profiles)

    def inspect(name: str) -> tuple[str, str]:
        spec = catalog["tools"][name]
        probe_spec = spec["probe"]
        probe_name = str(probe_spec.get("name", name))
        if probe_spec.get("kind") == "python-module":
            return name, _module_python(probe_name)
        return name, command_tool(spec, probe_name)

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(names)))) as executor:
        return dict(executor.map(inspect, names))


def _add_recommendation(
    recommendations: list[dict],
    catalog: dict,
    tool: str,
    profile: str,
    reason: str,
    priority: str,
    routes: dict[str, str],
) -> None:
    spec = catalog["tools"][tool]
    existing = next((r for r in recommendations if r["tool"] == tool), None)
    install = install_recipe(spec, routes)
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
    routes: dict[str, str],
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
                    "recommended", routes,
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
                    "required", routes,
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
                    "optional", routes,
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
                    "required", routes,
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
                    "required" if not caption_path else "recommended", routes,
                )
            if not have("ffmpeg"):
                _add_recommendation(
                    recommendations, catalog, "ffmpeg", profile,
                    "audio conversion for videos without captions",
                    "recommended", routes,
                )
            if not have("sensevoice"):
                _add_recommendation(
                    recommendations, catalog, "sensevoice", profile,
                    "preferred local ASR for Chinese videos without captions",
                    "recommended", routes,
                )
            if not have("faster-whisper"):
                _add_recommendation(
                    recommendations, catalog, "faster-whisper", profile,
                    "preferred local ASR for non-Chinese videos without captions",
                    "recommended", routes,
                )
        else:  # guarded by normalize_profiles; protects direct callers too
            raise ValueError(f"unsupported capture profile: {profile}")

    return capabilities, recommendations


def build_report(
    profiles: list[str] | None = None,
    catalog_path: Path | str | None = None,
    *,
    tools: dict | None = None,
    routes: dict[str, str] | None = None,
) -> dict:
    catalog = load_catalog(catalog_path)
    profiles = normalize_profiles(catalog, profiles)
    if tools is None and routes is None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            tools_future = executor.submit(probe, catalog, profiles)
            routes_future = executor.submit(network_routes)
            tools = tools_future.result()
            routes = routes_future.result()
    else:
        tools = probe(catalog, profiles) if tools is None else tools
        routes = network_routes() if routes is None else routes
    routes = {"system": "global", **routes}
    capabilities, recommendations = assess_profiles(tools, catalog, profiles, routes)

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
        tool_report[name] = item

    capability_states = {c.get("status") for c in capabilities.values()}
    status = (
        "action-required" if "unavailable" in capability_states
        else "warn" if "degraded" in capability_states
        else "ok"
    )
    return {
        "status": status,
        "network": {"ecosystems": routes},
        "profiles": profiles,
        "tools": tool_report,
        "capabilities": capabilities,
        "recommendations": recommendations,
    }


def emit_human(report: dict) -> None:
    print("network:")
    for key, value in report["network"]["ecosystems"].items():
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
                print(f"    route: {rec['install']['route']}")
                if rec["install"].get("reason"):
                    print(f"    reason: {rec['install']['reason']}")
                if rec["install"].get("env"):
                    print(f"    env: {json.dumps(rec['install']['env'], ensure_ascii=False)}")
                if rec["install"].get("runtime_env"):
                    print(
                        "    runtime env: "
                        + json.dumps(rec["install"]["runtime_env"], ensure_ascii=False)
                    )
                if rec["install"].get("unavailable_runtime"):
                    print(
                        "    unavailable runtime ecosystems: "
                        + ", ".join(rec["install"]["unavailable_runtime"])
                    )
                for step in rec["install"].get("steps", []):
                    print(f"    step: {json.dumps(step, ensure_ascii=False)}")
                print(
                    "    step timeout: "
                    f"{rec['install']['step_timeout_seconds']}s"
                )
                if rec["install"].get("postcheck"):
                    print(f"    postcheck: {json.dumps(rec['install']['postcheck'], ensure_ascii=False)}")
                    print(
                        "    postcheck timeout: "
                        f"{rec['install']['postcheck_timeout_seconds']}s"
                    )
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
