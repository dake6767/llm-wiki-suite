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

    def test_suite_payload_carries_tauri_updater_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "suite.zip"
            builder.build_suite_payload(dest)
            with zipfile.ZipFile(dest) as bundle:
                names = set(bundle.namelist())
        self.assertIn(
            "apps/my-llm-wiki-browser/desktop/src-tauri/tauri.conf.json", names
        )

    def test_executable_hides_owned_console_and_carries_icon(self) -> None:
        self.assertTrue(builder.SETUP_ICON_ICO.is_file())
        self.assertTrue(builder.SETUP_ICON_PNG.is_file())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = root / "dist"
            dist.mkdir()

            def fake_run(argv, **kwargs):
                (dist / "My-LLM-Wiki-Setup.exe").write_bytes(b"exe")
                return mock.Mock(returncode=0)

            with mock.patch.object(subprocess, "run", side_effect=fake_run) as run, \
                    mock.patch.object(builder, "checked_output"):
                builder.build_executable(root / "payload", dist, root / "work", "1.0.0")
            argv = run.call_args.args[0]
            self.assertIn("--console", argv)
            self.assertNotIn("--windowed", argv)
            self.assertIn("--hide-console", argv)
            self.assertIn("hide-early", argv)
            self.assertIn("--icon", argv)
            self.assertIn(str(builder.SETUP_ICON_ICO), argv)

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

    def test_npm_lock_entry_accepts_windows_prefix_relative_key(self) -> None:
        lock = {
            "packages": {
                "..\\..\\stage\\node_modules\\@jackwener\\opencli": {
                    "integrity": "sha512-example"
                }
            }
        }
        self.assertEqual(
            builder.npm_lock_entry(lock, "@jackwener/opencli")["integrity"],
            "sha512-example",
        )


if __name__ == "__main__":
    unittest.main()
