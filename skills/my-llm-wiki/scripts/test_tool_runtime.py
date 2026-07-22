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
    def test_open_skills_use_system_tool_without_setup_core(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "ffmpeg"
            executable.write_bytes(b"tool")
            with mock.patch.object(
                tool_runtime.shutil, "which", return_value=str(executable)
            ):
                resolved = tool_runtime.resolve_command(
                    "ffmpeg",
                    state_path=Path(tmp) / "missing-state.json",
                    providers_path=Path(tmp) / "missing-providers.json",
                )
            self.assertEqual(resolved.provider, "system")
            self.assertEqual(resolved.argv, [str(executable.resolve())])

    def test_official_provider_is_preferred_over_system(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official = root / "pack" / "bin" / "ffmpeg"
            official.parent.mkdir(parents=True)
            official.write_bytes(b"official")
            system = root / "system-ffmpeg"
            system.write_bytes(b"system")
            state = self._state(root, commands={"ffmpeg": ["{pack}/bin/ffmpeg"]})
            with mock.patch.object(tool_runtime.shutil, "which", return_value=str(system)):
                resolved = tool_runtime.resolve_command(
                    "ffmpeg", state_path=state, providers_path=root / "missing.json"
                )
            self.assertEqual(resolved.provider, "official")
            self.assertEqual(resolved.argv[0], str(official.resolve()))

    def test_explicit_system_provider_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official = root / "pack" / "bin" / "opencli"
            official.parent.mkdir(parents=True)
            official.write_bytes(b"official")
            system = root / "opencli"
            system.write_bytes(b"system")
            state = self._state(root, commands={"opencli": ["{pack}/bin/opencli"]})
            with mock.patch.object(tool_runtime.shutil, "which", return_value=str(system)):
                resolved = tool_runtime.resolve_command(
                    "opencli",
                    provider="system",
                    state_path=state,
                    providers_path=root / "missing.json",
                )
            self.assertEqual(resolved.provider, "system")

    def test_saved_custom_override_wins_and_uses_structured_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            custom = root / "custom-opencli"
            custom.write_bytes(b"custom")
            providers = root / "providers.json"
            providers.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "policy": "official-preferred",
                        "overrides": {"capture.web": "my-browser"},
                        "providers": {
                            "my-browser": {
                                "commands": {"opencli": [str(custom), "--profile", "wiki"]}
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            resolved = tool_runtime.resolve_command(
                "opencli",
                capability="capture.web",
                state_path=root / "missing.json",
                providers_path=providers,
            )
            self.assertEqual(resolved.provider, "my-browser")
            self.assertEqual(resolved.argv, [str(custom.resolve()), "--profile", "wiki"])

    def test_unhealthy_saved_override_falls_back_to_official(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official = root / "pack" / "bin" / "tool"
            official.parent.mkdir(parents=True)
            official.write_bytes(b"official")
            state = self._state(root, commands={"tool": ["{pack}/bin/tool"]})
            providers = root / "providers.json"
            providers.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "policy": "official-preferred",
                        "overrides": {"tool.tool": "broken"},
                        "providers": {
                            "broken": {"commands": {"tool": [str(root / "missing")]}}
                        },
                    }
                ),
                encoding="utf-8",
            )
            resolved = tool_runtime.resolve_command(
                "tool", state_path=state, providers_path=providers
            )
            self.assertEqual(resolved.provider, "official")

    def test_official_executable_cannot_escape_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.write_bytes(b"foreign")
            state = self._state(root, commands={"tool": [str(outside)]})
            with self.assertRaisesRegex(tool_runtime.ToolRuntimeError, "escapes its pack"):
                tool_runtime.resolve_command(
                    "tool",
                    provider="official",
                    state_path=state,
                    providers_path=root / "missing.json",
                )

    def test_python_provider_reexec_sets_identity_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / "pack" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"python")
            state = self._state(
                root,
                python_profiles={"asr-zh": ["{pack}/bin/python"]},
                environment={"asr-zh": {"MODEL_ROUTE": "official"}},
            )
            with mock.patch.dict(
                os.environ,
                {tool_runtime.SETUP_STATE_ENV: str(state)},
                clear=True,
            ), mock.patch.object(
                tool_runtime.sys, "argv", ["runner.py", "audio.wav"]
            ), mock.patch.object(tool_runtime.os, "execve") as execve:
                tool_runtime.ensure_provider_python("asr-zh")
            executable, argv, environment = execve.call_args.args
            self.assertEqual(executable, str(python.resolve()))
            self.assertEqual(argv[-1], "audio.wav")
            self.assertEqual(environment[tool_runtime.ACTIVE_PROVIDER_ENV], "official")
            self.assertEqual(environment["MODEL_ROUTE"], "official")

    def test_active_python_provider_does_not_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / "pack" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"python")
            state = self._state(
                root,
                python_profiles={"asr-other": ["{pack}/bin/python"]},
            )
            with mock.patch.dict(
                os.environ,
                {
                    tool_runtime.SETUP_STATE_ENV: str(state),
                    tool_runtime.ACTIVE_PYTHON_PROFILE_ENV: "asr-other",
                    tool_runtime.ACTIVE_PROVIDER_ENV: "official",
                },
                clear=True,
            ), mock.patch.object(
                tool_runtime.sys, "executable", str(python)
            ), mock.patch.object(tool_runtime.os, "execve") as execve:
                tool_runtime.ensure_provider_python("asr-other")
            execve.assert_not_called()

    @staticmethod
    def _state(
        root: Path,
        *,
        commands: dict[str, list[str]] | None = None,
        python_profiles: dict[str, list[str]] | None = None,
        environment: dict[str, dict[str, str]] | None = None,
    ) -> Path:
        pack = root / "pack"
        pack.mkdir(exist_ok=True)
        state = root / "setup-state.json"
        state.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "packs": {
                        "test": {
                            "path": str(pack),
                            "artifact": {
                                "commands": commands or {},
                                "python_profiles": python_profiles or {},
                                "environment": environment or {},
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return state


if __name__ == "__main__":
    unittest.main()
