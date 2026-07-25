from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preflight
import tool_runtime


class PreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = preflight.load_catalog()

    def test_caption_only_video_is_degraded_and_asr_is_on_demand(self) -> None:
        report = preflight.build_report(
            ["capture.video"],
            tools={
                "opencli": {"status": "ok", "provider": "official"},
                "yt-dlp": {"status": "missing"},
                "aria2c": {"status": "missing"},
                "ffmpeg": {"status": "missing"},
                "sensevoice": {"status": "missing"},
                "faster-whisper": {"status": "missing"},
            },
        )
        self.assertEqual(report["capabilities"]["capture.video"]["status"], "degraded")
        packs = {
            item["official_fallback"]["pack"]
            for item in report["recommendations"]
            if item["official_fallback"]
        }
        self.assertEqual(packs, {"toolchain-base", "asr-zh", "asr-other"})

    def test_report_exposes_provider_identity(self) -> None:
        report = preflight.build_report(
            ["capture.doc"],
            tools={
                "markitdown": {
                    "status": "ok",
                    "provider": "my-doc-service",
                    "source": "config:my-doc-service",
                }
            },
        )
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["tools"]["markitdown"]["provider"], "my-doc-service")

    def test_slow_postcheck_is_unverified_rather_than_missing(self) -> None:
        catalog = {
            "version": 4,
            "profiles": {"capture.note": {"tools": ["slowpoke"]}},
            "tools": {"slowpoke": {"kind": "command", "postcheck": ["5"]}},
        }
        resolved = tool_runtime.ResolvedProvider("official", ["/bin/sleep"], {}, "pack:test")
        with mock.patch.object(preflight, "resolve_command", return_value=resolved), \
                mock.patch.dict(preflight.POSTCHECK_TIMEOUT, {"command": 1}):
            tools = preflight.probe(catalog, ["capture.note"])
        self.assertEqual(tools["slowpoke"]["status"], "unverified")
        self.assertEqual(tools["slowpoke"]["source"], "pack:test")
        self.assertIn("timed out", tools["slowpoke"]["error"])

    def test_unverified_asr_still_enables_the_no_caption_path(self) -> None:
        report = preflight.build_report(
            ["capture.video"],
            tools={
                "opencli": {"status": "ok", "provider": "official"},
                "yt-dlp": {"status": "ok", "provider": "official"},
                "aria2c": {"status": "ok", "provider": "official"},
                "ffmpeg": {"status": "ok", "provider": "official"},
                "sensevoice": {
                    "status": "unverified",
                    "provider": "official",
                    "source": "pack:asr-zh",
                },
                "faster-whisper": {"status": "missing"},
            },
        )
        capability = report["capabilities"]["capture.video"]
        self.assertEqual(capability["status"], "ok")
        self.assertEqual(capability["asr"], ["sensevoice"])
        packs = {
            item["official_fallback"]["pack"]
            for item in report["recommendations"]
            if item["official_fallback"]
        }
        self.assertEqual(packs, {"asr-other"})

    def test_missing_document_provider_points_to_official_pack(self) -> None:
        report = preflight.build_report(
            ["capture.doc"], tools={"markitdown": {"status": "missing"}}
        )
        recommendation = report["recommendations"][0]
        self.assertEqual(recommendation["official_fallback"]["command"], [
            "my-llm-wiki", "ensure-pack", "toolchain-base"
        ])


if __name__ == "__main__":
    unittest.main()
