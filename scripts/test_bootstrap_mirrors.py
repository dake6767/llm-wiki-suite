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
    def test_requires_user_selected_target_before_reusing_or_cloning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            env = os.environ.copy()
            env["HOME"] = str(home)
            completed = subprocess.run(
                ["bash", str(BOOTSTRAP), "--repo", str(ROOT), "--dry-run"],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("no agent target selected", completed.stderr)
        self.assertNotIn("Syncing skills", completed.stdout)

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

    def test_windows_gitbash_normalizes_msys_paths_for_native_tools(self) -> None:
        """Simulated Git Bash: /c/... args must become C:/... before git/python."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "bootstrap.sh"
            shutil.copy2(BOOTSTRAP, script)
            home = root / "home"
            home.mkdir()

            shims = root / "shims"
            shims.mkdir()
            (shims / "uname").write_text("#!/bin/sh\necho MINGW64_NT-10.0\n", encoding="utf-8")
            (shims / "cygpath").write_text(
                "#!/bin/sh\n"
                '# minimal cygpath -m: /c/foo -> C:/foo, pass anything else through\n'
                'p="$2"\n'
                'case "$p" in\n'
                "  /[a-zA-Z]/*)\n"
                '    d=$(printf %s "$p" | cut -c2 | tr "[:lower:]" "[:upper:]")\n'
                '    printf "%s:%s\\n" "$d" "$(printf %s "$p" | cut -c3-)"\n'
                "    ;;\n"
                '  *) printf "%s\\n" "$p" ;;\n'
                "esac\n",
                encoding="utf-8",
            )
            for shim in ("uname", "cygpath"):
                (shims / shim).chmod(0o755)

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = f"{shims}{os.pathsep}{env['PATH']}"
            env["LLM_WIKI_REPO_HOME"] = "/c/Users/tester/.my-llm-wiki/suite"
            completed = subprocess.run(
                [
                    "bash",
                    str(script),
                    "--repo-url",
                    GITEE_REPO,
                    "--target",
                    "/c/Users/tester/.workbuddy/skills",
                    "--dry-run",
                ],
                cwd=root,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertIn("destination: C:/Users/tester/.my-llm-wiki/suite", completed.stdout)
        self.assertIn("target: C:/Users/tester/.workbuddy/skills", completed.stdout)
        self.assertNotIn("/c/Users/tester", completed.stdout)


if __name__ == "__main__":
    unittest.main()
