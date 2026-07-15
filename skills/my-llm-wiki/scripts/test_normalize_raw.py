#!/usr/bin/env python3
"""Regression tests for RAW identity and immutable recapture behavior."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent / "normalize_raw.py"


def parse_summary(text: str) -> dict[str, str]:
    summary: dict[str, str] = {}
    for line in text.splitlines():
        if ": " in line and not line.startswith(" "):
            key, value = line.split(": ", 1)
            summary[key] = value.strip().strip('"')
    return summary


class NormalizeRawIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.wiki = self.root / "wiki-root"
        (self.wiki / "wiki").mkdir(parents=True)
        (self.wiki / "schema.md").write_text("# schema\n", encoding="utf-8")
        self.capture = self.root / "capture.md"
        self.capture.write_text(
            "# Adapter title\n\n" + ("Substantial captured body. " * 30),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_normalize(
        self,
        *,
        title: str,
        original_id: str = "2074515412272996467",
        captured_at: str = "2026-07-07T10:00:00Z",
        on_exists: str = "skip",
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--md",
                str(self.capture),
                "--wiki",
                str(self.wiki),
                "--source-type",
                "x",
                "--source-url",
                f"https://x.com/example/status/{original_id}",
                "--original-id",
                original_id,
                "--title",
                title,
                "--captured-at",
                captured_at,
                "--on-exists",
                on_exists,
            ],
            capture_output=True,
            text=True,
        )
        return proc, parse_summary(proc.stdout)

    def test_same_original_id_skips_even_when_title_slug_changes(self) -> None:
        first, first_summary = self.run_normalize(title="Canonical title")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first_summary["status"], "ingested")

        second, second_summary = self.run_normalize(
            title='(1) Example on X: "Canonical title" / X',
            captured_at="2026-07-08T10:00:00Z",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second_summary["status"], "skipped_exists")
        self.assertEqual(second_summary["matched_by"], "original_id")
        self.assertEqual(second_summary["dest"], first_summary["dest"])
        self.assertEqual(
            len(list((self.wiki / "raw" / "sources" / "x").glob("*.md"))),
            1,
        )

    def test_version_uses_canonical_filename_family_after_slug_drift(self) -> None:
        first, first_summary = self.run_normalize(title="Canonical title")
        self.assertEqual(first.returncode, 0, first.stderr)
        canonical = Path(first_summary["dest"])

        second, second_summary = self.run_normalize(
            title="Decorated replacement title",
            captured_at="2026-07-08T10:00:00Z",
            on_exists="version",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second_summary["status"], "ingested")
        self.assertEqual(
            Path(second_summary["dest"]).name,
            f"{canonical.stem}-v2.md",
        )

    def test_legacy_duplicate_set_returns_oldest_canonical_raw(self) -> None:
        first, first_summary = self.run_normalize(title="Canonical title")
        self.assertEqual(first.returncode, 0, first.stderr)
        canonical = Path(first_summary["dest"])
        duplicate = canonical.with_name("decorated-duplicate.md")
        duplicate.write_text(
            canonical.read_text(encoding="utf-8").replace(
                "2026-07-07T10:00:00Z",
                "2026-07-09T10:00:00Z",
            ),
            encoding="utf-8",
        )

        third, third_summary = self.run_normalize(
            title="A third title",
            captured_at="2026-07-10T10:00:00Z",
        )
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(third_summary["status"], "skipped_exists")
        self.assertEqual(third_summary["dest"], str(canonical))

    def test_same_slug_with_different_ids_preserves_both_sources(self) -> None:
        first, _ = self.run_normalize(title="Shared title", original_id="111111111")
        self.assertEqual(first.returncode, 0, first.stderr)
        second, second_summary = self.run_normalize(
            title="Shared title",
            original_id="222222222",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(Path(second_summary["dest"]).name.endswith("22222222.md"))
        self.assertEqual(
            len(list((self.wiki / "raw" / "sources" / "x").glob("*.md"))),
            2,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
