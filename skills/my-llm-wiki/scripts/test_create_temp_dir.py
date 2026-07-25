#!/usr/bin/env python3
"""Regression tests for native, unique capture workspaces."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path, PureWindowsPath

from create_temp_dir import create_temp_dir, display_path, validate_prefix


class CreateTempDirTests(unittest.TestCase):
    def test_creates_distinct_directories_under_requested_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            first = create_temp_dir("llmwiki-vid-", parent=parent)
            second = create_temp_dir("llmwiki-vid-", parent=parent)

            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())
            self.assertEqual(first.parent, parent.resolve())
            self.assertEqual(second.parent, parent.resolve())
            self.assertNotEqual(first, second)

    def test_windows_display_is_drive_qualified_with_forward_slashes(self) -> None:
        path = PureWindowsPath(
            r"C:\Users\example\AppData\Local\Temp\llmwiki-vid-abcd"
        )
        self.assertEqual(
            display_path(path, os_name="nt"),
            "C:/Users/example/AppData/Local/Temp/llmwiki-vid-abcd",
        )

    def test_posix_display_is_unchanged(self) -> None:
        self.assertEqual(
            display_path("/var/tmp/llmwiki-vid-abcd", os_name="posix"),
            "/var/tmp/llmwiki-vid-abcd",
        )

    def test_prefix_cannot_escape_the_temp_root(self) -> None:
        for prefix in ("", "../llmwiki-", "llmwiki/video", r"llmwiki\video"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                validate_prefix(prefix)


if __name__ == "__main__":
    unittest.main()
