#!/usr/bin/env python3
from __future__ import annotations

import copy
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

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

    def test_lock_rejects_mismatched_pytorch_family(self) -> None:
        lock = builder.load_lock()
        modified = copy.deepcopy(lock)
        packages = modified["components"]["asr-zh"]["packages"]
        packages[packages.index("torchaudio==2.11.0")] = "torchaudio==2.10.0"
        with self.assertRaisesRegex(builder.BuildError, "same reviewed version"):
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

    def test_pip_target_creates_report_parent_before_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            subprocess, "run"
        ) as run:
            root = Path(tmp)
            target = root / "component" / "site-packages"
            report = root / "component" / "pip-report.json"
            builder.pip_target(target, ["example==1.0"], report)
            self.assertTrue(target.is_dir())
            self.assertTrue(report.parent.is_dir())
            argv = run.call_args.args[0]
            self.assertIn(str(report), argv)
            self.assertIn("example==1.0", argv)


if __name__ == "__main__":
    unittest.main()
