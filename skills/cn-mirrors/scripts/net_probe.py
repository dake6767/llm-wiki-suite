#!/usr/bin/env python3
"""Network-environment probe — which dev hosts can this machine actually reach?

Companion to the cn-mirrors skill: before recommending install commands, run
this to learn whether the machine sits behind a restricted (mainland-China
style) network. Pure stdlib, no curl/wget, works on macOS / Linux / Windows.
It NEVER installs or changes configuration — it only reports (same philosophy
as my-llm-wiki's preflight.py).

    python3 net_probe.py            # human-readable YAML-ish report
    python3 net_probe.py --json     # machine-readable, for preflight/doctor
    python3 net_probe.py --timeout 5

Statuses per endpoint: ok (HTTP response, fast), slow (HTTP response past the
slow threshold), blocked (timeout / reset / DNS failure). Any HTTP status code
counts as reachable — 403/404 from a CDN edge still proves connectivity.

Verdict: open | restricted | mixed | offline (see cn-mirrors SKILL.md §1).
The verdict is per-session — a VPN or proxy toggling changes it. Don't cache.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# (host, url, what breaks when it's unreachable)
DEV_ENDPOINTS = [
    ("github.com", "https://github.com/", "git clone, releases pages"),
    ("api.github.com", "https://api.github.com/", "gh CLI, release metadata (install scripts)"),
    ("raw.githubusercontent.com", "https://raw.githubusercontent.com/", "raw file fetches, many curl|sh installers"),
    ("objects.githubusercontent.com", "https://objects.githubusercontent.com/", "release asset downloads"),
    ("pypi.org", "https://pypi.org/", "pip index"),
    ("files.pythonhosted.org", "https://files.pythonhosted.org/", "pip package downloads"),
    ("registry.npmjs.org", "https://registry.npmjs.org/", "npm installs"),
    ("huggingface.co", "https://huggingface.co/", "HF model/dataset downloads (faster-whisper, transformers)"),
]

# Domestic controls: reachable here + blocked above ⇒ restricted network,
# not an offline machine.
CONTROL_ENDPOINTS = [
    ("modelscope.cn", "https://modelscope.cn/", "domestic control (also FunASR's default model hub)"),
    ("www.baidu.com", "https://www.baidu.com/", "domestic control"),
]

MIRROR_ENDPOINTS = [
    ("gitee.com", "https://gitee.com/", "Git repository mirror"),
    ("pypi.tuna.tsinghua.edu.cn", "https://pypi.tuna.tsinghua.edu.cn/simple/", "PyPI mirror"),
    ("registry.npmmirror.com", "https://registry.npmmirror.com/", "npm mirror"),
    ("hf-mirror.com", "https://hf-mirror.com/", "Hugging Face mirror"),
]

# Session-scoped mirror advice keyed by the endpoint(s) that being blocked
# triggers it. Kept in sync with cn-mirrors SKILL.md §2 (the authority).
MIRROR_ADVICE = [
    ({"pypi.org", "files.pythonhosted.org"},
     "pip: add `-i https://pypi.tuna.tsinghua.edu.cn/simple` (pipx: --pip-args)"),
    ({"registry.npmjs.org"},
     "npm: add `--registry=https://registry.npmmirror.com`"),
    ({"huggingface.co"},
     "HF models: prefix commands with `HF_ENDPOINT=https://hf-mirror.com`"),
    ({"github.com", "objects.githubusercontent.com"},
     "GitHub clone/releases: use the project's Gitee/CNB mirror if it has one; "
     "else an accelerator prefix (public repos only — see cn-mirrors SKILL.md §2 caveat)"),
    ({"github.com"},
     "yt-dlp: install/update via pip through the PyPI mirror, not brew/winget "
     "(its self-update hits GitHub Releases)"),
]

SLOW_MS = 2500


def probe_one(host: str, url: str, timeout: float) -> dict:
    req = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": "cn-mirrors-net-probe/1"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            pass
        status = "ok"
    except urllib.error.HTTPError:
        status = "ok"  # an HTTP error IS a response — the host is reachable
    except Exception as e:
        return {"host": host, "status": "blocked", "latency_ms": None,
                "error": type(e).__name__}
    ms = int((time.monotonic() - t0) * 1000)
    return {"host": host, "status": "slow" if ms > SLOW_MS else "ok",
            "latency_ms": ms, "error": ""}


def probe_all(timeout: float) -> tuple[list[dict], list[dict], list[dict]]:
    eps = DEV_ENDPOINTS + MIRROR_ENDPOINTS + CONTROL_ENDPOINTS
    with ThreadPoolExecutor(max_workers=len(eps)) as ex:
        results = list(ex.map(lambda e: probe_one(e[0], e[1], timeout), eps))
    dev_end = len(DEV_ENDPOINTS)
    mirror_end = dev_end + len(MIRROR_ENDPOINTS)
    return results[:dev_end], results[dev_end:mirror_end], results[mirror_end:]


def ecosystem_routes(dev: list[dict], mirrors: list[dict]) -> dict[str, str]:
    statuses = {row["host"]: row["status"] for row in dev + mirrors}

    def choose(global_hosts: tuple[str, ...], mirror_host: str) -> str:
        if all(statuses.get(host) == "ok" for host in global_hosts):
            return "global"
        if statuses.get(mirror_host) == "ok":
            return "cn"
        return "unavailable"

    return {
        "github": choose(("github.com", "api.github.com", "objects.githubusercontent.com"), "gitee.com"),
        "pypi": choose(("pypi.org", "files.pythonhosted.org"), "pypi.tuna.tsinghua.edu.cn"),
        "npm": choose(("registry.npmjs.org",), "registry.npmmirror.com"),
        "huggingface": choose(("huggingface.co",), "hf-mirror.com"),
        "system": "global",
    }


def verdict_of(dev: list[dict], control: list[dict]) -> str:
    bad = [r for r in dev if r["status"] != "ok"]
    control_ok = any(r["status"] != "blocked" for r in control)
    if not bad:
        return "open"
    if not control_ok and len(bad) == len(dev):
        return "offline"
    if len(bad) >= max(3, len(dev) // 2):
        return "restricted"
    return "mixed"


def advice_for(dev: list[dict]) -> list[str]:
    bad_hosts = {r["host"] for r in dev if r["status"] != "ok"}
    return [tip for hosts, tip in MIRROR_ADVICE if hosts & bad_hosts]


def main() -> None:
    ap = argparse.ArgumentParser(description="Probe dev-host reachability and suggest mirror routing.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--timeout", type=float, default=4.0, help="per-endpoint timeout, seconds (default 4)")
    args = ap.parse_args()

    dev, mirrors, control = probe_all(args.timeout)
    verdict = verdict_of(dev, control)
    tips = advice_for(dev) if verdict in ("restricted", "mixed") else []
    ecosystems = ecosystem_routes(dev, mirrors)

    if args.json:
        print(json.dumps({"verdict": verdict, "dev": dev, "mirror_endpoints": mirrors,
                          "control": control, "ecosystems": ecosystems,
                          "advice": tips}, ensure_ascii=False, indent=2))
        return

    roles = {h: role for h, _, role in DEV_ENDPOINTS + MIRROR_ENDPOINTS + CONTROL_ENDPOINTS}
    print(f"verdict: {verdict}")
    print("endpoints:")
    for r in dev + mirrors + control:
        lat = f"{r['latency_ms']} ms" if r["latency_ms"] is not None else r["error"]
        print(f"  {r['host']}: {r['status']} ({lat})  # {roles[r['host']]}")
    print("ecosystems:")
    for name, route in ecosystems.items():
        print(f"  {name}: {route}")
    print("advice:  # session-scoped — put on the failing command, don't rewrite global config")
    if tips:
        for t in tips:
            print(f"  - {t}")
    else:
        print("  []  # nothing to re-route" + (" — fix connectivity first" if verdict == "offline" else ""))


if __name__ == "__main__":
    main()
