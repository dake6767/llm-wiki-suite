#!/usr/bin/env python3
"""The Protocol 5 shell wrapper must tolerate CRLF from native Python."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")


class InstallCrLfTests(unittest.TestCase):
    def test_status_output_from_crlf_python_remains_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            driver = bindir / "driver.py"
            driver.write_text(
                "import os,subprocess,sys\n"
                "p=subprocess.run([os.environ['REAL_PY'],*sys.argv[1:]],stdout=subprocess.PIPE,stderr=subprocess.PIPE)\n"
                "sys.stdout.buffer.write(p.stdout.replace(b'\\n',b'\\r\\n'))\n"
                "sys.stderr.buffer.write(p.stderr)\nraise SystemExit(p.returncode)\n",
                encoding="utf-8",
            )
            wrapper = bindir / "python3"
            wrapper.write_text(
                "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                + shlex.quote(str(driver)) + " \"$@\"\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                "REAL_PY": sys.executable,
                "LLM_WIKI_INSTALL_HOME": str(root / "home"),
                "LLM_WIKI_INSTALL_SESSION_ROOT": str(root / "sessions"),
            }
            result = subprocess.run(
                [BASH, str(ROOT / "bootstrap.sh"), "--repo", str(ROOT), "status", "--json"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(json.loads(result.stdout.decode())["protocol"], 5)


if __name__ == "__main__":
    unittest.main()
