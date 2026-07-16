#!/usr/bin/env python3
"""Regression tests for native-Windows CLI path normalization."""

import unittest

from path_compat import native_path_text


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


if __name__ == "__main__":
    unittest.main()
