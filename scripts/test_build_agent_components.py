#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import build_agent_components as builder


class AgentComponentBuilderTests(unittest.TestCase):
    def test_lock_has_exact_runtime_and_component_pins(self) -> None:
        lock = json.loads(builder.LOCK.read_text(encoding="utf-8"))
        self.assertEqual(lock["protocol"], 5)
        self.assertRegex(lock["runtime"]["python"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(lock["runtime"]["uv"], r"^\d+\.\d+\.\d+$")
        for component in ("documents", "video", "asr-zh", "asr-other"):
            for package in lock["components"][component]["packages"]:
                self.assertIn("==", package)

    def test_zip_materializes_files_and_preserves_executable_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            executable = source / "tool"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            archive = builder.write_zip(source, root / "pack.zip")
            with zipfile.ZipFile(archive) as bundle:
                info = bundle.getinfo("tool")
                self.assertEqual((info.external_attr >> 16) & 0o111, 0o111)
            self.assertEqual(builder.expanded_zip_size(archive), executable.stat().st_size)

    def test_component_versions_are_content_specific(self) -> None:
        lock = json.loads(builder.LOCK.read_text(encoding="utf-8"))
        self.assertIn("ext.", builder.component_version("web", lock["components"]["web"]))
        self.assertIn("yt-dlp", builder.component_version("video", lock["components"]["video"]))


if __name__ == "__main__":
    unittest.main()
