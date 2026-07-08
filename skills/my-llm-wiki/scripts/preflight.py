#!/usr/bin/env python3
"""Capability probe — what can this machine capture, and with which tools?

The skill ships no fetchers (see references/adapter-contract.md): every capture
scenario is an SOP with recipes per available tool. So on any given machine the
best path per source type depends on what's installed. This script probes the
toolchain once and reports, per source type, the recommended path and whether
it's fully capable, degraded, or unavailable — so the agent picks the best
existing recipe and only asks the user to install something when a source they
actually want has *no* path.

Run it at the start of a capture session (especially the first time on a new
machine):

    python3 <skill>/scripts/preflight.py            # human-readable YAML
    python3 <skill>/scripts/preflight.py --quiet     # same, machine-friendly

It NEVER installs anything — it only reports. Install hints are suggestions for
the user/agent to act on deliberately. `agent-reach doctor --json` (when
agent-reach is installed) complements this with per-platform availability.

Capability model (cheapest viable path always exists for web/x/note; video/doc
can be genuinely blocked):
  web/公众号/小红书 — opencli `web read` (browser, localizes images) → best;
                       else agent-reach / the agent's own WebFetch (text-mostly).
  x               — opencli `twitter` / agent-reach → best; else the fxtwitter
                       fallback (network only).
  video           — captions via opencli or yt-dlp → best; no-caption fallback
                       needs yt-dlp + ffmpeg + a local ASR backend, language-
                       routed: Chinese → SenseVoice (funasr), else
                       faster-whisper/whisper. See references/video-capture-sop.md.
  doc             — markitdown (no browser needed). Blocked without it.
  note            — no tool needed.
"""

from __future__ import annotations

import argparse
import functools
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Tool resolution — tools are frequently installed but missing from a sandboxed
# agent's PATH (npm CLIs in an nvm bin; yt-dlp/ffmpeg in Homebrew; pip CLIs in
# ~/.local/bin). Probe the usual bin dirs, not just PATH.
# ---------------------------------------------------------------------------


def _candidate_dirs() -> list[str]:
    home = str(Path.home())
    dirs = glob.glob(f"{home}/.nvm/versions/node/*/bin")
    dirs += [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        f"{home}/.local/bin",
        f"{home}/Library/Python/3.*/bin",
    ]
    out = []
    for d in dirs:
        out += glob.glob(d) if "*" in d else [d]
    return out


def find_tool(name: str) -> str | None:
    p = shutil.which(name)
    if p:
        return p
    for d in _candidate_dirs():
        cand = Path(d) / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


# Conventional home for a dedicated SenseVoice venv, so a user can install funasr
# off to the side (it's a heavy, optional dep) and the probe finds it with no env
# var: `python3 -m venv <here> && <here>/bin/pip install funasr torch torchaudio`.
SENSEVOICE_VENV_PYTHON = Path.home() / ".local" / "share" / "llm-wiki" / "asr-venv" / "bin" / "python"


@functools.lru_cache(maxsize=1)
def _funasr_python() -> str:
    """An interpreter that can import funasr (SenseVoice's backend), or '' if none.
    SenseVoice may live in a different env than this script's python, so we probe,
    in order: an explicit LLM_WIKI_ASR_PYTHON override, our own interpreter, a plain
    python3, then the conventional dedicated venv (SENSEVOICE_VENV_PYTHON)."""
    cands = [os.environ.get("LLM_WIKI_ASR_PYTHON", ""), sys.executable, "python3",
             str(SENSEVOICE_VENV_PYTHON)]
    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        try:
            r = subprocess.run(
                [c, "-c", "import importlib.util as u,sys; sys.exit(0 if u.find_spec('funasr') else 1)"],
                capture_output=True, timeout=30)
            if r.returncode == 0:
                return c
        except Exception:
            pass
    return ""


def sensevoice_available() -> bool:
    return bool(_funasr_python())


# Where each tool lives — cite these when recommending an install, so the user
# can vet the source instead of blind-running a pip/npm/brew line.
TOOL_HOMES = {
    "opencli": "https://www.npmjs.com/package/@jackwener/opencli",
    "agent-reach": "https://github.com/Panniantong/agent-reach",
    "yt-dlp": "https://github.com/yt-dlp/yt-dlp",
    "ffmpeg": "https://ffmpeg.org",
    "whisper": "https://github.com/openai/whisper",
    "whisper-ctranslate2": "https://github.com/Softcatala/whisper-ctranslate2",
    "sensevoice": "https://github.com/FunAudioLLM/SenseVoice (via FunASR: https://github.com/modelscope/FunASR)",
    "markitdown": "https://github.com/microsoft/markitdown",
}

# Tools we probe. Value = a short note on what it's for.
TOOLS = {
    "opencli": "web/social fetch (real logged-in browser, localizes images)",
    "agent-reach": "multi-platform fetch routing (X/Reddit/YouTube/小红书/…; has its own doctor)",
    "yt-dlp": "video/audio download + subtitles (no browser)",
    "ffmpeg": "audio decode for local transcription",
    "whisper": "openai-whisper ASR (local, free)",
    "whisper-ctranslate2": "faster-whisper ASR (local, faster — afford large-v3)",
    "sensevoice": "SenseVoice/FunASR ASR (local; best & ~15x faster for CHINESE video)",
    "markitdown": "local document → Markdown (PDF/docx/pptx/…)",
}


def _torch_version(py: str) -> str:
    """torch version as reported by the funasr interpreter, '' if unavailable.
    SenseVoice's torch lives in ITS venv — checking with the system python3 (a
    common mistake) falsely reports 'No module named torch'. This probes the
    right interpreter so the report is authoritative."""
    if not py:
        return ""
    try:
        r = subprocess.run([py, "-c", "import torch; print(torch.__version__)"],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def probe() -> dict:
    # Most tools are PATH binaries; SenseVoice is a Python lib (funasr) reachable
    # via a possibly-separate interpreter, so probe it specially. Its "path" is
    # the funasr-capable interpreter (what LLM_WIKI_ASR_PYTHON would point at).
    out = {}
    for name in TOOLS:
        if name == "sensevoice":
            out[name] = _funasr_python()
        else:
            out[name] = find_tool(name) or ""
    # Confirm torch in the funasr interpreter, so the report can tell the agent
    # exactly where SenseVoice's deps live (and not to probe the system python3).
    out["_sensevoice_torch"] = _torch_version(out["sensevoice"])
    return out


def assess(tools: dict) -> tuple[dict, list[str]]:
    """Return (per-source-type capability, recommendations)."""
    have = {k: bool(v) for k, v in tools.items()}
    asr = ("faster-whisper" if have["whisper-ctranslate2"]
           else "whisper" if have["whisper"] else "")
    # ASR is language-routed: Chinese → SenseVoice, else Whisper-family. Either one
    # alone can drive the no-caption audio fallback (SenseVoice only covers zh well).
    asr_any = bool(asr) or have["sensevoice"]
    if asr and have["sensevoice"]:
        asr_desc = f"zh→SenseVoice, else {asr}"
    elif have["sensevoice"]:
        asr_desc = "SenseVoice (Chinese; add whisper for other languages)"
    else:
        asr_desc = asr or "none"
    recs: list[str] = []
    cap: dict = {}

    # web / 公众号 / 小红书 — never blocked (agent always has WebFetch).
    if have["opencli"]:
        cap["web"] = {"status": "ok", "via": "opencli web read",
                      "note": "browser render + auto image localization"}
    elif have["agent-reach"]:
        cap["web"] = {"status": "degraded", "via": "agent-reach (check `agent-reach doctor`)",
                      "note": "text-mostly; localize images yourself for a faithful capture"}
    else:
        cap["web"] = {"status": "degraded", "via": "agent WebFetch / tavily-extract",
                      "note": "text-mostly; images not auto-localized; JS/auth pages weaker"}
        recs.append("install opencli for cleaner web/公众号/小红书 captures with images: "
                    f"`npm install -g @jackwener/opencli` (optional; {TOOL_HOMES['opencli']})")

    # x / twitter — never blocked (fxtwitter needs only network).
    if have["opencli"]:
        cap["x"] = {"status": "ok", "via": "opencli twitter"}
    elif have["agent-reach"]:
        cap["x"] = {"status": "ok", "via": "agent-reach (twitter channel)"}
    else:
        cap["x"] = {"status": "ok", "via": "fxtwitter fallback",
                    "note": "see references/x-fallback-capture.md"}

    # doc — needs markitdown.
    if have["markitdown"]:
        cap["doc"] = {"status": "ok", "via": "markitdown"}
    else:
        cap["doc"] = {"status": "unavailable", "via": "", "install": "pipx install markitdown"}
        recs.append("install markitdown to capture local documents (PDF/docx/…): "
                    f"`pipx install markitdown` ({TOOL_HOMES['markitdown']})")

    # video — captions via opencli/yt-dlp; the no-caption fallback needs
    # yt-dlp + ffmpeg + a local ASR backend (see references/video-capture-sop.md).
    caption_path = have["opencli"] or have["yt-dlp"]
    audio_fallback = have["yt-dlp"] and have["ffmpeg"] and asr_any
    if not caption_path:
        cap["video"] = {"status": "unavailable", "via": "", "asr": asr_desc,
                        "install": "brew install yt-dlp ffmpeg "
                                   "(+ an ASR backend for no-caption videos)"}
        recs.append("install yt-dlp + ffmpeg + an ASR backend to capture video: "
                    f"`brew install yt-dlp ffmpeg` ({TOOL_HOMES['yt-dlp']}) "
                    "+ see the ASR hints below")
    elif audio_fallback:
        via = ("opencli captions + yt-dlp/ASR fallback" if have["opencli"]
               else "yt-dlp subtitles + ASR fallback")
        cap["video"] = {"status": "ok", "via": via, "asr": asr_desc}
    else:
        # Captions reachable but no audio fallback → no-caption videos will fail.
        missing = []
        if not have["yt-dlp"]:
            missing.append("yt-dlp")
        if not have["ffmpeg"]:
            missing.append("ffmpeg")
        if not asr_any:
            missing.append("an ASR backend (funasr for zh / whisper-ctranslate2 / openai-whisper)")
        cap["video"] = {"status": "degraded",
                        "via": ("opencli captions only" if have["opencli"]
                                else "yt-dlp subtitles only"),
                        "asr": asr_desc,
                        "note": "captioned videos work; no-caption videos will fail",
                        "install": "add " + " + ".join(missing)}
        recs.append("for videos without captions, install: " + " + ".join(missing))

    # ASR hints, Chinese-first: this skill's corpora are mostly zh, and SenseVoice
    # is both faster and better there — recommend it before the whisper family.
    if not have["sensevoice"] and caption_path:
        recs.append("recommended for CHINESE video (much faster & more accurate; route "
                    "zh audio here): install SenseVoice into the auto-discovered venv: "
                    "`python3 -m venv ~/.local/share/llm-wiki/asr-venv && "
                    "~/.local/share/llm-wiki/asr-venv/bin/pip install funasr torch torchaudio` "
                    "(or set LLM_WIKI_ASR_PYTHON to any python that has funasr; "
                    f"{TOOL_HOMES['sensevoice']}).")
    if not have["whisper-ctranslate2"] and (have["whisper"] or have["yt-dlp"]):
        recs.append("optional: `pip install whisper-ctranslate2` (faster-whisper) for "
                    "faster, higher-quality NON-Chinese video transcription "
                    f"({TOOL_HOMES['whisper-ctranslate2']})")

    cap["note"] = {"status": "ok", "via": "no tool needed"}
    return cap, recs


def emit(tools: dict, cap: dict, recs: list[str]) -> None:
    def line(k, v, ind=0):
        print("  " * ind + f"{k}: {v}")
    print("tools:")
    for name, note in TOOLS.items():
        path = tools[name]
        if name == "sensevoice" and path:
            tv = tools.get("_sensevoice_torch", "")
            dep = f"funasr+torch {tv} ✓" if tv else "funasr ✓ (torch import FAILED in this interp!)"
            # Tell the agent which interpreter owns SenseVoice's deps, so a torch
            # check uses THIS python — never the system python3 (false 'no torch').
            line(name, f"{path}  # {dep} — check ASR deps with THIS python, not system python3", 1)
        else:
            line(name, f'{path}' if path else f'absent  # {note}', 1)
    print("adapters:")
    for st, info in cap.items():
        print(f"  {st}:")
        for k, v in info.items():
            line(k, v, 2)
    print("recommendations:")
    if recs:
        for r in recs:
            print(f"  - {r}")
    else:
        print("  []  # everything needed is installed")


def main() -> None:
    ap = argparse.ArgumentParser(description="Probe the capture toolchain and report per-source capability.")
    ap.add_argument("--quiet", action="store_true", help="(reserved) same output, no extra chatter")
    ap.parse_args()
    tools = probe()
    cap, recs = assess(tools)
    emit(tools, cap, recs)


if __name__ == "__main__":
    main()
