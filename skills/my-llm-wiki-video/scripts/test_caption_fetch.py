#!/usr/bin/env python3
"""Unit tests for the deterministic caption fetcher.

Run: python3 scripts/test_caption_fetch.py
Covers timestamp parsing, tolerant cue extraction (opencli grouped / Bilibili
body shapes), SRT emission, language preference, and the CLI end-to-end via
--from-json — no network, no opencli/yt-dlp needed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import caption_fetch as cf  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "caption_fetch.py"


def run_cli(payload, tmp: Path):
    src = tmp / "captions.json"
    src.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    out = tmp / "out"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--from-json", str(src), "--out", str(out)],
        capture_output=True, text=True,
    )
    return proc, out


class TestTimestampParse(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(cf.parse_ts("0:32"), 32.0)
        self.assertEqual(cf.parse_ts("1:02:03"), 3723.0)
        self.assertEqual(cf.parse_ts("12.5"), 12.5)
        self.assertEqual(cf.parse_ts(7), 7.0)
        self.assertEqual(cf.parse_ts(1.25), 1.25)
        self.assertIsNone(cf.parse_ts("abc"))
        self.assertIsNone(cf.parse_ts(None))
        self.assertIsNone(cf.parse_ts(-3))


class TestExtractCues(unittest.TestCase):
    def test_opencli_youtube_grouped_shape(self):
        payload = [
            {"timestamp": "0:00", "speaker": "", "text": "辛亥革命成功以後"},
            {"timestamp": "0:32", "speaker": "", "text": "在他辭職的第二天"},
        ]
        self.assertEqual(cf.extract_cues(payload),
                         [(0.0, "辛亥革命成功以後"), (32.0, "在他辭職的第二天")])

    def test_bilibili_body_shape(self):
        payload = {"body": [
            {"from": 1.0, "to": 3.2, "content": "第一句"},
            {"from": 3.2, "to": 6.0, "content": "第二句"},
        ]}
        self.assertEqual(cf.extract_cues(payload), [(1.0, "第一句"), (3.2, "第二句")])

    def test_junk_tolerance(self):
        payload = [
            {"timestamp": "bad", "text": "dropped"},
            "not a dict",
            {"timestamp": "0:05", "text": "  kept\n text "},
            {"timestamp": "0:10", "text": ""},
        ]
        self.assertEqual(cf.extract_cues(payload), [(5.0, "kept text")])

    def test_non_list_payload(self):
        self.assertEqual(cf.extract_cues({"message": "no captions"}), [])


class TestSrtRoundTrip(unittest.TestCase):
    def test_write_then_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "subs.srt"
            cf.write_srt([(0.0, "第一段"), (32.0, "第二段"), (3661.0, "结尾")], dest)
            text = dest.read_text(encoding="utf-8")
            self.assertIn("00:00:00,000 --> 00:00:32,000", text)  # end = next start
            self.assertIn("01:01:01,000 --> 01:01:31,000", text)  # last cue: +30s
            stats = cf.subs_stats(dest)
            self.assertEqual(stats["cues"], 3)
            self.assertEqual(stats["first_anchor"], "0:00")
            self.assertEqual(stats["last_anchor"], "1:01:01")
            self.assertEqual(stats["text_chars"], len("第一段第二段结尾"))


class TestLangPreference(unittest.TestCase):
    def test_prefers_language_then_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            en = Path(tmp) / "subs.en.vtt"
            zh = Path(tmp) / "subs.zh-Hans.vtt"
            en.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n", encoding="utf-8")
            zh.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\n你好\n", encoding="utf-8")
            picked = cf.pick_caption_file([en, zh], ["zh-Hans", "en"])
            self.assertEqual(picked, zh)
            # no preference match -> largest file wins
            big = Path(tmp) / "subs.ja.vtt"
            big.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\n" + "あ" * 100, encoding="utf-8")
            self.assertEqual(cf.pick_caption_file([en, big], ["ko"]), big)


class TestCliEndToEnd(unittest.TestCase):
    def test_from_json_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = [{"timestamp": "0:00", "text": "開場"},
                       {"timestamp": "0:31", "text": "第二段"}]
            proc, out = run_cli(payload, tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads(proc.stdout)
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["cues"], 2)
            subs = Path(summary["subs"])
            self.assertEqual(subs, (out / "subs.srt").resolve())
            self.assertIn("開場", subs.read_text(encoding="utf-8"))
            # compact summary: the 11KB-class payload must stay on disk
            self.assertLess(len(proc.stdout), 2000)

    def test_from_json_no_cues_branches_to_asr(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, _ = run_cli([], Path(tmp))
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(json.loads(proc.stdout)["status"], "no-captions")

    def test_tool_breakage_is_error_not_no_captions(self):
        # A bot wall / broken tool must NOT report "no-captions" (the agent
        # would branch to ASR and hit the same wall downloading audio).
        with tempfile.TemporaryDirectory() as tmp:
            original = cf.try_ytdlp

            def bot_walled(url, out, browser, timeout, warnings, errors, prefs, provider):
                errors.append("yt-dlp captions failed (1): Sign in to confirm")
                return None

            cf.try_ytdlp = bot_walled
            try:
                with self.assertRaises(SystemExit) as ctx:
                    import contextlib
                    import io
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        cf.main(["--url", "https://www.youtube.com/watch?v=x",
                                 "--out", tmp, "--tool", "yt-dlp"])
                self.assertEqual(ctx.exception.code, 1)
                self.assertEqual(json.loads(buf.getvalue())["status"], "error")
            finally:
                cf.try_ytdlp = original

    def test_clean_empty_result_is_no_captions(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = cf.try_ytdlp

            def empty(url, out, browser, timeout, warnings, errors, prefs, provider):
                warnings.append("yt-dlp found no caption tracks")
                return None

            cf.try_ytdlp = empty
            try:
                import contextlib
                import io
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = cf.main(["--url", "https://www.youtube.com/watch?v=x",
                                    "--out", tmp, "--tool", "yt-dlp"])
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(buf.getvalue())["status"], "no-captions")
            finally:
                cf.try_ytdlp = original

    def test_rejects_non_https_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "--url", "ftp://x", "--out", tmp],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(json.loads(proc.stdout)["status"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
