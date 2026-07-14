#!/usr/bin/env python3
"""Regression tests for canonical + mainland-China bootstrap entry points."""

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
GITHUB_REPO = "https://github.com/dake6767/llm-wiki-suite.git"
GITEE_REPO = "https://gitee.com/dake6767/llm-wiki-suite.git"
GITEE_BOOTSTRAP = "https://gitee.com/dake6767/llm-wiki-suite/raw/main/bootstrap.sh"


class BootstrapMirrorTests(unittest.TestCase):
    def test_registry_declares_canonical_and_gitee_mirror(self) -> None:
        config = json.loads(REGISTRY.read_text(encoding="utf-8"))
        self.assertEqual(config["repo_url"], GITHUB_REPO)
        self.assertIn(
            GITEE_REPO,
            [mirror["url"] for mirror in config["repo_mirrors"]],
        )
        self.assertIn(
            GITEE_BOOTSTRAP,
            [mirror["url"] for mirror in config["bootstrap_script_mirrors"]],
        )

    def test_standalone_explicit_gitee_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "bootstrap.sh"
            shutil.copy2(BOOTSTRAP, script)
            home = root / "home"
            target = home / ".codex" / "skills"
            target.mkdir(parents=True)
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["LLM_WIKI_REPO_HOME"] = str(home / ".my-llm-wiki" / "suite")
            completed = subprocess.run(
                [
                    "bash",
                    str(script),
                    "--repo-url",
                    GITEE_REPO,
                    "--target",
                    str(target),
                    "--dry-run",
                ],
                cwd=root,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertIn(GITEE_REPO, completed.stdout)
        self.assertNotIn(GITHUB_REPO, completed.stdout)
        self.assertIn("requested skills: all active skills", completed.stdout)


if __name__ == "__main__":
    unittest.main()
