from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preflight


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
