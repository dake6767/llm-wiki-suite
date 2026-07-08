#!/usr/bin/env python3
"""Capability probe — what can this machine capture, and with which adapter?

The skill is adapter-agnostic (see references/adapter-contract.md) and opencli is
only the *default* adapter. So on any given machine the best available path per
source type depends on what's installed. This script probes the toolchain once and
reports, per source type, the recommended adapter and whether it's fully capable,
degraded, or unavailable — so the agent picks the best existing path and only asks
the user to install something when a source they actually want has *no* path.

Run it at the start of a capture session (especially the first time on a new
machine, or when distributing the skill to someone who may not have opencli):

    python3 <skill>/scripts/preflight.py            # human-readable YAML
    python3 <skill>/scripts/preflight.py --quiet     # same, machine-friendly

It NEVER installs anything — it only reports. Install hints are suggestions for
the user/agent to act on deliberately.

Capability model (cheapest viable path always exists for web/x/note; video/doc
can be genuinely blocked):
  web/公众号/小红书 — opencli `web read` (browser, localizes images) → best;
                       else the agent's own tavily-extract / WebFetch (text-mostly).
  x               — opencli `twitter` → best; else the fxtwitter fallback (network only).
  video           — opencli captions + yt-dlp → best; minimal = yt-dlp + ffmpeg +
                       an ASR backend. The no-caption ASR is language-routed:
                       Chinese → SenseVoice (funasr), else faster-whisper/whisper.
                       Degraded if only captions are reachable (no-caption videos
                       would fail).
  doc             — markitdown (no opencli needed). Blocked without it.
  note            — no tool needed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_video import (  # noqa: E402  (sibling module — shared resolvers)
    find_tool, sensevoice_available, _funasr_python,
)

# Tools we probe. Value = a short note on what it's for.
TOOLS = {
    "opencli": "default web/social/video adapter (real browser)",
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
            out[name] = _funasr_python() if sensevoice_available() else ""
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
    else:
        cap["web"] = {"status": "degraded", "via": "agent tavily-extract / WebFetch",
                      "note": "text-mostly; images not auto-localized; JS/auth pages weaker"}
        recs.append("install opencli for cleaner web/公众号/小红书 captures with images: "
                    "`npm install -g @jackwener/opencli` (optional)")

    # x / twitter — never blocked (fxtwitter needs only network).
    cap["x"] = ({"status": "ok", "via": "opencli twitter"} if have["opencli"]
                else {"status": "ok", "via": "fxtwitter fallback",
                      "note": "see references/x-fallback-capture.md"})

    # doc — needs markitdown.
    if have["markitdown"]:
        cap["doc"] = {"status": "ok", "via": "markitdown"}
    else:
        cap["doc"] = {"status": "unavailable", "via": "", "install": "pipx install markitdown"}
        recs.append("install markitdown to capture local documents (PDF/docx/…): "
                    "`pipx install markitdown`")

    # video — opencli captions and/or yt-dlp; audio fallback needs yt-dlp+ffmpeg+ASR.
    caption_path = have["opencli"] or have["yt-dlp"]
    audio_fallback = have["yt-dlp"] and have["ffmpeg"] and asr_any
    if not caption_path:
        cap["video"] = {"status": "unavailable", "via": "", "asr": asr_desc,
                        "install": "brew install yt-dlp ffmpeg openai-whisper "
                                   "(or `npm i -g @jackwener/opencli` for captions)"}
        recs.append("install yt-dlp + ffmpeg + an ASR backend to capture video: "
                    "`brew install yt-dlp ffmpeg openai-whisper`")
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
            missing.append("an ASR backend (whisper-ctranslate2 / openai-whisper / funasr)")
        cap["video"] = {"status": "degraded",
                        "via": ("opencli captions only" if have["opencli"]
                                else "yt-dlp subtitles only"),
                        "asr": asr_desc,
                        "note": "captioned videos work; no-caption videos will fail",
                        "install": "add " + " + ".join(missing)}
        recs.append("for videos without captions, install: " + " + ".join(missing)
                    + " (e.g. `brew install yt-dlp ffmpeg openai-whisper`)")

    if not have["whisper-ctranslate2"] and (have["whisper"] or have["yt-dlp"]):
        recs.append("optional: `pip install whisper-ctranslate2` (faster-whisper) for "
                    "faster, higher-quality NON-Chinese video transcription (--asr auto picks it up)")
    if not have["sensevoice"] and caption_path:
        recs.append("optional: SenseVoice for CHINESE video (much faster & more accurate; "
                    "--asr auto routes zh→SenseVoice). Install into the auto-discovered venv: "
                    "`python3 -m venv ~/.local/share/llm-wiki/asr-venv && "
                    "~/.local/share/llm-wiki/asr-venv/bin/pip install funasr torch torchaudio` "
                    "(or set LLM_WIKI_ASR_PYTHON to any python that has funasr).")

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
