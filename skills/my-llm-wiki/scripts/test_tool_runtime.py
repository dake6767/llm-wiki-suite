from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tool_runtime


class ToolRuntimeTests(unittest.TestCase):
    def test_non_windows_uses_path_without_reading_receipt(self) -> None:
        with mock.patch.object(tool_runtime.shutil, "which", return_value="/opt/bin/yt-dlp"), \
                mock.patch.object(tool_runtime, "load_setup_receipt") as load:
            self.assertEqual(
                tool_runtime.resolve_command_argv("yt-dlp", system="Linux"),
                ["/opt/bin/yt-dlp"],
            )
        load.assert_not_called()

    def test_windows_resolves_only_receipt_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            node = home / "tools" / "opencli" / "node.exe"
            node.parent.mkdir(parents=True)
            node.write_bytes(b"MZ")
            script = home / "tools" / "opencli" / "opencli.js"
            script.write_text("", encoding="utf-8")
            receipt = home / "install-state.json"
            receipt.write_text(json.dumps({
                "schema": 1,
                "platform": "windows",
                "home": str(home),
                "suite": str(home / "suite"),
                "runtime": str(home / "runtime"),
                "tools": {
                    "opencli": {"argv": ["{home}/tools/opencli/node.exe", "{home}/tools/opencli/opencli.js"]}
                },
            }), encoding="utf-8")
            self.assertEqual(
                tool_runtime.resolve_command_argv(
                    "opencli", system="Windows", receipt_path=receipt
                ),
                [str(node.resolve()), str(script.resolve())],
            )

    def test_windows_never_falls_back_to_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(tool_runtime.shutil, "which", return_value="C:/global/opencli.exe"):
            receipt = Path(tmp) / "receipt.json"
            receipt.write_text(json.dumps({
                "schema": 1,
                "platform": "windows",
                "home": tmp,
                "tools": {},
            }), encoding="utf-8")
            with self.assertRaisesRegex(tool_runtime.ToolRuntimeError, "not installed"):
                tool_runtime.resolve_command_argv(
                    "opencli", system="Windows", receipt_path=receipt
                )

    def test_windows_rejects_executable_outside_managed_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            executable = Path(other) / "tool.exe"
            executable.write_bytes(b"MZ")
            receipt = Path(tmp) / "receipt.json"
            receipt.write_text(json.dumps({
                "schema": 1,
                "platform": "windows",
                "home": tmp,
                "tools": {"tool": {"argv": [str(executable)]}},
            }), encoding="utf-8")
            with self.assertRaisesRegex(tool_runtime.ToolRuntimeError, "escapes Setup home"):
                tool_runtime.resolve_command_argv(
                    "tool", system="Windows", receipt_path=receipt
                )

    def test_non_windows_python_keeps_legacy_contract(self) -> None:
        with mock.patch.dict(os.environ, {"LLM_WIKI_ASR_PYTHON": ""}, clear=False):
            self.assertTrue(tool_runtime.resolve_python("asr-other", system="Darwin"))


if __name__ == "__main__":
    unittest.main()
