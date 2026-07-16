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
        self.assertIn("select at least one --host or --custom-target", completed.stderr)
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
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["LLM_WIKI_REPO_HOME"] = str(home / ".my-llm-wiki" / "suite")
            completed = subprocess.run(
                [
                    "bash",
                    str(script),
                    "--repo-url",
                    GITEE_REPO,
                    "--host",
                    "codex",
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
        self.assertIn("host: codex", completed.stdout)

    def test_fresh_dry_run_never_probes_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "bootstrap.sh"
            shutil.copy2(BOOTSTRAP, script)
            home = root / "home"
            shims = root / "shims"
            shims.mkdir()
            marker = root / "curl-called"
            curl = shims / "curl"
            curl.write_text(
                "#!/bin/sh\ntouch \"$CURL_MARKER\"\nexit 99\n", encoding="utf-8"
            )
            curl.chmod(0o755)
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = f"{shims}{os.pathsep}{env['PATH']}"
            env["CURL_MARKER"] = str(marker)
            env["LLM_WIKI_REPO_HOME"] = str(home / ".my-llm-wiki" / "suite")
            completed = subprocess.run(
                ["bash", str(script), "--host", "codex", "--dry-run"],
                cwd=root,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            curl_called = marker.exists()

        self.assertFalse(curl_called)
        self.assertIn(GITHUB_REPO, completed.stdout)
        self.assertIn(GITEE_REPO, completed.stdout)

    def test_update_rejects_non_main_checkout_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shims = root / "shims"
            shims.mkdir()
            git = shims / "git"
            git.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'rev-parse --is-inside-work-tree'*) echo true ;;\n"
                "  *'status --porcelain'*) : ;;\n"
                "  *'symbolic-ref --quiet --short HEAD'*) echo feature/test ;;\n"
                "  *) echo unexpected-git-call >&2; exit 90 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            git.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{shims}{os.pathsep}{env['PATH']}"
            completed = subprocess.run(
                [
                    "bash", str(BOOTSTRAP), "--repo", str(ROOT),
                    "--host", "codex", "--update", "--dry-run",
                ],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("requires branch main", completed.stderr)
        self.assertNotIn("checkout:", completed.stderr)

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
                    "--custom-target",
                    "/c/Users/tester/.workbuddy/skills",
                    "--dry-run",
                ],
                cwd=root,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertIn("repo: C:/Users/tester/.my-llm-wiki/suite", completed.stdout)
        self.assertIn("custom-target: C:/Users/tester/.workbuddy/skills", completed.stdout)
        self.assertNotIn("/c/Users/tester", completed.stdout)


if __name__ == "__main__":
    unittest.main()
