#!/usr/bin/env python3
"""Regression tests for the external-tool execution boundary."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

import path_compat
import tool_exec


class ToolExecTests(unittest.TestCase):
    def test_rejects_msys_tmp_before_launching_native_windows_tool(self) -> None:
        with (
            mock.patch.object(path_compat.os, "name", "nt"),
            mock.patch.object(tool_exec, "resolve_command_argv") as resolve,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            result = tool_exec.main(
                [
                    "--capability",
                    "capture.video.captions",
                    "yt-dlp",
                    "--",
                    "-o",
                    "/tmp/llmwiki-vid-old/audio.%(ext)s",
                    "https://www.bilibili.com/video/BVexample/",
                ]
            )

        self.assertEqual(result, 2)
        self.assertIn("create_temp_dir.py", stderr.getvalue())
        resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
