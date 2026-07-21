from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tool_runtime


class ToolRuntimeTests(unittest.TestCase):
    def test_non_windows_requires_protocol_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(tool_runtime.ToolRuntimeError, "receipt is missing"):
                tool_runtime.resolve_command_argv(
                    "yt-dlp", system="Linux", receipt_path=Path(tmp) / "missing.json"
                )

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
                "protocol": 5,
                "platform": "windows",
                "home": str(home),
                "suite": str(home / "suite"),
                "runtime": str(home / "runtime"),
                "tools": {
                    "opencli": {"argv": ["{home}/tools/opencli/node.exe", "{home}/tools/opencli/opencli.js"]}
                },
            }), encoding="utf-8")
            resolved = tool_runtime.resolve_command_argv(
                "opencli", system="Windows", receipt_path=receipt
            )
            self.assertEqual(resolved[0], str(node.resolve()))
            self.assertEqual(Path(resolved[1]), script.resolve())

    def test_windows_never_falls_back_to_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "receipt.json"
            receipt.write_text(json.dumps({
                "schema": 1,
                "protocol": 5,
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
                "protocol": 5,
                "platform": "windows",
                "home": tmp,
                "tools": {"tool": {"argv": [str(executable)]}},
            }), encoding="utf-8")
            with self.assertRaisesRegex(tool_runtime.ToolRuntimeError, "escapes install home"):
                tool_runtime.resolve_command_argv(
                    "tool", system="Windows", receipt_path=receipt
                )

    def test_non_windows_python_uses_managed_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            profile = home / "components" / "asr-other" / "python-asr-other"
            profile.parent.mkdir(parents=True)
            profile.write_text("#!/bin/sh\n", encoding="utf-8")
            receipt = home / "install-state.json"
            receipt.write_text(json.dumps({
                "schema": 1,
                "protocol": 5,
                "platform": "darwin",
                "home": str(home),
                "python_profiles": {"asr-other": str(profile)},
            }), encoding="utf-8")
            self.assertEqual(
                tool_runtime.resolve_python(
                    "asr-other", system="Darwin", receipt_path=receipt
                ),
                str(profile.resolve()),
            )


if __name__ == "__main__":
    unittest.main()
