#!/usr/bin/env python3
"""SenseVoice (FunASR) as a whisper-compatible ASR backend for fetch_video.py.

The video pipeline calls every ASR backend the same way —
    <exe> <audio> --model <m> --output_format srt --output_dir <dir> [--language L] [--initial_prompt P]
— and then parses `<dir>/<audiostem>.srt` for per-segment timestamps (the deep-link
index that makes a video RAW answer "where, in which video, was this said?"). So this
wrapper's whole job is: **take an audio file, write a timestamped `<stem>.srt`.**

SenseVoiceSmall is non-autoregressive and has no built-in timestamps, so we get them
from VAD: run `fsmn-vad` to split speech into <=30s segments, recognise each segment
with SenseVoice, and emit one SRT cue per segment (cue time = VAD segment bounds).
SenseVoice also injects emotion/event emoji (😊🎵) — we strip them for a clean RAW.

`--model` and `--initial_prompt` are accepted and ignored (SenseVoiceSmall is the only
model; it takes no decoder prompt). Why a separate script instead of an entry in
ASR_BACKENDS: SenseVoice is a Python API, not a PATH CLI, and may live in a different
interpreter — `LLM_WIKI_ASR_PYTHON` (or whichever python imports funasr) runs it.
Device via `LLM_WIKI_ASR_DEVICE` (cpu | mps; default cpu — CPU is already ~28x realtime).
"""
import argparse
import os
import re
import sys
from pathlib import Path

# Re-exec into a funasr-capable interpreter if this one can't import it but a
# designated one can. Keeps the script runnable standalone (and under whatever
# python fetch_video.py picked) without the caller threading interpreters through.
import importlib.util as _ilu
if _ilu.find_spec("funasr") is None:
    alt = os.environ.get("LLM_WIKI_ASR_PYTHON")
    # A venv and its base python can share one binary (same realpath) yet have
    # different site-packages, so guard the re-exec with a sentinel, not a path
    # comparison — that both lets us switch envs and prevents an exec loop.
    if alt and Path(alt).exists() and not os.environ.get("_LLM_WIKI_ASR_REEXEC"):
        os.environ["_LLM_WIKI_ASR_REEXEC"] = "1"
        os.execv(alt, [alt, os.path.realpath(__file__), *sys.argv[1:]])
    sys.stderr.write(
        "sensevoice: funasr is not importable in this Python. Install it "
        "(`pip install funasr torch torchaudio`) or point LLM_WIKI_ASR_PYTHON "
        "at an interpreter that has it.\n")
    sys.exit(3)

_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF☀-➿️]")


def _lang(raw: str) -> str:
    """Map a whisper-style language hint to SenseVoice's set (zh/en/yue/ja/ko/auto)."""
    r = (raw or "").strip().lower()
    if not r:
        return "auto"
    if r.startswith("zh") or r in ("chinese", "mandarin"):
        return "zh"
    if r.startswith("yue") or r in ("cantonese",):
        return "yue"
    if r.startswith("en"):
        return "en"
    if r.startswith("ja"):
        return "ja"
    if r.startswith("ko"):
        return "ko"
    return "auto"


def _srt_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main() -> None:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("audio")
    ap.add_argument("--model", default="")           # accepted, ignored
    ap.add_argument("--initial_prompt", default="")  # accepted, ignored
    ap.add_argument("--output_format", default="srt")
    ap.add_argument("--output_dir", default=".")
    ap.add_argument("--language", default="")
    # tolerate any extra whisper-style flags the caller may pass
    args, _unknown = ap.parse_known_args()

    audio = Path(args.audio).expanduser().resolve()
    outdir = Path(args.output_dir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    srt_path = outdir / (audio.stem + ".srt")
    lang = _lang(args.language)
    device = os.environ.get("LLM_WIKI_ASR_DEVICE", "cpu")

    import librosa
    from funasr import AutoModel
    from funasr.utils.postprocess_utils import rich_transcription_postprocess

    # 16k mono float — librosa reads mp3/m4a/wav and resamples, so it matches the
    # pipeline's bestaudio mp3 as well as a pre-made wav.
    wav, _sr = librosa.load(str(audio), sr=16000, mono=True)

    # VAD → speech segments (ms), each capped at 30s so SenseVoice stays in its sweet spot.
    vad = AutoModel(model="fsmn-vad", max_single_segment_time=30000,
                    device=device, disable_update=True)
    vad_res = vad.generate(input=str(audio))
    spans = vad_res[0].get("value") or [] if vad_res else []

    sv = AutoModel(model="iic/SenseVoiceSmall", device=device, disable_update=True)

    def recognise(chunk) -> str:
        res = sv.generate(input=chunk, cache={}, language=lang, use_itn=True)
        if not res:
            return ""
        return _EMOJI.sub("", rich_transcription_postprocess(res[0]["text"])).strip()

    cues: list[tuple[float, float, str]] = []
    if spans:
        for s_ms, e_ms in spans:
            a, b = int(s_ms / 1000 * 16000), int(e_ms / 1000 * 16000)
            chunk = wav[a:b]
            if len(chunk) < 160:  # < 10ms, skip
                continue
            text = recognise(chunk)
            if text:
                cues.append((s_ms / 1000.0, e_ms / 1000.0, text))
    else:
        # VAD found nothing (or is unavailable) — recognise the whole clip as one cue.
        text = recognise(wav)
        if text:
            cues.append((0.0, len(wav) / 16000.0, text))

    if not cues:
        sys.stderr.write("sensevoice: produced no text\n")
        sys.exit(1)

    lines = []
    for i, (start, end, text) in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{_srt_ts(start)} --> {_srt_ts(end)}")
        lines.append(text)
        lines.append("")
    srt_path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
