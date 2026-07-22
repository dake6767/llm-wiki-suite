from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_profile_launcher_reexec_marks_the_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            profile = home / "components" / "asr-zh" / "python-asr-zh"
            profile.parent.mkdir(parents=True)
            profile.write_text("#!/bin/sh\n", encoding="utf-8")
            receipt = home / "install-state.json"
            receipt.write_text(json.dumps({
                "schema": 1,
                "protocol": 5,
                "platform": tool_runtime._system_name(),
                "home": str(home),
                "python_profiles": {"asr-zh": str(profile)},
                "runtime_env": {"asr-zh": {"MODEL_ROUTE": "managed"}},
            }), encoding="utf-8")

            with mock.patch.dict(os.environ, {
                tool_runtime.INSTALL_RECEIPT_ENV: str(receipt),
            }, clear=True), mock.patch.object(
                tool_runtime.sys, "argv", ["runner.py", "audio.wav"]
            ), mock.patch.object(tool_runtime.os, "execve") as execve:
                tool_runtime.ensure_managed_python("asr-zh")

            argv = execve.call_args.args
            self.assertEqual(argv[0], str(profile.resolve()))
            self.assertEqual(argv[1][0], str(profile.resolve()))
            self.assertEqual(argv[1][-1], "audio.wav")
            self.assertEqual(
                argv[2][tool_runtime.ACTIVE_PYTHON_PROFILE_ENV], "asr-zh"
            )
            self.assertEqual(argv[2]["MODEL_ROUTE"], "managed")

    def test_profile_launcher_does_not_reexec_after_managed_python_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            runtime_python = home / "runtime" / "versions" / "3.12" / "bin" / "python3"
            runtime_python.parent.mkdir(parents=True)
            runtime_python.write_bytes(b"managed")
            component = home / "components" / "asr-zh"
            component.mkdir(parents=True)
            profile = component / "python-asr-zh"
            profile.write_text("#!/bin/sh\n", encoding="utf-8")
            site = component / "site"
            site.mkdir()
            receipt = home / "install-state.json"
            receipt.write_text(json.dumps({
                "schema": 1,
                "protocol": 5,
                "platform": tool_runtime._system_name(),
                "home": str(home),
                "runtime": {"path": str(runtime_python.parents[2])},
                "python_profiles": {"asr-zh": str(profile)},
                "runtime_env": {"asr-zh": {"MODEL_ROUTE": "managed"}},
            }), encoding="utf-8")

            with mock.patch.dict(os.environ, {
                tool_runtime.INSTALL_RECEIPT_ENV: str(receipt),
                tool_runtime.ACTIVE_PYTHON_PROFILE_ENV: "asr-zh",
                "PYTHONPATH": str(site),
            }, clear=True), mock.patch.object(
                tool_runtime.sys, "executable", str(runtime_python)
            ), mock.patch.object(tool_runtime.os, "execve") as execve:
                tool_runtime.ensure_managed_python("asr-zh")
                self.assertEqual(os.environ["MODEL_ROUTE"], "managed")

            execve.assert_not_called()

    def test_profile_marker_cannot_select_python_outside_managed_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            home = Path(tmp)
            component = home / "components" / "asr-zh"
            component.mkdir(parents=True)
            profile = component / "python-asr-zh"
            profile.write_text("#!/bin/sh\n", encoding="utf-8")
            site = component / "site"
            site.mkdir()
            foreign_python = Path(other) / "python3"
            foreign_python.write_bytes(b"foreign")
            receipt = home / "install-state.json"
            receipt.write_text(json.dumps({
                "schema": 1,
                "protocol": 5,
                "platform": tool_runtime._system_name(),
                "home": str(home),
                "python_profiles": {"asr-zh": str(profile)},
            }), encoding="utf-8")

            with mock.patch.dict(os.environ, {
                tool_runtime.INSTALL_RECEIPT_ENV: str(receipt),
                tool_runtime.ACTIVE_PYTHON_PROFILE_ENV: "asr-zh",
                "PYTHONPATH": str(site),
            }, clear=True), mock.patch.object(
                tool_runtime.sys, "executable", str(foreign_python)
            ), mock.patch.object(tool_runtime.os, "execve") as execve:
                tool_runtime.ensure_managed_python("asr-zh")

            execve.assert_called_once()

    @unittest.skipIf(os.name == "nt", "POSIX profile launchers use /bin/sh")
    def test_legacy_profile_launcher_completes_one_real_process_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            component = root / "components" / "asr-zh"
            site = component / "site"
            site.mkdir(parents=True)
            profile = component / "python-asr-zh"
            profile.write_text(
                "#!/bin/sh\n"
                f"export PYTHONPATH={shlex.quote(str(site))}\n"
                f"exec {shlex.quote(sys.executable)} \"$@\"\n",
                encoding="utf-8",
            )
            profile.chmod(0o755)
            result_path = root / "result.txt"
            runner = root / "runner.py"
            runner.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                f"sys.path.insert(0, {str(Path(tool_runtime.__file__).resolve().parent)!r})\n"
                "from tool_runtime import ensure_managed_python\n"
                "ensure_managed_python('asr-zh')\n"
                "Path(sys.argv[1]).write_text('ok', encoding='utf-8')\n",
                encoding="utf-8",
            )
            managed_home = os.path.commonpath(
                [str(root.resolve()), str(Path(sys.executable).resolve())]
            )
            receipt = root / "install-state.json"
            receipt.write_text(json.dumps({
                "schema": 1,
                "protocol": 5,
                "platform": tool_runtime._system_name(),
                "home": managed_home,
                "python_profiles": {"asr-zh": str(profile)},
            }), encoding="utf-8")
            env = os.environ.copy()
            env[tool_runtime.INSTALL_RECEIPT_ENV] = str(receipt)
            env.pop(tool_runtime.ACTIVE_PYTHON_PROFILE_ENV, None)

            completed = subprocess.run(
                [str(profile), str(runner), str(result_path)],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(result_path.read_text(encoding="utf-8"), "ok")


if __name__ == "__main__":
    unittest.main()
