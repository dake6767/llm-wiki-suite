#!/usr/bin/env python3
"""Regression tests for RAW identity and immutable recapture behavior."""

from __future__ import annotations

import hashlib
import json
import os
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


class ImageCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.wiki = self.root / "wiki-root"
        (self.wiki / "wiki").mkdir(parents=True)
        (self.wiki / "schema.md").write_text("# schema\n", encoding="utf-8")
        self.image = self.root / "diagram.png"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"faithful-image-bytes")
        self.capture = self.root / "image.md"
        self.capture.write_text(
            "# 检索链路架构图\n\n"
            "![原图](diagram.png)\n\n"
            "## 可检索文字\n\nQuery → Retriever → Results\n\n"
            "## 画面描述\n\n三个方框由左向右连接。\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_normalize(
        self,
        *,
        title: str = "检索链路架构图",
        source_file: Path | None = None,
        on_exists: str = "skip",
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        command = [
            sys.executable,
            str(SCRIPT),
            "--md",
            str(self.capture),
            "--assets",
            str(self.root),
            "--wiki",
            str(self.wiki),
            "--source-type",
            "image",
            "--title",
            title,
            "--captured-at",
            "2026-07-31T10:00:00Z",
            "--on-exists",
            on_exists,
        ]
        if source_file is not None:
            command.extend(["--source-file", str(source_file)])
        proc = subprocess.run(command, capture_output=True, text=True)
        return proc, parse_summary(proc.stdout)

    def test_image_archives_original_embeds_asset_and_hashes_identity(self) -> None:
        proc, summary = self.run_normalize(source_file=self.image)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(summary["source_type"], "image")
        self.assertEqual(summary["assets"], "1")
        self.assertEqual(summary["capture_health"], "ok")

        raw = Path(summary["dest"])
        text = raw.read_text(encoding="utf-8")
        expected_hash = hashlib.sha256(self.image.read_bytes()).hexdigest()
        asset_name = "2026-07-31-检索链路架构图--diagram.png"
        asset = self.wiki / "raw" / "assets" / asset_name

        self.assertIn("source_type: image", text)
        self.assertIn(f"original_id: {expected_hash}", text)
        self.assertIn("tags:\n  - inbox\n  - image", text)
        self.assertIn(f"source_file: ../../assets/{asset_name}", text)
        self.assertIn(f"![原图](../../assets/{asset_name})", text)
        self.assertIn("## 可检索文字", text)
        self.assertIn("## 画面描述", text)
        self.assertEqual(asset.read_bytes(), self.image.read_bytes())
        self.assertEqual(len(list((self.wiki / "raw" / "assets").iterdir())), 1)

    def test_identical_image_deduplicates_even_when_title_changes(self) -> None:
        first, first_summary = self.run_normalize(source_file=self.image)
        self.assertEqual(first.returncode, 0, first.stderr)
        second, second_summary = self.run_normalize(
            title="另一种标题", source_file=self.image
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second_summary["status"], "skipped_exists")
        self.assertEqual(second_summary["matched_by"], "original_id")
        self.assertEqual(second_summary["dest"], first_summary["dest"])

    def test_image_requires_source_file(self) -> None:
        proc, _ = self.run_normalize()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("requires --source-file", proc.stderr)

    def test_image_requires_exact_source_file_embed(self) -> None:
        self.capture.write_text(
            "# 检索链路架构图\n\n## 可检索文字\n\nQuery → Results\n",
            encoding="utf-8",
        )
        proc, _ = self.run_normalize(source_file=self.image)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("requires the markdown body to embed", proc.stderr)

    def test_image_rejects_non_image_source_file(self) -> None:
        not_image = self.root / "fake.png"
        not_image.write_text("not an image", encoding="utf-8")
        self.capture.write_text(
            "# 检索链路架构图\n\n![原图](fake.png)\n",
            encoding="utf-8",
        )
        proc, _ = self.run_normalize(source_file=not_image)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not a supported image", proc.stderr)

    def test_svg_image_is_supported_by_magic_bytes(self) -> None:
        svg = self.root / "diagram.svg"
        svg.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"><text>Query</text></svg>\n',
            encoding="utf-8",
        )
        self.capture.write_text(
            "# SVG 检索链路\n\n![原图](diagram.svg)\n\n"
            "## 可检索文字\n\nQuery\n\n## 画面描述\n\n一个文本标签。\n",
            encoding="utf-8",
        )
        proc, summary = self.run_normalize(
            title="SVG 检索链路", source_file=svg
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(
            (self.wiki / "raw" / "assets" /
             "2026-07-31-SVG-检索链路--diagram.svg").is_file()
        )
        self.assertEqual(summary["capture_health"], "ok")


class WikiByNameTests(unittest.TestCase):
    """--wiki accepts a registered name, not just a path."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        # resolve(): the script reports resolved destinations, and macOS temp
        # dirs live behind the /var -> /private/var symlink.
        self.root = Path(self.temp.name).resolve()
        self.wiki = self.root / "wikis" / "my-llm-wiki"
        (self.wiki / "wiki").mkdir(parents=True)
        (self.wiki / "schema.md").write_text("# schema\n", encoding="utf-8")
        self.capture = self.root / "capture.md"
        self.capture.write_text(
            "# Adapter title\n\n" + ("Substantial captured body. " * 30),
            encoding="utf-8",
        )
        self.registry = self.root / "wikis.json"
        self.write_registry([{"path": str(self.wiki), "name": "my-llm-wiki"}])
        # Run from a dir that is not a wiki and holds no same-named child, so a
        # name can only resolve through the registry.
        self.cwd = self.root / "elsewhere"
        self.cwd.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_registry(self, entries: list[dict]) -> None:
        self.registry.write_text(
            json.dumps({"version": 1, "wikis": entries}), encoding="utf-8"
        )

    def run_normalize(self, wiki_arg: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable, str(SCRIPT),
                "--md", str(self.capture),
                "--wiki", wiki_arg,
                "--source-type", "web",
                "--source-url", "https://example.com/a",
                "--original-id", "abc123",
                "--title", "Named wiki",
                "--captured-at", "2026-07-20T10:00:00Z",
            ],
            capture_output=True, text=True, cwd=str(self.cwd),
            env={**os.environ, "LLM_WIKI_REGISTRY": str(self.registry)},
        )

    def test_registered_name_resolves_to_its_path(self) -> None:
        proc = self.run_normalize("my-llm-wiki")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = parse_summary(proc.stdout)
        self.assertTrue(Path(summary["dest"]).is_relative_to(self.wiki))

    def test_name_match_is_case_insensitive(self) -> None:
        proc = self.run_normalize("My-LLM-Wiki")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_existing_path_wins_over_a_same_named_registry_entry(self) -> None:
        # A directory named like a registered wiki must never be hijacked.
        decoy = self.cwd / "my-llm-wiki"
        (decoy / "wiki").mkdir(parents=True)
        (decoy / "schema.md").write_text("# schema\n", encoding="utf-8")
        proc = self.run_normalize("my-llm-wiki")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = parse_summary(proc.stdout)
        self.assertTrue(Path(summary["dest"]).is_relative_to(decoy))

    def test_ambiguous_name_is_refused_rather_than_guessed(self) -> None:
        other = self.root / "wikis" / "other"
        (other / "wiki").mkdir(parents=True)
        (other / "schema.md").write_text("# schema\n", encoding="utf-8")
        self.write_registry([
            {"path": str(self.wiki), "name": "dup"},
            {"path": str(other), "name": "dup"},
        ])
        proc = self.run_normalize("dup")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not exist", proc.stderr)

    def test_registered_name_whose_directory_vanished_says_so(self) -> None:
        self.write_registry([{"path": str(self.root / "gone"), "name": "ghost"}])
        proc = self.run_normalize("ghost")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("registered at", proc.stderr)

    def test_unknown_bare_name_lists_the_registered_ones(self) -> None:
        proc = self.run_normalize("typo-wiki")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("my-llm-wiki", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
