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


if __name__ == "__main__":
    unittest.main()
