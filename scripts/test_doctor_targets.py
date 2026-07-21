#!/usr/bin/env python3
"""Regression tests for doctor target scoping across multiple agent homes."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "doctor.py"
SPEC = importlib.util.spec_from_file_location("suite_doctor", SCRIPT)
doctor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(doctor)


class DoctorTargetScopeTests(unittest.TestCase):
    def test_auto_scope_uses_canonical_links_not_unrelated_copies(self):
        registry = json.loads((ROOT / "registry" / "skills.json").read_text(encoding="utf-8"))
        slug = "cn-mirrors"
        source = ROOT / "skills" / slug
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            copied_target = root / ".codex" / "skills"
            linked_target = root / ".workbuddy" / "skills"
            (copied_target / slug).mkdir(parents=True)
            linked_target.mkdir(parents=True)
            try:
                (linked_target / slug).symlink_to(source, target_is_directory=True)
            except OSError as exc:  # pragma: no cover - Windows without symlink privileges
                self.skipTest(f"cannot create test symlink: {exc}")

            bootstrap = {"agent_hosts": {
                "codex": {"skills_dir": str(copied_target)},
                "workbuddy": {"skills_dir": str(linked_target)},
            }}
            auto = doctor.check_skills(bootstrap, registry, {slug})
            audited = doctor.check_skills(
                bootstrap,
                registry,
                {slug},
                target_dirs=[str(copied_target), str(linked_target)],
                target_scope="all-configured",
            )

        self.assertEqual(auto["target_scope"], "auto-linked")
        self.assertEqual(auto["status"], "ok")
        self.assertEqual(auto["skills"][0]["targets"], {str(linked_target): "linked"})
        self.assertEqual(audited["target_scope"], "all-configured")
        self.assertEqual(audited["status"], "error")
        self.assertEqual(audited["skills"][0]["targets"][str(copied_target)], "invalid-copy")


class DoctorExpandTests(unittest.TestCase):
    """--custom-target may arrive shell-expanded as an MSYS path from Git Bash."""

    def test_msys_target_follows_the_actual_runner_platform(self):
        expected = (
            Path("C:/Users/x/.workbuddy/skills")
            if os.name == "nt"
            else Path("/c/Users/x/.workbuddy/skills")
        )
        self.assertEqual(doctor.expand("/c/Users/x/.workbuddy/skills"), expected)


class DoctorWikiRegistryTests(unittest.TestCase):
    def test_environment_registry_override_matches_initializer(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "wikis.json"
            registry.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "wikis": [
                            {
                                "path": str(Path(tmp) / "wiki"),
                                "name": "default",
                                "description": "",
                                "default": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"LLM_WIKI_REGISTRY": str(registry)}, clear=False
            ):
                result = doctor.check_wiki(
                    {"wiki_registry_path": str(Path(tmp) / "wrong.json")}
                )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["default"], "default")


class DoctorMcpScopeTests(unittest.TestCase):
    def test_default_report_does_not_check_or_include_mcp(self):
        ok = {"status": "ok", "detail": "ok"}
        skills = {
            "status": "ok",
            "skills": [],
            "existing_targets": [],
            "target_scope": "explicit",
        }
        toolchain = {"status": "skip", "capabilities": {}, "detail": "not selected"}
        with mock.patch.object(doctor, "check_repo_home", return_value=ok), \
             mock.patch.object(doctor, "check_skills", return_value=skills), \
             mock.patch.object(doctor, "check_wiki", return_value=ok), \
             mock.patch.object(doctor, "check_browser", return_value=ok), \
             mock.patch.object(doctor, "check_toolchain", return_value=toolchain), \
             mock.patch.object(doctor, "check_mcp", return_value=ok) as check_mcp:
            report = doctor.build_report(hosts=["codex"])

        self.assertNotIn("mcp", report["components"])
        check_mcp.assert_not_called()

    def test_explicit_mcp_report_includes_check(self):
        ok = {"status": "ok", "detail": "ok"}
        skills = {
            "status": "ok",
            "skills": [],
            "existing_targets": [],
            "target_scope": "explicit",
        }
        toolchain = {"status": "skip", "capabilities": {}, "detail": "not selected"}
        with mock.patch.object(doctor, "check_repo_home", return_value=ok), \
             mock.patch.object(doctor, "check_skills", return_value=skills), \
             mock.patch.object(doctor, "check_wiki", return_value=ok), \
             mock.patch.object(doctor, "check_browser", return_value=ok), \
             mock.patch.object(doctor, "check_toolchain", return_value=toolchain), \
             mock.patch.object(doctor, "check_mcp", return_value=ok) as check_mcp:
            report = doctor.build_report(hosts=["codex"], check_mcp_state=True)

        self.assertEqual(report["components"]["mcp"], ok)
        check_mcp.assert_called_once()


class BrowserRecommendationTests(unittest.TestCase):
    """Browser is one row of the capture-toolchain consent list (AGENTS.md §6)."""

    MISSING = {"status": "warn", "detail": "not reachable", "install": {"ok": False}}

    def _report(self, browser, toolchain=None):
        ok = {"status": "ok", "detail": "ok"}
        skills = {"status": "ok", "skills": [], "existing_targets": [],
                  "target_scope": "explicit"}
        toolchain = toolchain if toolchain is not None else {
            "status": "ok", "capabilities": {}, "recommendations": [],
        }
        repo_home = {"status": "ok", "detail": "ok", "root": str(ROOT)}
        with mock.patch.object(doctor.tool_runtime, "is_windows", return_value=False), \
             mock.patch.object(doctor, "check_repo_home", return_value=repo_home), \
             mock.patch.object(doctor, "check_skills", return_value=skills), \
             mock.patch.object(doctor, "check_wiki", return_value=ok), \
             mock.patch.object(doctor, "check_browser", return_value=browser), \
             mock.patch.object(doctor, "check_toolchain", return_value=toolchain), \
             mock.patch.object(doctor, "check_opencli_extension", return_value=ok):
            return doctor.build_report(hosts=["codex"])

    def _rows(self, report):
        return [r["tool"] for r in report["components"]["toolchain"]["recommendations"]]

    def test_missing_browser_is_recommended_in_the_toolchain_list(self):
        report = self._report(self.MISSING)
        self.assertIn("browser", self._rows(report))
        row = next(r for r in report["components"]["toolchain"]["recommendations"]
                   if r["tool"] == "browser")
        self.assertEqual(row["priority"], "recommended")
        # The recipe must be a directly executable argv, like every other row.
        self.assertIn("--open-web", row["install"]["steps"][0])

    def test_a_receipted_install_produces_no_row(self):
        report = self._report({"status": "warn", "detail": "not running",
                               "install": {"ok": True}})
        self.assertNotIn("browser", self._rows(report))

    def test_a_running_server_without_a_receipt_produces_no_row(self):
        report = self._report({"status": "ok", "detail": "up", "install": {"ok": False}})
        self.assertNotIn("browser", self._rows(report))

    def test_windows_never_shows_the_row_because_setup_owns_that_choice(self):
        with mock.patch.object(doctor.tool_runtime, "is_windows", return_value=True):
            self.assertIsNone(doctor.browser_recommendation(self.MISSING))

    def test_row_survives_a_skipped_capture_probe(self):
        report = self._report(
            self.MISSING,
            toolchain={"status": "skip", "capabilities": {},
                       "detail": "selected skills declare no capture profiles"},
        )
        self.assertIn("browser", self._rows(report))
        # It must also reach the human report, which prints skip on one line.
        self.assertIn("browser [recommended]", doctor.render_human(report))


if __name__ == "__main__":
    unittest.main()
