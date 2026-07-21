#!/usr/bin/env python3
"""Regression tests for skill-role resolution and profile-aware tool detection."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from skill_graph import SkillGraphError, resolve_selection  # noqa: E402

sys.path.insert(0, str(ROOT / "skills" / "my-llm-wiki" / "scripts"))
import preflight  # noqa: E402


class SkillGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads(
            (ROOT / "registry" / "skills.json").read_text(encoding="utf-8")
        )

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
            routes={"npm": "cn", "github": "cn", "pypi": "cn", "huggingface": "cn"},
        )
        self.assertEqual(report["capabilities"]["capture.x.single"]["status"], "ok")
        self.assertEqual(report["capabilities"]["capture.x.bookmarks"]["status"], "unavailable")
        self.assertEqual([r["tool"] for r in report["recommendations"]], ["opencli"])
        recommendation = report["recommendations"][0]
        self.assertEqual(recommendation["priority"], "required")
        self.assertEqual(recommendation["install"]["component"], "web")
        self.assertEqual(recommendation["install"]["steps"], [])

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
            routes={"npm": "global", "pypi": "global", "huggingface": "global"},
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
            routes={"npm": "global", "pypi": "global", "huggingface": "global"},
        )
        capability = report["capabilities"]["capture.video"]
        self.assertEqual(capability["status"], "ok")
        self.assertIn("SenseVoice", capability["asr"])
        self.assertIn("sensevoice", {r["tool"] for r in report["recommendations"]})

    def test_doctor_render_shows_asr_routing(self) -> None:
        import doctor

        stub = lambda: {"status": "ok", "detail": "", "root": ""}  # noqa: E731
        report = {
            "state": "ready",
            "overall": "ok",
            "components": {
                "repo_home": stub(),
                "skills": {
                    "status": "ok", "skills": [], "existing_targets": [],
                    "target_scope": "explicit",
                },
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

    def test_doctor_render_lists_every_tool_present_or_missing(self) -> None:
        import doctor

        stub = lambda: {"status": "ok", "detail": "", "root": ""}  # noqa: E731
        report = {
            "state": "ready",
            "overall": "ok",
            "components": {
                "repo_home": stub(),
                "skills": {
                    "status": "ok", "skills": [], "existing_targets": [],
                    "target_scope": "explicit",
                },
                "wiki_registry": stub(),
                "browser": stub(),
                "toolchain": {
                    "status": "warn",
                    "profiles": ["capture.web", "capture.doc"],
                    "tools": {
                        "opencli": {"status": "ok", "path": "/bin/opencli"},
                        "markitdown": {"status": "missing", "path": ""},
                    },
                    "capabilities": {},
                    "recommendations": [],
                },
            },
        }
        rendered = doctor.render_human(report)
        self.assertIn("tools: opencli ✓ · markitdown ✗ missing", rendered)

    def test_doc_profile_uses_cn_variant_when_network_is_restricted(self) -> None:
        report = preflight.build_report(
            ["capture.doc"],
            self.catalog_path,
            tools={"markitdown": ""},
            routes={"pypi": "cn"},
        )
        self.assertEqual(report["status"], "action-required")
        recommendation = report["recommendations"][0]
        self.assertEqual(recommendation["tool"], "markitdown")
        self.assertEqual(recommendation["install"]["component"], "documents")
        self.assertEqual(recommendation["install"]["route"], "protocol-5-plan")

    def test_unknown_profile_fails(self) -> None:
        with self.assertRaises(ValueError):
            preflight.normalize_profiles(self.catalog, ["capture.unknown"])

    def test_linux_ffmpeg_requires_a_new_protocol_5_component_plan(self) -> None:
        ffmpeg = self.catalog["tools"]["ffmpeg"]
        recipe = preflight.install_recipe(ffmpeg, {"system": "global"})
        self.assertEqual(recipe["platform"], "protocol-5")
        self.assertEqual(recipe["route"], "protocol-5-plan")
        self.assertEqual(recipe["component"], "video")
        self.assertEqual(recipe["steps"], [])
        self.assertEqual(recipe["step_timeout_seconds"], 900)
        self.assertEqual(recipe["postcheck_timeout_seconds"], 30)

    def test_ffmpeg_is_owned_by_protocol_5_independent_of_network_probe(self) -> None:
        ffmpeg = self.catalog["tools"]["ffmpeg"]
        open_net = preflight.install_recipe(
            ffmpeg, {"system": "global", "github": "global"}
        )
        restricted = preflight.install_recipe(
            ffmpeg, {"system": "global", "github": "cn"}
        )
        offline = preflight.install_recipe(
            ffmpeg, {"system": "global", "github": "unavailable"}
        )
        for recipe in (open_net, restricted, offline):
            self.assertEqual(recipe["platform"], "protocol-5")
            self.assertEqual(recipe["route"], "protocol-5-plan")
            self.assertEqual(recipe["component"], "video")
            self.assertEqual(recipe["steps"], [])
            self.assertEqual(recipe["postcheck"], [])
            self.assertNotIn("winget", json.dumps(recipe))
            self.assertNotIn("fetch_ffmpeg.py", json.dumps(recipe))

    def test_command_probe_uses_only_receipt_managed_argv(self) -> None:
        spec = {
            "probe": {"kind": "command", "name": "ffmpeg", "extra_paths": []},
            "postcheck": ["ffmpeg", "-version"],
        }
        completed = mock.Mock(returncode=0)
        with mock.patch.object(
            preflight, "resolve_command_argv", return_value=["/managed/python", "runner.py"]
        ), mock.patch.object(preflight.subprocess, "run", return_value=completed) as run:
            found = preflight.command_tool(spec, "ffmpeg")
        self.assertEqual(json.loads(found), ["/managed/python", "runner.py"])
        self.assertEqual(run.call_args.args[0], ["/managed/python", "runner.py", "-version"])

    def test_catalog_rejects_legacy_global_install_recipes(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["tools"]["ffmpeg"]["install"] = {
            "linux": {"global": {"steps": [["apt-get", "install", "ffmpeg"]]}}
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(catalog, handle)
            bad_path = handle.name
        try:
            with self.assertRaises(ValueError):
                preflight.load_catalog(bad_path)
        finally:
            os.unlink(bad_path)

    def test_every_managed_component_has_probe_deadlines(self) -> None:
        for name, spec in self.catalog["tools"].items():
            if "windows_component" not in spec:
                continue
            with self.subTest(tool=name):
                self.assertGreater(spec["step_timeout_seconds"], 0)
                self.assertGreater(spec["postcheck_timeout_seconds"], 0)
                self.assertNotIn("install", spec)

    def test_huggingface_route_is_reported_as_runtime_environment(self) -> None:
        spec = self.catalog["tools"]["faster-whisper"]
        recipe = preflight.install_recipe(
            spec, {"pypi": "global", "huggingface": "cn"}
        )
        self.assertEqual(recipe["env"], {})
        self.assertEqual(
            recipe["runtime_env"], {"HF_ENDPOINT": "https://hf-mirror.com"}
        )
        self.assertEqual(recipe["step_timeout_seconds"], 1200)


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
