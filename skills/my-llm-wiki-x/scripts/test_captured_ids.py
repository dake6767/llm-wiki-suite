#!/usr/bin/env python3
"""Tests for batch id listing and single-post RAW lookup."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent / "captured_ids.py"


class CapturedIdsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.wiki = Path(self.temp.name) / "wiki"
        self.raw = self.wiki / "raw" / "sources" / "x"
        self.raw.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_raw(self, rel: str, original_id: str, captured_at: str) -> Path:
        path = self.raw / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join([
                "---",
                f"original_id: {original_id}",
                f'captured_at: "{captured_at}"',
                "---",
                "body",
                "",
            ]),
            encoding="utf-8",
        )
        return path.resolve()

    def run_cli(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--wiki", str(self.wiki), *extra],
            capture_output=True,
            text=True,
        )

    def test_default_mode_lists_unique_ids_including_nested_raw(self) -> None:
        self.write_raw("one.md", "22", "2026-07-08T00:00:00Z")
        self.write_raw("nested/two.md", "11", "2026-07-07T00:00:00Z")
        self.write_raw("duplicate.md", "22", "2026-07-09T00:00:00Z")

        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["11", "22"])

    def test_find_prints_all_matches_oldest_first(self) -> None:
        newer = self.write_raw("decorated.md", "99", "2026-07-09T00:00:00Z")
        older = self.write_raw("canonical.md", "99", "2026-07-07T00:00:00Z")

        proc = self.run_cli("--find", "99")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), [str(older), str(newer)])

    def test_find_absent_returns_one_and_no_paths(self) -> None:
        proc = self.run_cli("--find", "missing")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
