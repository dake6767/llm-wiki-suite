#!/usr/bin/env python3
"""Regression tests for resumable, post-normalize-only video cleanup."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPT_DIR = SCRIPT_DIR.parents[1] / "my-llm-wiki" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(CORE_SCRIPT_DIR))

import apply_transcript_repairs as repairs  # noqa: E402
import assemble_transcript as assembler  # noqa: E402
import audio_to_wav  # noqa: E402
import capture_checkpoint  # noqa: E402
import commit_capture  # noqa: E402
from capture_state import CaptureStateError  # noqa: E402


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


normalize_raw = load_module("normalize_raw_transaction", CORE_SCRIPT_DIR / "normalize_raw.py")


URL = "https://www.youtube.com/watch?v=video123"
VIDEO_ID = "video123"
TITLE = "Transaction-safe capture"


def write_wav(
    path: Path, *, channels: int = 1, sample_rate: int = 16_000, frames: int = 400
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\0\0" * channels * frames)


class CaptureFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workdir = self.root / "capture"
        (self.workdir / "images").mkdir(parents=True)
        (self.workdir / "images" / "cover.jpg").write_bytes(b"jpeg fixture")
        (self.workdir / "anchored.md").write_text(
            f"**[0:00]({URL}&t=0s)** first sentence\n\n"
            f"**[0:31]({URL}&t=31s)** second sentence\n",
            encoding="utf-8",
        )
        (self.workdir / "metadata.json").write_text(
            json.dumps(
                {
                    "id": VIDEO_ID,
                    "title": TITLE,
                    "uploader": "Example channel",
                    "upload_date": "20260729",
                    "webpage_url": URL,
                    "duration": 75,
                    "description": "Faithful source description.",
                }
            ),
            encoding="utf-8",
        )
        (self.workdir / "status.yaml").write_text(
            "\n".join(
                (
                    "status: \"ok\"",
                    f"source_url: {json.dumps(URL)}",
                    f"original_id: {json.dumps(VIDEO_ID)}",
                    'transcript_source: "sensevoice(zh)"',
                    "transcript_cues: 2",
                    "transcript_chars: 29",
                    "",
                )
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assemble(self) -> dict:
        return assembler.build_transcript(
            workdir=self.workdir,
            anchored_path=self.workdir / "anchored.md",
            output=self.workdir / "transcript.md",
            metadata_path=self.workdir / "metadata.json",
            source_url=URL,
        )


class AssemblyTests(CaptureFixture):
    def test_atomic_assembly_builds_and_validates_the_complete_shape(self) -> None:
        result = self.assemble()
        self.assertEqual(result["status"], "assembled")
        text = (self.workdir / "transcript.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(f"# {TITLE}\n"))
        self.assertIn("![封面](images/cover.jpg)", text)
        self.assertEqual(result["anchors"], 2)

    def test_existing_valid_transcript_is_reused_without_overwrite(self) -> None:
        self.assemble()
        expected = (self.workdir / "transcript.md").read_bytes()
        (self.workdir / "anchored.md").unlink()
        result = self.assemble()
        self.assertEqual(result["status"], "reused")
        self.assertEqual((self.workdir / "transcript.md").read_bytes(), expected)

    def test_failed_forced_reassembly_preserves_existing_transcript(self) -> None:
        self.assemble()
        transcript = self.workdir / "transcript.md"
        expected = transcript.read_bytes()
        (self.workdir / "anchored.md").write_text("no anchors\n", encoding="utf-8")
        with self.assertRaises(CaptureStateError):
            assembler.build_transcript(
                workdir=self.workdir,
                anchored_path=self.workdir / "anchored.md",
                output=transcript,
                metadata_path=self.workdir / "metadata.json",
                source_url=URL,
                force=True,
            )
        self.assertEqual(transcript.read_bytes(), expected)

    def test_repair_plan_cannot_change_timestamp_anchors(self) -> None:
        self.assemble()
        transcript = self.workdir / "transcript.md"
        expected = transcript.read_bytes()
        plan = self.root / "repairs.json"
        plan.write_text(
            json.dumps(
                [
                    {
                        "old": f"**[0:00]({URL}&t=0s)**",
                        "new": f"**[0:01]({URL}&t=1s)**",
                    }
                ]
            ),
            encoding="utf-8",
        )
        with self.assertRaises(CaptureStateError):
            repairs.apply_repairs(transcript, plan)
        self.assertEqual(transcript.read_bytes(), expected)

    def test_exact_text_repair_is_atomic_and_preserves_anchors(self) -> None:
        self.assemble()
        plan = self.root / "repairs.json"
        plan.write_text(
            json.dumps([{"old": "first sentence", "new": "First sentence."}]),
            encoding="utf-8",
        )
        result = repairs.apply_repairs(self.workdir / "transcript.md", plan)
        self.assertEqual(result["status"], "repaired")
        self.assertEqual(result["anchors"], 2)

    def test_translation_requires_and_appends_the_same_anchors(self) -> None:
        self.assemble()
        translation = self.root / "translation.md"
        translation.write_text(
            f"**[0:00]({URL}&t=0s)** 第一句。\n\n"
            f"**[0:31]({URL}&t=31s)** 第二句。\n",
            encoding="utf-8",
        )
        result = repairs.apply_repairs(
            self.workdir / "transcript.md", translation_file=translation
        )
        self.assertEqual(result["status"], "repaired")
        text = (self.workdir / "transcript.md").read_text(encoding="utf-8")
        self.assertIn("## 中文译文", text)
        self.assertEqual(result["anchors"], 4)


class CheckpointTests(CaptureFixture):
    def test_completed_srt_is_reused_before_audio_or_download(self) -> None:
        (self.workdir / "anchored.md").unlink()
        (self.workdir / "transcript.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nhello\n",
            encoding="utf-8",
        )
        (self.workdir / "audio.m4a").write_bytes(b"audio")
        code, result = capture_checkpoint.inspect_capture(
            self.workdir, source_url=URL, original_id=VIDEO_ID
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["stage"], "asr-ready")

    def test_downloaded_audio_is_reused(self) -> None:
        (self.workdir / "anchored.md").unlink()
        (self.workdir / "status.yaml").unlink()
        (self.workdir / "audio.m4a").write_bytes(b"audio")
        code, result = capture_checkpoint.inspect_capture(self.workdir)
        self.assertEqual(code, 0)
        self.assertEqual(result["stage"], "audio-ready")

    def test_identity_mismatch_refuses_stale_srt(self) -> None:
        (self.workdir / "anchored.md").unlink()
        (self.workdir / "transcript.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nhello\n",
            encoding="utf-8",
        )
        with self.assertRaises(CaptureStateError):
            capture_checkpoint.inspect_capture(
                self.workdir, source_url=URL, original_id="different"
            )


class AudioConversionTests(CaptureFixture):
    def test_valid_existing_mono_wav_is_reused(self) -> None:
        source = self.workdir / "audio.m4a"
        source.write_bytes(b"audio")
        output = self.workdir / "audio.wav"
        write_wav(output)
        with mock.patch.object(
            audio_to_wav, "resolve_command_argv"
        ) as resolver:
            result = audio_to_wav.convert(source, output)
        resolver.assert_not_called()
        self.assertEqual(result["status"], "reused")
        self.assertEqual(result["channels"], 1)
        self.assertEqual(result["sample_rate"], 16_000)

    def test_ffmpeg_conversion_requests_and_validates_mono_16khz(self) -> None:
        source = self.workdir / "audio.m4a"
        source.write_bytes(b"audio")
        output = self.workdir / "audio.wav"

        def fake_run(command, **_kwargs):
            self.assertEqual(command[command.index("-ac") + 1], "1")
            self.assertEqual(command[command.index("-ar") + 1], "16000")
            write_wav(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(
            audio_to_wav, "resolve_command_argv", return_value=["ffmpeg"]
        ), mock.patch.object(audio_to_wav.subprocess, "run", side_effect=fake_run):
            result = audio_to_wav.convert(source, output)
        self.assertEqual(result["status"], "converted")
        self.assertTrue(output.is_file())

    def test_invalid_stereo_wav_is_atomically_replaced(self) -> None:
        source = self.workdir / "audio.m4a"
        source.write_bytes(b"audio")
        output = self.workdir / "audio.wav"
        write_wav(output, channels=2)

        def fake_run(command, **_kwargs):
            write_wav(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(
            audio_to_wav, "resolve_command_argv", return_value=["ffmpeg"]
        ), mock.patch.object(audio_to_wav.subprocess, "run", side_effect=fake_run):
            result = audio_to_wav.convert(source, output)
        self.assertEqual(result["status"], "converted")
        self.assertEqual(result["channels"], 1)

    def test_failed_ffmpeg_keeps_downloaded_audio(self) -> None:
        source = self.workdir / "audio.m4a"
        source.write_bytes(b"audio")
        output = self.workdir / "audio.wav"
        failed = subprocess.CompletedProcess(["ffmpeg"], 1, "", "decode error")
        with mock.patch.object(
            audio_to_wav, "resolve_command_argv", return_value=["ffmpeg"]
        ), mock.patch.object(audio_to_wav.subprocess, "run", return_value=failed):
            with self.assertRaises(CaptureStateError):
                audio_to_wav.convert(source, output)
        self.assertTrue(source.is_file())
        self.assertFalse(output.exists())


class CommitTests(CaptureFixture):
    def setUp(self) -> None:
        super().setUp()
        self.assemble()
        self.audio = self.workdir / "audio.m4a"
        self.audio.write_bytes(b"audio")
        (self.workdir / "audio.wav").write_bytes(b"wav")
        (self.workdir / "transcript.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nhello\n",
            encoding="utf-8",
        )
        self.wiki = self.root / "wiki"
        self.raw = self.wiki / "raw" / "sources" / "video" / "source.md"
        self.asset = self.wiki / "raw" / "assets" / "source--cover.jpg"
        self.asset.parent.mkdir(parents=True)
        self.asset.write_bytes(b"localized cover")

    def write_raw(self, *, warning: bool = False) -> None:
        self.raw.parent.mkdir(parents=True, exist_ok=True)
        health = "capture_health: warn\n" if warning else ""
        self.raw.write_text(
            "---\n"
            f"title: {TITLE}\n"
            "source_type: video\n"
            f"source_url: {URL}\n"
            f"{health}"
            "---\n\n"
            f"# {TITLE}\n\n"
            "![封面](../../assets/source--cover.jpg)\n\n"
            f"**[0:00]({URL}&t=0s)** first sentence\n",
            encoding="utf-8",
        )

    def normalizer_result(self, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
        stdout = (
            "status: ingested\n"
            f"dest: {json.dumps(str(self.raw))}\n"
            "capture_health: ok\n"
        )
        return subprocess.CompletedProcess(
            ["normalize_raw.py"], returncode, stdout if not returncode else "", "boom"
        )

    def run_commit(self) -> tuple[int, dict]:
        return commit_capture.commit(
            workdir=self.workdir,
            wiki=str(self.wiki),
            title=TITLE,
            source_url=URL,
            original_id=VIDEO_ID,
            normalizer=CORE_SCRIPT_DIR / "normalize_raw.py",
        )

    def test_normalize_failure_retains_every_recovery_input(self) -> None:
        with mock.patch.object(
            commit_capture.subprocess,
            "run",
            return_value=self.normalizer_result(returncode=1),
        ):
            with self.assertRaises(CaptureStateError):
                self.run_commit()
        for name in ("audio.m4a", "audio.wav", "transcript.srt", "anchored.md"):
            self.assertTrue((self.workdir / name).exists(), name)
        self.assertFalse((self.workdir / ".capture-commit.json").exists())

    def test_success_verifies_raw_then_cleans_exact_intermediates(self) -> None:
        self.write_raw()
        with mock.patch.object(
            commit_capture.subprocess, "run", return_value=self.normalizer_result()
        ) as normalizer:
            code, result = self.run_commit()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "committed")
        normalizer.assert_called_once()
        for name in ("audio.m4a", "audio.wav", "transcript.srt", "anchored.md"):
            self.assertFalse((self.workdir / name).exists(), name)
        self.assertTrue((self.workdir / "transcript.md").is_file())
        self.assertTrue((self.workdir / "images" / "cover.jpg").is_file())

    def test_commit_checkpoint_prevents_a_second_normalize(self) -> None:
        self.write_raw()
        with mock.patch.object(
            commit_capture.subprocess, "run", return_value=self.normalizer_result()
        ):
            first_code, _ = self.run_commit()
        self.assertEqual(first_code, 0)
        with mock.patch.object(commit_capture.subprocess, "run") as normalizer:
            second_code, result = self.run_commit()
        normalizer.assert_not_called()
        self.assertEqual(second_code, 0)
        self.assertEqual(result["status"], "reused")

    def test_capture_warning_retains_audio_and_srt(self) -> None:
        self.write_raw(warning=True)
        with mock.patch.object(
            commit_capture.subprocess, "run", return_value=self.normalizer_result()
        ):
            code, result = self.run_commit()
        self.assertEqual(code, 3)
        self.assertEqual(result["cleanup"], "retained")
        self.assertTrue(self.audio.is_file())
        self.assertTrue((self.workdir / "transcript.srt").is_file())

    def test_cleanup_error_propagates_after_normalize(self) -> None:
        self.write_raw()
        (self.workdir / "audio.bad").mkdir()
        with mock.patch.object(
            commit_capture.subprocess, "run", return_value=self.normalizer_result()
        ):
            code, result = self.run_commit()
        self.assertEqual(code, 4)
        self.assertEqual(result["status"], "cleanup-error")
        checkpoint = json.loads(
            (self.workdir / ".capture-commit.json").read_text(encoding="utf-8")
        )
        self.assertEqual(checkpoint["status"], "cleanup-error")

    def test_real_normalizer_commits_before_cleanup(self) -> None:
        (self.wiki / "wiki").mkdir(parents=True)
        (self.wiki / "schema.md").write_text("# Schema\n", encoding="utf-8")
        code, result = self.run_commit()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "committed")
        raw = Path(result["raw_dest"])
        self.assertTrue(raw.is_file())
        self.assertIn("source_type: video", raw.read_text(encoding="utf-8"))
        self.assertFalse(self.audio.exists())
        self.assertFalse((self.workdir / "transcript.srt").exists())


class CoreAtomicWriteTests(unittest.TestCase):
    def test_failed_raw_replace_never_clobbers_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "raw.md"
            destination.write_text("existing RAW\n", encoding="utf-8")
            with mock.patch.object(
                normalize_raw.os, "replace", side_effect=OSError("disk failure")
            ):
                with self.assertRaises(OSError):
                    normalize_raw.atomic_write_text(destination, "partial replacement\n")
            self.assertEqual(
                destination.read_text(encoding="utf-8"), "existing RAW\n"
            )


class DocumentationContractTests(unittest.TestCase):
    def test_video_docs_never_restore_precommit_cleanup(self) -> None:
        skill_root = SCRIPT_DIR.parent
        documents = [skill_root / "SKILL.md", *sorted((skill_root / "references").glob("*.md"))]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in documents)
        self.assertNotIn("audio is deleted after transcription", combined.lower())
        self.assertNotIn("delete the audio when done", combined.lower())
        self.assertNotIn("**delete the mp4**", combined.lower())

    def test_skill_routes_normalize_and_cleanup_through_transaction_wrapper(self) -> None:
        text = (SCRIPT_DIR.parent / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("scripts/commit_capture.py", text)
        self.assertIn("capture_health: warn", text)
        self.assertIn("retains every recovery input", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
