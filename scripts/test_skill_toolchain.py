#!/usr/bin/env python3
"""Regression tests for skill-role resolution and profile-aware tool detection."""

from __future__ import annotations

import json
import os
import shutil
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
        with mock.patch.object(preflight.platform, "system", return_value="Darwin"), \
                mock.patch.object(preflight.shutil, "which", return_value="/bin/npm"):
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
        self.assertIn(
            "registry.npmmirror.com",
            json.dumps(recommendation["install"]),
        )

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
        with mock.patch.object(preflight.platform, "system", return_value="Darwin"):
            report = preflight.build_report(
                ["capture.doc"],
                self.catalog_path,
                tools={"markitdown": ""},
                routes={"pypi": "cn"},
            )
        self.assertEqual(report["status"], "action-required")
        recommendation = report["recommendations"][0]
        self.assertEqual(recommendation["tool"], "markitdown")
        self.assertIn(
            "pypi.tuna.tsinghua.edu.cn",
            json.dumps(recommendation["install"]),
        )

    def test_unknown_profile_fails(self) -> None:
        with self.assertRaises(ValueError):
            preflight.normalize_profiles(self.catalog, ["capture.unknown"])

    def test_linux_ffmpeg_recipe_matches_detected_package_manager(self) -> None:
        ffmpeg = self.catalog["tools"]["ffmpeg"]

        def which(name: str):
            return f"/usr/bin/{name}" if name in {"apt-get", "sudo"} else None

        with mock.patch.object(preflight.platform, "system", return_value="Linux"), \
             mock.patch.object(preflight.shutil, "which", side_effect=which), \
             mock.patch.object(preflight.os, "geteuid", return_value=1000, create=True):
            recipe = preflight.install_recipe(ffmpeg, {"system": "global"})
        self.assertEqual(recipe["platform"], "linux-apt")
        self.assertEqual(recipe["steps"][0][:3], ["sudo", "-n", "apt-get"])
        self.assertEqual(recipe["env"], {"DEBIAN_FRONTEND": "noninteractive"})
        self.assertEqual(recipe["step_timeout_seconds"], 900)
        self.assertEqual(recipe["postcheck_timeout_seconds"], 30)

    def test_windows_ffmpeg_is_owned_by_setup_independent_of_network_probe(self) -> None:
        ffmpeg = self.catalog["tools"]["ffmpeg"]
        with mock.patch.object(preflight.platform, "system", return_value="Windows"):
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
            self.assertEqual(recipe["platform"], "windows-setup")
            self.assertEqual(recipe["route"], "windows-setup")
            self.assertEqual(recipe["steps"][0][1:4], ["components", "install", "--component"])
            self.assertEqual(recipe["steps"][0][-1], "video")
            self.assertEqual(recipe["postcheck"][1:4], ["components", "doctor", "--component"])
            self.assertEqual(recipe["postcheck"][-1], "video")
            self.assertNotIn("winget", json.dumps(recipe))
            self.assertNotIn("fetch_ffmpeg.py", json.dumps(recipe))

    def test_command_probe_falls_back_to_declared_extra_paths(self) -> None:
        real_which = shutil.which
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            if os.name == "nt":
                fake = bin_dir / "ffmpeg.bat"
                fake.write_text("@exit /b 0\n", encoding="utf-8")
            else:
                fake = bin_dir / "ffmpeg"
                fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                fake.chmod(0o755)
            spec = {
                "probe": {
                    "kind": "command",
                    "name": "ffmpeg",
                    "extra_paths": [str(bin_dir)],
                },
                "postcheck": ["ffmpeg", "-version"],
            }

            def which(name: str, path: str | None = None):
                return real_which(name, path=path) if path else None

            with mock.patch.object(preflight, "is_windows", return_value=False), \
                    mock.patch.object(preflight.shutil, "which", side_effect=which):
                found = preflight.command_tool(spec, "ffmpeg")
        # Path comparison: Windows which() may report the PATHEXT match with
        # different case (ffmpeg.BAT); WindowsPath equality folds case.
        self.assertEqual(Path(found), fake)

    def test_route_ecosystem_validation_rejects_unknown_keys(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["tools"]["ffmpeg"]["route_ecosystem"] = {"solaris": "github"}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(catalog, handle)
            bad_path = handle.name
        try:
            with self.assertRaises(ValueError):
                preflight.load_catalog(bad_path)
        finally:
            os.unlink(bad_path)

    def test_every_install_recipe_has_noninteractive_deadlines(self) -> None:
        for name, spec in self.catalog["tools"].items():
            if "install" not in spec:
                continue
            with self.subTest(tool=name):
                self.assertGreater(spec["step_timeout_seconds"], 0)
                self.assertGreater(spec["postcheck_timeout_seconds"], 0)
                encoded = json.dumps(spec["install"])
                if '"pip", "install"' in encoded:
                    self.assertIn("--no-input", encoded)
                if '"npm", "install"' in encoded:
                    self.assertIn("--no-audit", encoded)
                    self.assertIn("--no-fund", encoded)
                if '"sudo"' in encoded:
                    self.assertIn('"sudo", "-n"', encoded)
                if '"winget"' in encoded:
                    self.assertIn("--disable-interactivity", encoded)

    def test_huggingface_route_is_reported_as_runtime_environment(self) -> None:
        spec = self.catalog["tools"]["faster-whisper"]
        with mock.patch.object(preflight.platform, "system", return_value="Linux"):
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
