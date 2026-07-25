#!/usr/bin/env python3
"""Read-only capability and Provider health report for capture Skills."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tool_runtime import (
    ToolRuntimeError,
    resolve_command,
    resolve_python_provider,
)


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
# A postcheck only proves the already-resolved Provider runs. Command probes are
# `--version`-shaped and answer immediately; a python-profile probe imports the
# pack's whole dependency tree (torch, for both ASR backends), which takes tens
# of seconds on a cold page cache and must not be read as an absent pack.
POSTCHECK_TIMEOUT = {"command": 8, "python-profile": 30}


def load_catalog(path: Path | str | None = None) -> dict:
    catalog_path = Path(path) if path else DEFAULT_CATALOG
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read Provider catalog {catalog_path}: {exc}") from exc
    if data.get("version") != 4:
        raise ValueError(f"unsupported Provider catalog: {catalog_path}")
    profiles = data.get("profiles")
    tools = data.get("tools")
    if not isinstance(profiles, dict) or not isinstance(tools, dict):
        raise ValueError(f"invalid Provider catalog shape: {catalog_path}")
    for profile, spec in profiles.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("tools", []), list):
            raise ValueError(f"invalid profile definition: {profile}")
        unknown = sorted(set(spec.get("tools", [])) - set(tools))
        if unknown:
            raise ValueError(f"{profile} references unknown tool(s): {', '.join(unknown)}")
    for name, spec in tools.items():
        if not isinstance(spec, dict) or spec.get("kind") not in {"command", "python-profile"}:
            raise ValueError(f"invalid tool definition: {name}")
        postcheck = spec.get("postcheck", [])
        if not isinstance(postcheck, list) or any(not isinstance(arg, str) for arg in postcheck):
            raise ValueError(f"invalid postcheck: {name}")
        pack = spec.get("official_pack")
        if pack is not None and (not isinstance(pack, str) or not pack):
            raise ValueError(f"invalid official_pack: {name}")
    return data


def normalize_profiles(catalog: dict, profiles: list[str] | None = None) -> list[str]:
    if not profiles:
        return list(catalog["profiles"])
    result: list[str] = []
    for raw in profiles:
        profile = PROFILE_ALIASES.get(raw, raw)
        if profile not in catalog["profiles"]:
            raise ValueError(f"unknown capture profile: {raw}")
        if profile not in result:
            result.append(profile)
    return result


def profile_tool_names(catalog: dict, profiles: list[str]) -> list[str]:
    names: list[str] = []
    for profile in profiles:
        for name in catalog["profiles"][profile].get("tools", []):
            if name not in names:
                names.append(name)
    return names


def probe(catalog: dict, profiles: list[str]) -> dict[str, dict]:
    names = profile_tool_names(catalog, profiles)

    def inspect(name: str) -> tuple[str, dict]:
        spec = catalog["tools"][name]
        try:
            if spec["kind"] == "python-profile":
                resolved = resolve_python_provider(spec["profile"])
            else:
                resolved = resolve_command(name)
        except (ToolRuntimeError, OSError) as exc:
            return name, {"status": "missing", "error": str(exc)}
        identity = {
            "provider": resolved.provider,
            "source": resolved.source,
            "argv": resolved.argv,
        }
        timeout = POSTCHECK_TIMEOUT[spec["kind"]]
        try:
            completed = subprocess.run(
                [*resolved.argv, *spec.get("postcheck", [])],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
                env={**os.environ, **resolved.environment},
            )
        except subprocess.TimeoutExpired:
            # Resolution already proved the Provider is installed and that its
            # executable lives inside the pack; only the health check ran out of
            # time. Say so instead of reporting it as absent, which would send
            # the caller off to re-download a pack that is already here.
            return name, {
                "status": "unverified",
                "error": f"postcheck timed out after {timeout}s",
                **identity,
            }
        except (OSError, subprocess.SubprocessError) as exc:
            return name, {"status": "missing", "error": str(exc), **identity}
        if completed.returncode:
            return name, {
                "status": "missing",
                "error": f"postcheck exited with {completed.returncode}",
                **identity,
            }
        return name, {"status": "ok", **identity}

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(names)))) as executor:
        return dict(executor.map(inspect, names))


def _available(value: object) -> bool:
    if isinstance(value, dict):
        # `unverified` means resolved but not health-checked in time. Treat it as
        # usable: the capture can run and will fail loudly with a real error if
        # the Provider is genuinely broken, which beats blocking on a probe that
        # was merely slow.
        return value.get("status") in {"ok", "unverified"}
    return bool(value)


def _recommend(
    recommendations: list[dict],
    catalog: dict,
    tool: str,
    profile: str,
    reason: str,
    priority: str,
) -> None:
    spec = catalog["tools"][tool]
    existing = next((item for item in recommendations if item["tool"] == tool), None)
    if existing:
        if PRIORITY_ORDER[priority] > PRIORITY_ORDER[existing["priority"]]:
            existing["priority"] = priority
        if profile not in existing["profiles"]:
            existing["profiles"].append(profile)
        if reason not in existing["reasons"]:
            existing["reasons"].append(reason)
        return
    pack = spec.get("official_pack")
    recommendations.append(
        {
            "tool": tool,
            "priority": priority,
            "profiles": [profile],
            "reasons": [reason],
            "official_fallback": (
                {
                    "pack": pack,
                    "command": ["my-llm-wiki", "ensure-pack", pack],
                }
                if pack
                else None
            ),
            "home": spec.get("home", ""),
        }
    )


def assess_profiles(tools: dict, catalog: dict, profiles: list[str]) -> tuple[dict, list[dict]]:
    have = lambda name: _available(tools.get(name))  # noqa: E731
    capabilities: dict[str, dict] = {}
    recommendations: list[dict] = []
    for profile in profiles:
        if profile == "capture.web":
            if have("opencli"):
                capabilities[profile] = {"status": "ok", "via": "opencli", "quality": "full"}
            elif have("agent-reach"):
                capabilities[profile] = {"status": "degraded", "via": "agent-reach", "quality": "text-first"}
            else:
                capabilities[profile] = {"status": "degraded", "via": "agent web tools", "quality": "text-first"}
            if not have("opencli"):
                _recommend(recommendations, catalog, "opencli", profile, "authenticated/JS capture and asset localization", "recommended")
        elif profile == "capture.doc":
            if have("markitdown"):
                capabilities[profile] = {"status": "ok", "via": "markitdown"}
            else:
                capabilities[profile] = {"status": "unavailable", "missing": ["markitdown"]}
                _recommend(recommendations, catalog, "markitdown", profile, "local documents to Markdown", "required")
        elif profile == "capture.note":
            capabilities[profile] = {"status": "ok", "via": "no external provider"}
        elif profile == "capture.x.single":
            via = "opencli" if have("opencli") else "agent-reach" if have("agent-reach") else "fxtwitter fallback"
            capabilities[profile] = {"status": "ok", "via": via}
            if not have("opencli"):
                _recommend(recommendations, catalog, "opencli", profile, "logged-in X and media localization", "optional")
        elif profile == "capture.x.bookmarks":
            if have("opencli"):
                capabilities[profile] = {"status": "ok", "via": "opencli"}
            else:
                capabilities[profile] = {"status": "unavailable", "missing": ["opencli"]}
                _recommend(recommendations, catalog, "opencli", profile, "logged-in bookmark listing", "required")
        elif profile == "capture.video":
            caption_path = have("opencli") or have("yt-dlp")
            asr = [name for name in ("sensevoice", "faster-whisper") if have(name)]
            audio_path = have("yt-dlp") and have("ffmpeg") and bool(asr)
            status = "ok" if caption_path and audio_path else "degraded" if caption_path else "unavailable"
            capabilities[profile] = {
                "status": status,
                "via": "captions first, audio/ASR fallback",
                "details": {
                    "captioned": {"status": "ok" if caption_path else "unavailable"},
                    "no_captions": {"status": "ok" if audio_path else "unavailable"},
                },
                "asr": asr,
            }
            for name, reason, priority in (
                ("yt-dlp", "caption and audio retrieval", "required" if not caption_path else "recommended"),
                ("aria2c", "resilient parallel audio download fallback", "recommended"),
                ("ffmpeg", "audio extraction", "recommended"),
                ("sensevoice", "Chinese timestamped ASR", "recommended"),
                ("faster-whisper", "non-Chinese timestamped ASR", "recommended"),
            ):
                if not have(name):
                    _recommend(recommendations, catalog, name, profile, reason, priority)
        else:
            raise ValueError(f"unsupported capture profile: {profile}")
    return capabilities, recommendations


def build_report(
    profiles: list[str] | None = None,
    catalog_path: Path | str | None = None,
    *,
    tools: dict | None = None,
) -> dict:
    catalog = load_catalog(catalog_path)
    selected = normalize_profiles(catalog, profiles)
    tools = probe(catalog, selected) if tools is None else tools
    capabilities, recommendations = assess_profiles(tools, catalog, selected)
    tool_report = {}
    for name in profile_tool_names(catalog, selected):
        spec = catalog["tools"][name]
        raw = tools.get(name)
        if isinstance(raw, dict):
            item = dict(raw)
        else:
            item = {"status": "ok" if raw else "missing", "source": raw or ""}
        item.update({"description": spec.get("description", ""), "home": spec.get("home", "")})
        tool_report[name] = item
    states = {item.get("status") for item in capabilities.values()}
    status = "action-required" if "unavailable" in states else "warn" if "degraded" in states else "ok"
    return {
        "status": status,
        "provider_policy": "explicit override → saved override → official → system/custom",
        "profiles": selected,
        "tools": tool_report,
        "capabilities": capabilities,
        "recommendations": recommendations,
    }


def emit_human(report: dict) -> None:
    print(f"provider policy: {report['provider_policy']}")
    print("profiles:")
    for profile in report["profiles"]:
        print(f"  - {profile}")
    print("providers:")
    for name, info in report["tools"].items():
        identity = f"{info.get('provider', 'available')} ({info.get('source', '')})"
        if info["status"] == "ok":
            print(f"  {name}: {identity}")
        elif info["status"] == "unverified":
            print(f"  {name}: {identity} — unverified: {info.get('error', '')}")
        else:
            detail = info.get("error")
            print(f"  {name}: missing{f' — {detail}' if detail else ''}")
    print("capabilities:")
    for profile, info in report["capabilities"].items():
        print(f"  {profile}: {json.dumps(info, ensure_ascii=False)}")
    print("recommendations:")
    for item in report["recommendations"]:
        print(f"  - {item['tool']} [{item['priority']}]: {'; '.join(item['reasons'])}")
        if item["official_fallback"]:
            print(f"    official fallback: {' '.join(item['official_fallback']['command'])}")
    if not report["recommendations"]:
        print("  []")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    args = parser.parse_args()
    try:
        report = build_report(args.profile, args.catalog)
    except (ValueError, ToolRuntimeError) as exc:
        print(f"preflight error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        emit_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
