#!/usr/bin/env python3
"""Regression tests for native-Windows CLI path normalization."""

import unittest

from path_compat import native_path_text, reject_ambiguous_windows_temp_path


class NativePathTextTests(unittest.TestCase):
    def test_translates_msys_drive_path_on_windows(self):
        self.assertEqual(
            native_path_text("/c/Users/simplelife67/wikis/demo", os_name="nt"),
            "C:/Users/simplelife67/wikis/demo",
        )

    def test_translates_cygdrive_path_on_windows(self):
        self.assertEqual(
            native_path_text("/cygdrive/c/Users/simplelife67/wikis/demo", os_name="nt"),
            "C:/Users/simplelife67/wikis/demo",
        )

    def test_preserves_native_windows_path(self):
        value = "C:/Users/simplelife67/wikis/demo"
        self.assertEqual(native_path_text(value, os_name="nt"), value)

    def test_preserves_posix_path_off_windows(self):
        value = "/c/Users/simplelife67/wikis/demo"
        self.assertEqual(native_path_text(value, os_name="posix"), value)

    def test_rejects_git_bash_tmp_path_on_windows(self):
        with self.assertRaisesRegex(ValueError, "create_temp_dir.py"):
            native_path_text("/tmp/llmwiki-vid-old", os_name="nt")

    def test_rejects_git_bash_var_tmp_path_on_windows(self):
        with self.assertRaisesRegex(ValueError, "ambiguous Git Bash temp path"):
            reject_ambiguous_windows_temp_path(
                "/var/tmp/llmwiki-vid-old", os_name="nt"
            )

    def test_allows_native_temp_path_on_windows(self):
        value = "C:/Users/simplelife67/AppData/Local/Temp/llmwiki-vid-new"
        reject_ambiguous_windows_temp_path(value, os_name="nt")
        self.assertEqual(native_path_text(value, os_name="nt"), value)


if __name__ == "__main__":
    unittest.main()
