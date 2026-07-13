#!/usr/bin/env python3
"""Tests for the skill version-signal helpers (doc 21 §2.2/§9)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_skills_version as gen  # noqa: E402
import check_pack_version_bump as bump  # noqa: E402


class GenTests(unittest.TestCase):
    def _registry(self, tmp: Path, **fields) -> Path:
        data = {"version": 3, **fields}
        p = tmp / "skills.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_minimal_payload(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            reg = self._registry(Path(d), pack_version="1.2.3")
            out = gen.build(reg, source_commit=None, released_at="2026-07-13T00:00:00Z")
        self.assertEqual(out["schema"], 1)
        self.assertEqual(out["pack_version"], "1.2.3")
        self.assertEqual(out["released_at"], "2026-07-13T00:00:00Z")
        self.assertNotIn("source_commit", out)  # no valid sha given

    def test_valid_sha_and_notes(self) -> None:
        import tempfile

        sha = "a" * 40
        with tempfile.TemporaryDirectory() as d:
            reg = self._registry(Path(d), pack_version="2.0.0", pack_notes="  hi  ")
            out = gen.build(reg, source_commit=sha, released_at=None)
        self.assertEqual(out["source_commit"], sha)
        self.assertEqual(out["pack_notes"], "hi")

    def test_bad_sha_dropped(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            reg = self._registry(Path(d), pack_version="1.0.0")
            out = gen.build(reg, source_commit="not-a-sha", released_at=None)
        self.assertNotIn("source_commit", out)

    def test_rejects_non_semver(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            reg = self._registry(Path(d), pack_version="latest")
            with self.assertRaises(SystemExit):
                gen.build(reg, source_commit=None, released_at=None)


class BumpTests(unittest.TestCase):
    def test_semver_core(self) -> None:
        self.assertEqual(bump.semver_core("1.2.3"), (1, 2, 3))
        self.assertEqual(bump.semver_core("1.2.3-rc.1"), (1, 2, 3))

    def test_semver_core_rejects(self) -> None:
        with self.assertRaises(SystemExit):
            bump.semver_core("1.2")
        with self.assertRaises(SystemExit):
            bump.semver_core(None)

    def test_distribution_classification(self) -> None:
        self.assertTrue(bump.is_distribution_change("skills/my-llm-wiki/SKILL.md"))
        self.assertTrue(bump.is_distribution_change("registry/skills.json"))
        self.assertFalse(bump.is_distribution_change("docs/readme.md"))
        self.assertFalse(bump.is_distribution_change("apps/x/y.rs"))


if __name__ == "__main__":
    unittest.main()
