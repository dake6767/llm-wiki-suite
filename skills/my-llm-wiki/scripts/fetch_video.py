#!/usr/bin/env python3
"""Fetch an online video's *content* into the RAW adapter shape — without ever
keeping the video file.

This is a thin fetch adapter (see `references/adapter-contract.md`): it lays an
opencli-style folder down in a temp dir (one `transcript.md` + `images/cover.jpg`)
and prints a YAML summary, then `normalize_raw.py --from <dir>` commits it. The
LLM-shaped steps — light-polishing the transcript and translating a foreign video
into Chinese — are deliberately NOT done here; the agent does those by editing
`transcript.md` before normalizing (SKILL.md). Keep this script deterministic.

Two transcript paths, cheapest first:

  1. Captions (free, no download). YouTube → `opencli youtube transcript`
     (drives the logged-in browser, so it sidesteps yt-dlp's bot wall). Other
     hosts → yt-dlp's own subtitle tracks.
  2. Audio + local Whisper (fallback when a video has no captions). Download
     audio-only via yt-dlp, transcribe with the local `whisper` CLI, then
     **delete the audio** — transcription costs no API money and we never keep
     the media. This is the path the user explicitly wants (local, free, no
     stored video).

The faithful "original" of a video RAW item is its **URL** (always recorded as
`source_url`); the transcript is a lossy text extraction, exactly like a `doc`'s
markitdown text. So the body is the transcript, the link is the archive.

The transcript keeps **per-segment timestamps**: every source above (opencli
captions, bilibili subtitles, yt-dlp VTT, local Whisper via SRT) carries cue
start times, and we render each ~30s chunk with a clickable `[MM:SS](…&t=NNNs)`
deep link to that exact moment. This is what lets the knowledge base answer
"where, in which video, was this said?" — and lets the reader jump straight
there. `--segment-seconds` tunes anchor spacing.

Usage:
  fetch_video.py --url <url> --output <tmpdir>
                 [--whisper-model medium] [--browser chrome]
                 [--lang <caption lang code>] [--keep-audio]

Prints a YAML summary to stdout (status, metadata, transcript_source,
needs_translation, …). On a hard failure (no captions AND audio download failed)
it prints `status: error` with a human-actionable message instead of laying down
a half-empty capture.
"""

from __future__ import annotations

import argparse
import functools
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Tool resolution — the tools are frequently installed but missing from a
# sandboxed agent's PATH (opencli lives in an nvm bin; yt-dlp/whisper/ffmpeg in
# Homebrew). Resolve absolute paths and build an env whose PATH contains all the
# bin dirs, so e.g. opencli can find `node` and `whisper` can find `ffmpeg`.
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


def _tool_env() -> dict:
    env = dict(os.environ)
    extra = [d for d in _candidate_dirs() if Path(d).is_dir()]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def run(cmd: list[str], env: dict, timeout: int | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return 127, "", str(e)


# opencli prints adapter-load warnings (⚠ …) to stdout; strip them before JSON.
def _strip_opencli_noise(s: str) -> str:
    return "\n".join(l for l in s.splitlines() if not l.lstrip().startswith("⚠"))


# ---------------------------------------------------------------------------
# ASR backends (for the no-caption fallback) — pluggable, language-routed.
# Two kinds:
#   * whisper-compatible PATH CLIs (same flags: <audio> --model --output_format
#     srt --output_dir [--language] [--initial_prompt]) — faster-whisper first
#     (runs the SAME Whisper models at a fraction of the time, so a large-v3 pass
#     is affordable), then stock openai-whisper.
#   * SenseVoice (FunASR) — a Python API, not a PATH CLI, wrapped by the bundled
#     scripts/asr_sensevoice.py (same flags + emits a timestamped SRT). It's a
#     much stronger *and* ~15x faster CHINESE model, but weaker than Whisper on
#     English proper nouns, so `--asr auto` routes by language: zh → SenseVoice,
#     else faster-whisper → openai-whisper. (Benchmarks: docs 04 / the wiki.)
# ---------------------------------------------------------------------------

ASR_BACKENDS = [
    ("faster-whisper", "whisper-ctranslate2"),
    ("whisper", "whisper"),
]

SENSEVOICE_SCRIPT = Path(__file__).resolve().parent / "asr_sensevoice.py"


# Conventional home for a dedicated SenseVoice venv, so a user can install funasr
# off to the side (it's a heavy, optional dep) and the skill finds it with no env
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
    return SENSEVOICE_SCRIPT.exists() and bool(_funasr_python())


def resolve_asr(preference: str, prefer_zh: bool = False) -> tuple[str, str]:
    """Return (backend_name, exe_path) for the chosen / best-available ASR, or
    ('', '') if none is installed. `preference` is 'auto' | a backend name.
    For 'sensevoice', exe_path is the bundled wrapper script. With 'auto',
    `prefer_zh` routes Chinese audio to SenseVoice when it's installed."""
    if preference == "sensevoice":
        return ("sensevoice", str(SENSEVOICE_SCRIPT)) if sensevoice_available() else ("", "")
    table = dict(ASR_BACKENDS)
    if preference not in ("", "auto"):
        exe = find_tool(table.get(preference, preference))
        return (preference, exe) if exe else ("", "")
    # auto: Chinese prefers SenseVoice; everything else prefers Whisper (faster-whisper first)
    if prefer_zh and sensevoice_available():
        return "sensevoice", str(SENSEVOICE_SCRIPT)
    for name, exe in ASR_BACKENDS:
        p = find_tool(exe)
        if p:
            return name, p
    return "", ""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def is_youtube(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url, re.I))


def is_bilibili(url: str) -> bool:
    return bool(re.search(r"(bilibili\.com|b23\.tv)", url, re.I))


def extract_bvid(url: str) -> str:
    m = re.search(r"BV[0-9A-Za-z]{10}", url)
    return m.group(0) if m else ""


def fmt_duration(raw) -> str:
    """Normalize assorted duration strings to 'MM:SS' (or 'H:MM:SS').

    Handles: '948s' / 948 (YouTube), '10m20s (620s)' (bilibili), '15:48'."""
    if raw is None:
        return ""
    s = str(raw).strip()
    total = None
    paren = re.search(r"\((\d+)\s*s\)", s)          # bilibili '... (620s)'
    hms = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s.replace(" ", ""))
    if paren:
        total = int(paren.group(1))
    elif ":" in s:
        return s                                     # already H:MM:SS / MM:SS
    elif hms and any(hms.groups()):
        h, m, sec = (int(g or 0) for g in hms.groups())
        total = h * 3600 + m * 60 + sec
    else:
        m = re.search(r"\d+", s)
        total = int(m.group(0)) if m else None
    if total is None:
        return ""
    h, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    return f"{h}:{mm:02d}:{ss:02d}" if h else f"{mm}:{ss:02d}"


def detect_lang(text: str) -> str:
    cjk = len(re.findall(r"[一-鿿]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk == 0 and latin == 0:
        return "unknown"
    return "zh" if cjk >= max(20, latin * 0.3) else "non-zh"


def looks_zh(text: str) -> bool:
    """Lightweight 'is this Chinese?' for SHORT strings (a title) — used to route
    the ASR backend before transcription. detect_lang's >=20-CJK threshold is for
    transcript-length text; a title like '赘婿为什么…' has too few chars for it."""
    cjk = len(re.findall(r"[一-鿿]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return cjk >= 4 and cjk >= latin


# ---------------------------------------------------------------------------
# timestamps — the whole point of a video RAW is "where, in which video, was
# this said?". Every transcript source (opencli captions, bilibili subtitles,
# yt-dlp VTT, local Whisper) actually carries per-segment start times; we keep
# them instead of flattening to prose, and render each chunk with a CLICKABLE
# deep link to that exact moment (`…&t=1450s`). So the maintainer can cite the
# moment and the reader can jump straight to it. A segment is `(start_seconds,
# text)`; `start_seconds` may be None when a source genuinely has no timing.
# ---------------------------------------------------------------------------

Segment = tuple  # (float | None, str)


def ts_to_seconds(v) -> float | None:
    """Parse assorted timestamp forms to float seconds: int/float seconds,
    'SS', 'MM:SS', 'H:MM:SS', with optional '.mmm' or ',mmm' (SRT/VTT)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".")
    if not s:
        return None
    if ":" in s:
        try:
            parts = [float(p) for p in s.split(":")]
        except ValueError:
            return None
        sec = 0.0
        for p in parts:
            sec = sec * 60 + p
        return sec
    try:
        return float(s)
    except ValueError:
        m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s)
        if m and any(m.groups()):
            h, mm, ss = (int(g or 0) for g in m.groups())
            return float(h * 3600 + mm * 60 + ss)
        return None


def secs_to_label(total: float) -> str:
    total = int(total)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _youtube_id(url: str) -> str:
    m = re.search(r"[?&]v=([0-9A-Za-z_-]{11})", url) or re.search(r"youtu\.be/([0-9A-Za-z_-]{11})", url)
    return m.group(1) if m else ""


def build_deeplink(meta: dict, seconds: float) -> str:
    """A URL that opens the video AT this moment. YouTube/Bilibili are honored
    natively; other hosts get a best-effort `t=` param."""
    sec = int(seconds)
    url = meta.get("source_url", "")
    oid = meta.get("original_id", "")
    if is_youtube(url):
        vid = oid if re.fullmatch(r"[0-9A-Za-z_-]{11}", oid or "") else _youtube_id(url)
        if vid:
            return f"https://www.youtube.com/watch?v={vid}&t={sec}s"
    if is_bilibili(url):
        bv = extract_bvid(url) or oid
        if bv:
            return f"https://www.bilibili.com/video/{bv}/?t={sec}"
    if url:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}t={sec}s"
    return ""


def group_segments(segments: list, target_gap: float = 30.0) -> list:
    """Merge fine-grained segments into ~`target_gap`-second chunks so the body
    has a timestamp anchor roughly every half-minute (not one per caption line).
    Drops empties and consecutive exact duplicates (rolling auto-captions)."""
    chunks: list = []
    cur_start = None
    cur: list[str] = []
    last = None
    for sec, text in segments:
        text = (text or "").strip()
        if not text or text == last:
            continue
        last = text
        if cur_start is not None and sec is not None and (sec - cur_start) >= target_gap and cur:
            chunks.append((cur_start, _join_texts(cur)))
            cur, cur_start = [], None
        if cur_start is None:
            cur_start = sec if sec is not None else 0.0
        cur.append(text)
    if cur:
        chunks.append((cur_start, _join_texts(cur)))
    return chunks


def _join_texts(texts: list[str]) -> str:
    joined = " ".join(texts)
    # CJK transcripts read wrong with spaces between cues; join tight when the
    # chunk is Chinese-dominant.
    return "".join(texts) if detect_lang(joined) == "zh" else joined


def segments_plain_text(segments: list) -> str:
    """Anchor-free text, for language detection and char counting."""
    return _join_texts([(t or "").strip() for _, t in segments if (t or "").strip()])


def segments_to_body(segments: list, meta: dict, target_gap: float = 30.0) -> str:
    """Render segments as the transcript body. With timestamps, every chunk is
    prefixed by a bold, clickable `[MM:SS](deeplink)` anchor; without any timing
    it falls back to plain paragraphs (old behavior)."""
    has_ts = any(s is not None for s, _ in segments)
    if not has_ts:
        return "\n\n".join(t.strip() for _, t in segments if t and t.strip())
    out = []
    for start, text in group_segments(segments, target_gap):
        label = secs_to_label(start)
        link = build_deeplink(meta, start)
        out.append(f"**[{label}]({link})** {text}" if link else f"**[{label}]** {text}")
    return "\n\n".join(out)


def parse_cue_segments(text: str) -> list:
    """Parse SRT or VTT cue blocks into `(start_seconds, text)` segments.
    Both formats share the `START --> END` cue line; we take the left side."""
    segs: list = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        ts_idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ts_idx is None:
            continue
        start_raw = lines[ts_idx].split("-->")[0].strip().split()[0]
        sec = ts_to_seconds(start_raw)
        body = " ".join(lines[ts_idx + 1:])
        body = re.sub(r"<[^>]+>", "", body).strip()  # strip inline timing tags
        if body:
            segs.append((sec, body))
    return segs


def _yaml_scalar(v) -> str:
    if v is None:
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v)
    if s == "":
        return '""'
    if re.search(r'[:#\[\]{}",\n]', s) or s.strip() != s or s[0] in set("@`!&*?|>%#,-:[]{}\"'~ "):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


# Set in main() from --status-file. When set, the final YAML summary (success or
# error) is also written here atomically, so an agent that launched this script in
# the background (to escape a short command-timeout, e.g. hermes' 300s terminal
# cap) has ONE file to poll for completion — see SKILL.md §8.
STATUS_FILE: str | None = None


def _yaml_text(d: dict) -> str:
    lines = []
    for k, v in d.items():
        if isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
            else:
                lines.append(f"{k}:")
                for it in v:
                    lines.append(f"  - {_yaml_scalar(it)}")
        else:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    return "\n".join(lines)


def _record_status(d: dict) -> None:
    """Atomically write the final summary to STATUS_FILE (tmp + rename), so a
    poller never reads a half-written file and 'file exists' means 'done'."""
    if not STATUS_FILE:
        return
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(_yaml_text(d) + "\n")
        os.replace(tmp, STATUS_FILE)
    except Exception:
        pass


def emit_yaml(d: dict) -> None:
    print(_yaml_text(d))


def fail(msg: str, **extra) -> None:
    out = {"status": "error", "message": msg}
    out.update(extra)
    emit_yaml(out)
    _record_status(out)
    sys.exit(1)


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------

def youtube_metadata(url: str, opencli: str, env: dict) -> dict:
    rc, out, err = run([opencli, "youtube", "video", url, "-f", "json"], env, timeout=120)
    if rc != 0:
        return {}
    try:
        arr = json.loads(_strip_opencli_noise(out))
    except json.JSONDecodeError:
        return {}
    meta = {}
    if isinstance(arr, list):
        for item in arr:
            if isinstance(item, dict) and "field" in item:
                meta[item["field"]] = item.get("value", "")
    elif isinstance(arr, dict):
        meta = arr
    return {
        "title": meta.get("title", ""),
        "author": meta.get("channel", ""),
        "publish_time": meta.get("publishDate", "") or meta.get("uploadDate", ""),
        "description": meta.get("description", ""),
        "duration": fmt_duration(meta.get("duration")),
        "original_id": meta.get("videoId", ""),
        "thumbnail": meta.get("thumbnail", ""),
        "keywords": meta.get("keywords", ""),
    }


def bilibili_metadata(bvid: str, opencli: str, env: dict) -> dict:
    rc, out, err = run([opencli, "bilibili", "video", bvid, "-f", "json"], env, timeout=120)
    if rc != 0:
        return {}
    try:
        arr = json.loads(_strip_opencli_noise(out))
    except json.JSONDecodeError:
        return {}
    meta = {x["field"]: x.get("value", "") for x in arr
            if isinstance(x, dict) and "field" in x}
    author = re.sub(r"\s*\(mid:[^)]*\)\s*$", "", meta.get("author", "")).strip()
    return {
        "title": meta.get("title", ""),
        "author": author,
        "publish_time": meta.get("publish_time", ""),
        "description": meta.get("description", ""),
        "duration": fmt_duration(meta.get("duration")),
        "original_id": meta.get("bvid", "") or bvid,
        "thumbnail": meta.get("thumbnail", ""),
        "keywords": meta.get("tag", "") or meta.get("keywords", ""),
    }


def ytdlp_metadata(url: str, ytdlp: str, env: dict, browser: str) -> dict:
    cmd = [ytdlp, "--dump-single-json", "--no-playlist", "--skip-download"]
    if browser:
        cmd += ["--cookies-from-browser", browser]
    cmd.append(url)
    rc, out, err = run(cmd, env, timeout=180)
    if rc != 0:
        return {}
    try:
        j = json.loads(out)
    except json.JSONDecodeError:
        return {}
    thumb = j.get("thumbnail", "")
    if not thumb and j.get("thumbnails"):
        thumb = j["thumbnails"][-1].get("url", "")
    upload = j.get("upload_date", "")  # YYYYMMDD
    if re.fullmatch(r"\d{8}", upload or ""):
        upload = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}"
    return {
        "title": j.get("title", ""),
        "author": j.get("uploader", "") or j.get("channel", ""),
        "publish_time": upload,
        "description": j.get("description", ""),
        "duration": fmt_duration(j.get("duration")),
        "original_id": j.get("id", ""),
        "thumbnail": thumb,
        "keywords": ", ".join(j.get("tags") or []),
    }


def build_whisper_prompt(meta: dict, override: str = "") -> str:
    """Bias Whisper toward the video's own vocabulary via `--initial_prompt`.

    Whisper mis-decodes code-switching / domain terms badly on small models —
    a mixed-language tech talk turns 'token' into '偷肯', 'GPT' into '吉皮提'.
    Feeding the video's OWN title + keywords (we already fetched them) as a prompt
    primes the decoder to spell those terms right and pick the right domain. Free,
    and it travels with every video. `override` (the --prompt flag) replaces the
    auto prompt when the caller wants to hand-tune the term list."""
    if override:
        return override.strip()
    parts = []
    title = (meta.get("title") or "").strip()
    if title:
        parts.append(title)
    kw = (meta.get("keywords") or "").strip()
    if kw:
        parts.append("相关术语：" + kw)
    desc = (meta.get("description") or "").strip().splitlines()
    if desc and desc[0].strip():
        parts.append(desc[0].strip())
    prompt = "。".join(parts)
    return prompt[:600]  # keep within Whisper's effective prompt window


# ---------------------------------------------------------------------------
# transcript — captions first, audio+whisper fallback
# ---------------------------------------------------------------------------

def youtube_captions(url: str, opencli: str, env: dict, lang: str) -> list:
    """Return `(start_seconds, text)` segments from opencli's grouped transcript
    (its `timestamp` is an 'M:SS'/'H:MM:SS' string). [] on failure."""
    cmd = [opencli, "youtube", "transcript", url, "--mode", "grouped", "-f", "json"]
    if lang:
        cmd += ["--lang", lang]
    rc, out, err = run(cmd, env, timeout=180)
    if rc != 0:
        return []
    cleaned = _strip_opencli_noise(out).strip()
    if not cleaned:
        return []
    try:
        arr = json.loads(cleaned)
    except json.JSONDecodeError:
        return []
    if not isinstance(arr, list):
        return []
    segs = []
    for seg in arr:
        text = str(seg.get("text", "")).strip()
        if text:
            segs.append((ts_to_seconds(seg.get("timestamp")), text))
    return segs


def bilibili_captions(bvid: str, opencli: str, env: dict, lang: str) -> list:
    """Return `(start_seconds, text)` segments from opencli's bilibili subtitle
    (each cue has `from` seconds + `content`). [] on failure."""
    cmd = [opencli, "bilibili", "subtitle", bvid, "-f", "json"]
    if lang:
        cmd += ["--lang", lang]
    rc, out, err = run(cmd, env, timeout=180)
    if rc != 0:
        return []
    cleaned = _strip_opencli_noise(out).strip()
    if not cleaned:
        return []
    try:
        arr = json.loads(cleaned)
    except json.JSONDecodeError:
        return []
    if not isinstance(arr, list):
        return []
    segs = []
    for seg in arr:
        content = str(seg.get("content", "")).strip()
        if content:
            segs.append((ts_to_seconds(seg.get("from")), content))
    return segs


def ytdlp_subtitles(url: str, ytdlp: str, env: dict, browser: str, outdir: Path) -> list:
    """Best-effort subtitle grab for non-YouTube hosts (or as a generic path).
    Writes a .vtt then parses it into timestamped segments. [] if none."""
    cmd = [ytdlp, "--skip-download", "--no-playlist",
           "--write-subs", "--write-auto-subs", "--sub-format", "vtt",
           "--sub-langs", "all", "-o", str(outdir / "subs.%(ext)s")]
    if browser:
        cmd += ["--cookies-from-browser", browser]
    cmd.append(url)
    run(cmd, env, timeout=180)
    vtts = sorted(outdir.glob("subs*.vtt"))
    if not vtts:
        return []
    segs = parse_cue_segments(vtts[0].read_text(encoding="utf-8", errors="ignore"))
    for f in vtts:
        f.unlink(missing_ok=True)
    return segs


def probe_audio_seconds(path: Path, env: dict) -> float | None:
    """Measure a media file's duration in seconds via ffprobe; None if ffprobe is
    missing or the probe fails. Used to verify a download is complete."""
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        return None
    rc, out, _ = run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                      "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                     env, timeout=60)
    if rc != 0:
        return None
    try:
        return float(out.strip())
    except (ValueError, AttributeError):
        return None


def audio_transcribe(url: str, ytdlp: str, asr_name: str, asr_exe: str, env: dict,
                     browser: str, model: str, lang: str, outdir: Path,
                     keep_audio: bool, warnings: list[str], prompt: str = "",
                     expected_seconds: float | None = None) -> list:
    """Download audio-only, transcribe with the chosen local ASR backend, then
    delete the audio. Backends are whisper-compatible CLIs (openai-whisper or
    faster-whisper's whisper-ctranslate2) — see ASR_BACKENDS / resolve_asr.
    Emits SRT (both backends support it) so we keep per-segment timestamps, and
    returns `(start_seconds, text)` segments ([] on failure)."""
    # try/finally guarantees the audio (and any yt-dlp `.part` / intermediate) is
    # removed on EVERY exit path — download failure, transcription failure, or
    # exception — so a downloaded media file never lingers on disk. We never keep
    # the video at all (only `-f bestaudio` is fetched); this also discards that
    # audio once transcribed. `--keep-audio` (debug) is the only opt-out.
    try:
        audio_base = outdir / "audio"
        cmd = [ytdlp, "-f", "bestaudio", "-x", "--audio-format", "mp3", "--no-playlist",
               "-o", str(audio_base) + ".%(ext)s"]
        if browser:
            cmd += ["--cookies-from-browser", browser]
        cmd.append(url)
        rc, out, err = run(cmd, env, timeout=900)
        audio_file = outdir / "audio.mp3"
        if rc != 0 or not audio_file.exists():
            detail = (err or out).strip().splitlines()
            hint = detail[-1] if detail else "unknown error"
            warnings.append(f"audio download failed: {hint}")
            return []

        # Completeness guard: bilibili/youtube can hand back a short "preview"
        # clip (e.g. 2:30 of a 25:33 video) behind a login/cookie wall, and the
        # download still exits 0. Without this check we'd silently transcribe the
        # fragment and emit a transcript that LOOKS whole (the header shows the
        # real duration from metadata) but is missing most of the video. Compare
        # the actual audio length against the expected duration and fail loudly.
        if expected_seconds and expected_seconds > 0:
            actual = probe_audio_seconds(audio_file, env)
            if actual is not None and actual < expected_seconds * 0.8:
                warnings.append(
                    f"audio download incomplete: got {actual / 60:.1f}min of "
                    f"{expected_seconds / 60:.1f}min expected — likely a preview "
                    f"clip behind a login/cookie wall. Retry with ONLY --browser "
                    f"<your-browser> added so yt-dlp can read your logged-in cookies; "
                    f"keep --asr auto (do NOT switch to --asr whisper — auto already "
                    f"routes Chinese audio to SenseVoice, which is faster and better).")
                return []

        # SenseVoice is a Python script, not a PATH CLI: run it under the
        # funasr-capable interpreter. Whisper backends are executed directly.
        wcmd = ([_funasr_python() or sys.executable, asr_exe] if asr_name == "sensevoice"
                else [asr_exe])
        wcmd += [str(audio_file), "--model", model,
                 "--output_format", "srt", "--output_dir", str(outdir)]
        if lang:
            wcmd += ["--language", lang]
        if prompt:
            # Prime the decoder with the video's own vocabulary so code-switching
            # jargon is spelled right.
            wcmd += ["--initial_prompt", prompt]
            # Carry the prompt across every internal window (keeps terms correct
            # through a long video) — an openai-whisper-only flag.
            if asr_name == "whisper":
                wcmd += ["--carry_initial_prompt", "True"]
        rc, out, err = run(wcmd, env, timeout=7200)
        srt_file = outdir / "audio.srt"
        segs: list = []
        if rc == 0 and srt_file.exists():
            segs = parse_cue_segments(srt_file.read_text(encoding="utf-8", errors="ignore"))
        else:
            detail = (err or out).strip().splitlines()
            warnings.append(f"{asr_name} failed: {detail[-1] if detail else 'unknown error'}")
        return segs
    finally:
        if not keep_audio:
            # audio.* covers the media and the .srt/.txt sidecars Whisper writes.
            for f in glob.glob(str(outdir / "audio.*")):
                Path(f).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# cover + transcript.md
# ---------------------------------------------------------------------------

def download_cover(url: str, images_dir: Path) -> bool:
    if not url:
        return False
    try:
        images_dir.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if not data:
            return False
        (images_dir / "cover.jpg").write_bytes(data)
        return True
    except Exception:
        return False


def write_transcript_md(path: Path, meta: dict, transcript: str,
                        source_label: str, has_cover: bool) -> None:
    parts = [f"# {meta['title']}", ""]
    if meta.get("author"):
        parts.append(f"> 作者: {meta['author']}")
    if meta.get("publish_time"):
        parts.append(f"> 发布时间: {meta['publish_time']}")
    if meta.get("source_url"):
        parts.append(f"> 原文链接: {meta['source_url']}")
    parts += ["", "---", ""]
    if has_cover:
        parts += ["![](images/cover.jpg)", ""]
    dur = meta.get("duration", "")
    parts += [f"*时长 {dur} · 转写来源：{source_label}*", ""]
    desc = (meta.get("description") or "").strip()
    if desc:
        parts += ["## 简介", "", desc, ""]
    parts += ["## 文字转写", "", transcript.strip(), ""]
    path.write_text("\n".join(parts), encoding="utf-8")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _clean_capture_dir(outdir: Path) -> None:
    """Remove a previous capture left in the output dir, with a guard against
    nuking a non-temp path. Refuses obviously dangerous targets (filesystem
    root, the user's home, or any shallow top-level dir) and otherwise wipes
    the dir so each run starts from a clean slate."""
    if not outdir.exists():
        return
    home = Path.home().resolve()
    # parts e.g. ('/', 'tmp', 'llmwiki-vid') -> len 3; refuse anything shallower
    # than 3 parts (root or a top-level dir like /tmp) and the home dir itself.
    if outdir == outdir.anchor or outdir == home or len(outdir.parts) < 3:
        # Too risky to wipe wholesale — only drop our own known products.
        for name in ("transcript.md", "status.yaml"):
            (outdir / name).unlink(missing_ok=True)
        shutil.rmtree(outdir / "images", ignore_errors=True)
        return
    shutil.rmtree(outdir, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch an online video's content into the RAW adapter shape (no video kept).")
    ap.add_argument("--url", required=True, help="video URL (YouTube first-class; others via yt-dlp)")
    ap.add_argument("--output", required=True, help="temp dir to lay the capture into")
    ap.add_argument("--whisper-model", default="medium",
                    help="ASR model for the no-caption fallback (default: medium; "
                         "large-v3 / turbo are more accurate, base/small faster)")
    ap.add_argument("--asr", default="auto",
                    choices=["auto", "whisper", "faster-whisper", "sensevoice"],
                    help="local ASR backend for the no-caption fallback (default: auto "
                         "— routes by language: Chinese → SenseVoice if installed, "
                         "else faster-whisper, else openai-whisper)")
    ap.add_argument("--browser", default="chrome",
                    help="browser to read cookies from for yt-dlp (default: chrome)")
    ap.add_argument("--lang", default="",
                    help="caption / whisper language hint (e.g. en, zh-Hans). Omit to auto-select the original track")
    ap.add_argument("--keep-audio", action="store_true",
                    help="keep the downloaded audio (debug); default deletes it")
    ap.add_argument("--prompt", default="",
                    help="override the Whisper initial_prompt (domain terms to spell "
                         "correctly). Default: auto-built from the video's title + keywords")
    ap.add_argument("--segment-seconds", type=float, default=30.0,
                    help="target spacing (seconds) between timestamp anchors in the "
                         "transcript (default: 30). Smaller = finer-grained jump points")
    ap.add_argument("--status-file", default="",
                    help="also write the final YAML summary (success or error) here, "
                         "atomically. Lets an agent run this in the background (to "
                         "escape a short command timeout) and poll one file for done")
    args = ap.parse_args()

    global STATUS_FILE
    STATUS_FILE = args.status_file or None

    outdir = Path(args.output).expanduser().resolve()
    # Start from a clean capture dir. The output dir is a reusable temp dir
    # (e.g. /tmp/llmwiki-vid), so stale products from a previous run — an old
    # transcript.md / status.yaml / cover — must be wiped first. Otherwise a
    # caller that polls those files before this process finishes can read the
    # *previous* video's capture and silently ingest the wrong source.
    _clean_capture_dir(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    # Also clear a stale status file living outside outdir, so a poller never
    # mistakes the previous run's "status: ok" for this run's completion.
    if STATUS_FILE:
        sf = Path(STATUS_FILE).expanduser().resolve()
        if outdir not in sf.parents:  # outside outdir — not already wiped
            sf.unlink(missing_ok=True)
    env = _tool_env()
    warnings: list[str] = []

    yt = is_youtube(args.url)
    bvid = extract_bvid(args.url) if is_bilibili(args.url) else ""
    opencli = find_tool("opencli")
    ytdlp = find_tool("yt-dlp")

    # --- metadata ---
    meta = {}
    if yt and opencli:
        meta = youtube_metadata(args.url, opencli, env)
    elif bvid and opencli:
        meta = bilibili_metadata(bvid, opencli, env)
    if not meta.get("title") and ytdlp:
        meta = ytdlp_metadata(args.url, ytdlp, env, args.browser) or meta
    if not meta:
        fail("could not read video metadata. For YouTube ensure opencli is installed "
             "(`opencli youtube login` if it hits an auth wall); for other hosts ensure "
             "yt-dlp is installed and you're logged into the browser given by --browser.",
             tmp_dir=str(outdir))
    meta["source_url"] = args.url

    # --- transcript: captions first (segments carry per-cue timestamps) ---
    segments, source_label, src_kind = [], "", ""
    if yt and opencli:
        segments = youtube_captions(args.url, opencli, env, args.lang)
        if segments:
            source_label, src_kind = "官方字幕", "captions"
    elif bvid and opencli:
        segments = bilibili_captions(bvid, opencli, env, args.lang)
        if segments:
            source_label, src_kind = "B站字幕", "captions"
    if not segments and ytdlp:
        sub_segs = ytdlp_subtitles(args.url, ytdlp, env, args.browser, outdir)
        if sub_segs:
            segments, source_label, src_kind = sub_segs, "官方字幕", "captions"

    # --- transcript: audio + local ASR fallback (pluggable, language-routed) ---
    if not segments:
        # Pick the backend BEFORE transcribing, from the language we can see now —
        # an explicit --lang wins, else guess from the title/author. Chinese gets
        # SenseVoice (faster + far better on zh); everything else gets Whisper.
        if args.lang:
            prefer_zh = args.lang.lower().startswith(("zh", "yue"))
        else:
            prefer_zh = looks_zh(f"{meta.get('title', '')} {meta.get('author', '')}")
        asr_name, asr_exe = resolve_asr(args.asr, prefer_zh=prefer_zh)
        if not ytdlp:
            fail("no captions available and yt-dlp is not installed, so the audio "
                 "fallback can't run. Install yt-dlp (brew install yt-dlp).",
                 tmp_dir=str(outdir), title=meta.get("title", ""))
        if not asr_exe:
            fail("no captions available and no local ASR backend is installed, so "
                 "transcription can't run. Install one: `pip install whisper-ctranslate2` "
                 "(faster-whisper — recommended), `brew install openai-whisper`, or "
                 "`pip install funasr torch torchaudio` (SenseVoice — best for Chinese).",
                 tmp_dir=str(outdir), title=meta.get("title", ""))
        wprompt = build_whisper_prompt(meta, args.prompt)
        segments = audio_transcribe(
            args.url, ytdlp, asr_name, asr_exe, env, args.browser,
            args.whisper_model, args.lang, outdir, args.keep_audio, warnings,
            prompt=wprompt,
            expected_seconds=ts_to_seconds(meta.get("duration")),
        )
        if segments:
            model_label = "Small" if asr_name == "sensevoice" else args.whisper_model
            source_label = f"本地 {asr_name}({model_label})"
            src_kind = f"{asr_name}({model_label})"

    if not segments:
        fail("no transcript could be produced (captions empty and audio/whisper "
             "fallback failed). " + (" ".join(warnings) if warnings else "")
             + " If this is an auth/cookie issue, log into the video site in the "
             "browser given by --browser, or run `opencli youtube login`.",
             tmp_dir=str(outdir), title=meta.get("title", ""), warnings=warnings)

    # --- render: timestamped body + anchor-free plain text for stats ---
    body = segments_to_body(segments, meta, args.segment_seconds)
    plain = segments_plain_text(segments)
    has_timestamps = any(s is not None for s, _ in segments)
    if has_timestamps:
        source_label += " · 含可跳转时间戳"

    # --- cover + transcript.md ---
    has_cover = download_cover(meta.get("thumbnail", ""), outdir / "images")
    if not has_cover:
        warnings.append("thumbnail/cover could not be downloaded")

    md_path = outdir / "transcript.md"
    write_transcript_md(md_path, meta, body, source_label, has_cover)

    lang = detect_lang(plain)
    summary = {
        "status": "ok",
        "tmp_dir": str(outdir),
        "transcript_md": str(md_path),
        "title": meta.get("title", ""),
        "author": meta.get("author", ""),
        "publish_time": meta.get("publish_time", ""),
        "source_url": args.url,
        "original_id": meta.get("original_id", ""),
        "duration": meta.get("duration", ""),
        "transcript_source": src_kind,
        "has_timestamps": has_timestamps,
        "segment_count": len(segments),
        "detected_lang": lang,
        "needs_translation": lang == "non-zh",
        "transcript_chars": len(re.sub(r"\s+", "", plain)),
        "has_cover": has_cover,
    }
    if warnings:
        summary["warnings"] = warnings
    emit_yaml(summary)
    _record_status(summary)


if __name__ == "__main__":
    main()
