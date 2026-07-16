#!/usr/bin/env python3
"""Regression test for CRLF emitted by native Windows Python into Git Bash."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts" / "install.sh"
BOOTSTRAP = ROOT / "bootstrap.sh"


def bash_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if os.name == "nt" and len(value) >= 3 and value[1:3] == ":/":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def native_forward_path(path: Path) -> str:
    return path.resolve().as_posix()


class InstallCrLfTests(unittest.TestCase):
    def make_crlf_python(self, temp: Path) -> dict[str, str]:
        fake_bin = temp / "bin"
        fake_bin.mkdir()
        driver = fake_bin / "crlf_driver.py"
        driver.write_text(
            "import os, subprocess, sys\n"
            "p = subprocess.run([os.environ['LLM_WIKI_REAL_PY'], *sys.argv[1:]], "
            "stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
            "sys.stdout.buffer.write(p.stdout.replace(b'\\n', b'\\r\\n'))\n"
            "sys.stderr.buffer.write(p.stderr)\n"
            "raise SystemExit(p.returncode)\n",
            encoding="utf-8",
        )
        wrapper = fake_bin / "python3"
        wrapper.write_text(
            "#!/bin/sh\nexec "
            + shlex.quote(bash_path(Path(sys.executable)))
            + " "
            + shlex.quote(bash_path(driver))
            + ' "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return {
            **os.environ,
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "LLM_WIKI_REAL_PY": sys.executable,
        }

    def test_crlf_graph_output_does_not_corrupt_source_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            env = self.make_crlf_python(temp)
            result = subprocess.run(
                ["bash", bash_path(INSTALL), "--dry-run", "--custom-target",
                 bash_path(temp / "skills"), "my-llm-wiki"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("invalid skill source", result.stdout)
            self.assertIn("my-llm-wiki [requested]: create", result.stdout)

    def test_install_requires_an_explicit_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            env = {**os.environ, "HOME": bash_path(temp / "home")}
            result = subprocess.run(
                ["bash", bash_path(INSTALL), "--dry-run"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("select at least one --host or --custom-target", result.stderr)
            self.assertFalse((temp / "home" / ".claude").exists())
            self.assertFalse((temp / "home" / ".hermes").exists())

    def test_bootstrap_handles_crlf_graph_output_with_selected_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            env = self.make_crlf_python(temp)
            env["HOME"] = bash_path(temp / "home")
            target = temp / "home" / ".workbuddy" / "skills"
            result = subprocess.run(
                ["bash", bash_path(BOOTSTRAP), "--repo", bash_path(ROOT),
                 "--custom-target", bash_path(target), "--dry-run"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertNotIn(b"invalid skill source", result.stdout)
            self.assertIn(native_forward_path(target).encode(), result.stdout)


if __name__ == "__main__":
    unittest.main()
