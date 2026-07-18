#!/usr/bin/env python3
from __future__ import annotations

import copy
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import build_windows_setup as builder


class BuildWindowsSetupTests(unittest.TestCase):
    def test_committed_lock_is_complete_and_exact(self) -> None:
        lock = builder.load_lock()
        self.assertEqual(
            set(lock["components"]),
            {"documents", "web", "video", "asr-zh", "asr-other"},
        )
        self.assertEqual(lock["build"]["pyinstaller"], "6.21.0")
        self.assertIn("shlex", builder.DYNAMIC_SUITE_STDLIB)

    def test_lock_rejects_unpinned_python_package(self) -> None:
        lock = builder.load_lock()
        modified = copy.deepcopy(lock)
        modified["components"]["asr-other"]["packages"] = ["faster-whisper"]
        with self.assertRaisesRegex(builder.BuildError, "exact == pins"):
            builder.validate_lock(modified)

    def test_lock_rejects_direct_input_without_hash(self) -> None:
        lock = builder.load_lock()
        modified = copy.deepcopy(lock)
        modified["components"]["video"]["yt_dlp"]["sha256"] = "latest"
        with self.assertRaisesRegex(builder.BuildError, r"URL\+SHA256"):
            builder.validate_lock(modified)

    def test_safe_extract_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../../outside", "bad")
            with self.assertRaisesRegex(builder.BuildError, "unsafe zip member"):
                builder.safe_extract(archive, root / "out")


if __name__ == "__main__":
    unittest.main()
