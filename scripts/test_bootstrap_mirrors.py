#!/usr/bin/env python3
"""Regression tests for the Protocol 5 acquisition wrapper."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "bootstrap.sh"
REGISTRY = ROOT / "registry" / "bootstrap.json"
BASH = shutil.which("bash")
if BASH is None:
    raise RuntimeError("bash is required")


class BootstrapProtocolTests(unittest.TestCase):
    def test_registry_declares_protocol_and_canonical_mirror_order(self) -> None:
        config = json.loads(REGISTRY.read_text(encoding="utf-8"))
        self.assertEqual(config["version"], 5)
        self.assertEqual(config["repo_url"], "https://github.com/dake6767/llm-wiki-suite.git")
        self.assertIn(
            "https://gitee.com/dake6767/llm-wiki-suite.git",
            [row["url"] for row in config["repo_mirrors"]],
        )

    def test_requires_explicit_protocol_command(self) -> None:
        result = subprocess.run(
            [BASH, str(BOOTSTRAP), "--repo", str(ROOT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("choose one command", result.stderr)

    def test_rejects_protocol4_flags_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "HOME": str(Path(tmp) / "home")}
            result = subprocess.run(
                [BASH, str(BOOTSTRAP), "--repo", str(ROOT), "--host", "codex"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertFalse((Path(tmp) / "home" / ".codex").exists())
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsupported Protocol 5 command", result.stderr)

    def test_dispatches_status_to_protocol_core(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "LLM_WIKI_INSTALL_HOME": str(Path(tmp) / "managed"),
                "LLM_WIKI_INSTALL_SESSION_ROOT": str(Path(tmp) / "sessions"),
            }
            result = subprocess.run(
                [BASH, str(BOOTSTRAP), "--repo", str(ROOT), "status", "--json"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["protocol"], 5)

    def test_update_rejects_non_main_before_pull(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shims = Path(tmp) / "shims"
            shims.mkdir()
            git = shims / "git"
            git.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'status --porcelain'*) : ;;\n"
                "  *'symbolic-ref --quiet --short HEAD'*) echo feature/test ;;\n"
                "  *) echo unexpected >&2; exit 90 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            git.chmod(0o755)
            env = {**os.environ, "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}"}
            result = subprocess.run(
                [BASH, str(BOOTSTRAP), "--repo", str(ROOT), "--update", "status"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires branch main", result.stderr)

    def test_windows_gitbash_points_to_headless_native_core(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "bootstrap.sh"
            shutil.copy2(BOOTSTRAP, script)
            shims = root / "shims"
            shims.mkdir()
            uname = shims / "uname"
            uname.write_text("#!/bin/sh\necho MINGW64_NT-10.0\n", encoding="utf-8")
            uname.chmod(0o755)
            env = {**os.environ, "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}"}
            result = subprocess.run(
                [BASH, str(script), "inspect"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Protocol 5 headless native core", result.stderr)
        self.assertIn("inspect/plan/apply", result.stderr)


if __name__ == "__main__":
    unittest.main()
