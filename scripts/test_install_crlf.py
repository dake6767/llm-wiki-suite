#!/usr/bin/env python3
"""Regression test for CRLF emitted by native Windows Python into Git Bash."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts" / "install.sh"
BOOTSTRAP = ROOT / "bootstrap.sh"


class InstallCrLfTests(unittest.TestCase):
    def make_crlf_python(self, temp: Path) -> dict[str, str]:
        fake_bin = temp / "bin"
        fake_bin.mkdir()
        wrapper = fake_bin / "python3"
        wrapper.write_text(
            "#!" + sys.executable + "\n"
            "import os, subprocess, sys\n"
            "p = subprocess.run([os.environ['LLM_WIKI_REAL_PY'], *sys.argv[1:]], "
            "stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
            "sys.stdout.buffer.write(p.stdout.replace(b'\\n', b'\\r\\n'))\n"
            "sys.stderr.buffer.write(p.stderr)\n"
            "raise SystemExit(p.returncode)\n",
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
                ["bash", str(INSTALL), "--dry-run", "--target", str(temp / "skills"), "my-llm-wiki"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("source missing", result.stdout)
            self.assertIn("my-llm-wiki: linking", result.stdout)

    def test_bootstrap_strips_crlf_from_default_target_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            env = self.make_crlf_python(temp)
            env["HOME"] = str(temp / "home")
            result = subprocess.run(
                ["bash", str(BOOTSTRAP), "--repo", str(ROOT), "--dry-run"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertNotIn(b"\r", result.stdout)
            self.assertNotIn(b"source missing", result.stdout)


if __name__ == "__main__":
    unittest.main()
