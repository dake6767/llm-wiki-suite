#!/usr/bin/env python3
"""Regression tests for skill-role resolution and profile-aware tool detection."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from skill_graph import SkillGraphError, resolve_selection  # noqa: E402

sys.path.insert(0, str(ROOT / "skills" / "my-llm-wiki" / "scripts"))
import preflight  # noqa: E402


class SkillGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads((ROOT / "registry" / "skills.json").read_text())

    def test_x_leaf_enables_only_x_profiles(self) -> None:
        result = resolve_selection(self.registry, ["my-llm-wiki-x"])
        roles = {item["slug"]: item["role"] for item in result["skills"]}
        self.assertEqual(roles, {
            "my-llm-wiki": "required",
            "my-llm-wiki-x": "requested",
        })
        self.assertEqual(result["profiles"], [
            "capture.x.single",
            "capture.x.bookmarks",
        ])

    def test_video_leaf_does_not_enable_core_feature_profiles(self) -> None:
        result = resolve_selection(self.registry, ["my-llm-wiki-video"])
        self.assertEqual(result["profiles"], ["capture.video"])
        core = next(item for item in result["skills"] if item["slug"] == "my-llm-wiki")
        self.assertFalse(core["feature_enabled"])

    def test_facade_enables_bundled_profiles(self) -> None:
        result = resolve_selection(self.registry, ["my-llm-wiki"])
        self.assertEqual(set(result["profiles"]), {
            "capture.web",
            "capture.doc",
            "capture.note",
            "capture.video",
            "capture.x.single",
            "capture.x.bookmarks",
        })
        self.assertEqual(set(result["feature_skills"]), {
            "my-llm-wiki",
            "my-llm-wiki-video",
            "my-llm-wiki-x",
        })

    def test_unknown_skill_fails(self) -> None:
        with self.assertRaises(SkillGraphError):
            resolve_selection(self.registry, ["does-not-exist"])


class ToolchainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog_path = ROOT / "skills" / "my-llm-wiki" / "references" / "toolchain.json"
        cls.catalog = preflight.load_catalog(cls.catalog_path)

    def test_x_profiles_never_pull_video_or_doc_tools(self) -> None:
        profiles = ["capture.x.single", "capture.x.bookmarks"]
        names = preflight.profile_tool_names(self.catalog, profiles)
        self.assertEqual(names, ["opencli", "agent-reach"])
        report = preflight.build_report(
            profiles,
            self.catalog_path,
            tools={"opencli": "", "agent-reach": ""},
            github_ok=False,
        )
        self.assertEqual(report["capabilities"]["capture.x.single"]["status"], "ok")
        self.assertEqual(report["capabilities"]["capture.x.bookmarks"]["status"], "unavailable")
        self.assertEqual([r["tool"] for r in report["recommendations"]], ["opencli"])
        recommendation = report["recommendations"][0]
        self.assertEqual(recommendation["priority"], "required")
        self.assertIn("npmmirror.com", recommendation["install"])

    def test_caption_only_video_is_degraded_not_unavailable(self) -> None:
        report = preflight.build_report(
            ["capture.video"],
            self.catalog_path,
            tools={
                "opencli": "/bin/opencli",
                "yt-dlp": "",
                "ffmpeg": "",
                "sensevoice": "",
                "faster-whisper": "",
                "whisper": "",
            },
            github_ok=True,
        )
        capability = report["capabilities"]["capture.video"]
        self.assertEqual(capability["status"], "degraded")
        self.assertEqual(capability["details"]["captioned"]["status"], "ok")
        self.assertEqual(capability["details"]["no_captions"]["status"], "unavailable")
        tools = {r["tool"] for r in report["recommendations"]}
        self.assertNotIn("markitdown", tools)
        self.assertNotIn("opencli", tools)
        self.assertTrue({"yt-dlp", "ffmpeg", "sensevoice", "faster-whisper"} <= tools)

    def test_missing_sensevoice_is_always_surfaced(self) -> None:
        report = preflight.build_report(
            ["capture.video"],
            self.catalog_path,
            tools={
                "opencli": "/bin/opencli",
                "yt-dlp": "/bin/yt-dlp",
                "ffmpeg": "/bin/ffmpeg",
                "sensevoice": "",
                "faster-whisper": "/bin/python",
                "whisper": "",
            },
            github_ok=True,
        )
        capability = report["capabilities"]["capture.video"]
        self.assertEqual(capability["status"], "ok")
        self.assertIn("SenseVoice", capability["asr"])
        self.assertIn("sensevoice", {r["tool"] for r in report["recommendations"]})

    def test_doctor_render_shows_asr_routing(self) -> None:
        import doctor

        stub = lambda: {"status": "ok", "detail": "", "root": ""}  # noqa: E731
        report = {
            "overall": "ok",
            "components": {
                "repo_home": stub(),
                "skills": {"status": "ok", "skills": [], "existing_targets": []},
                "wiki_registry": stub(),
                "browser": stub(),
                "mcp": stub(),
                "toolchain": {
                    "status": "ok",
                    "profiles": ["capture.video"],
                    "capabilities": {
                        "capture.video": {
                            "status": "ok",
                            "via": "captions first, audio/ASR fallback",
                            "asr": "zh→SenseVoice, else faster-whisper",
                        },
                    },
                    "recommendations": [],
                },
            },
        }
        rendered = doctor.render_human(report)
        self.assertIn("asr routing: zh→SenseVoice, else faster-whisper", rendered)

    def test_doc_profile_uses_cn_variant_when_network_is_restricted(self) -> None:
        report = preflight.build_report(
            ["capture.doc"],
            self.catalog_path,
            tools={"markitdown": ""},
            github_ok=False,
        )
        self.assertEqual(report["status"], "warn")
        recommendation = report["recommendations"][0]
        self.assertEqual(recommendation["tool"], "markitdown")
        self.assertIn("pypi.tuna.tsinghua.edu.cn", recommendation["install"])

    def test_unknown_profile_fails(self) -> None:
        with self.assertRaises(ValueError):
            preflight.normalize_profiles(self.catalog, ["capture.unknown"])


class XSinglePostSopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sop = (
            ROOT / "skills" / "my-llm-wiki-x" / "references" / "x-capture-sop.md"
        ).read_text(encoding="utf-8")

    def test_dedupe_precedes_network_fetch(self) -> None:
        dedupe = self.sop.index("## 2. Deduplicate before any network fetch")
        fetch = self.sop.index("opencli web read --url")
        self.assertLess(dedupe, fetch)
        self.assertIn('--find "<tweet-id>"', self.sop)

    def test_opencli_output_is_pinned_to_fresh_temp_root(self) -> None:
        self.assertIn('mktemp -d "${TMPDIR:-/tmp}/llmwiki-x.XXXXXX"', self.sop)
        self.assertIn('--output "$CAPTURE_ROOT"', self.sop)
        self.assertIn('>"$STATUS_FILE"', self.sop)


if __name__ == "__main__":
    unittest.main()
