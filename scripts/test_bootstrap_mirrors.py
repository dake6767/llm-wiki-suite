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
INSTALL = ROOT / "scripts" / "install.sh"
REGISTRY = ROOT / "registry" / "bootstrap.json"
GITHUB_REPO = "https://github.com/dake6767/llm-wiki-suite.git"
GITEE_REPO = "https://gitee.com/dake6767/llm-wiki-suite.git"
GITEE_BOOTSTRAP = "https://gitee.com/dake6767/llm-wiki-suite/raw/main/bootstrap.sh"
BASH = shutil.which("bash")
if BASH is None:
    raise RuntimeError("bash is required for bootstrap protocol tests")


def bash_path(path: Path) -> str:
    """Render a filesystem path for Bash itself, including MSYS drive form."""
    value = path.resolve().as_posix()
    if os.name == "nt" and len(value) >= 3 and value[1:3] == ":/":
        return f"/{value[0].lower()}{value[2:]}"
    return value


class BootstrapMirrorTests(unittest.TestCase):
    def test_requires_user_selected_target_before_reusing_or_cloning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            env = os.environ.copy()
            env["HOME"] = bash_path(home)
            completed = subprocess.run(
                [BASH, bash_path(BOOTSTRAP), "--repo", bash_path(ROOT), "--dry-run"],
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
            env["HOME"] = bash_path(home)
            env["LLM_WIKI_REPO_HOME"] = bash_path(home / ".my-llm-wiki" / "suite")
            completed = subprocess.run(
                [
                    BASH,
                    bash_path(script),
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
            env["HOME"] = bash_path(home)
            env["PATH"] = f"{shims}{os.pathsep}{env['PATH']}"
            env["CURL_MARKER"] = bash_path(marker)
            env["LLM_WIKI_REPO_HOME"] = bash_path(home / ".my-llm-wiki" / "suite")
            completed = subprocess.run(
                [BASH, bash_path(script), "--host", "codex", "--dry-run"],
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
                    BASH, bash_path(BOOTSTRAP), "--repo", bash_path(ROOT),
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

    def test_windows_gitbash_hard_cuts_over_to_native_setup(self) -> None:
        """A simulated Git Bash must stop before parsing legacy install arguments."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "bootstrap.sh"
            shutil.copy2(BOOTSTRAP, script)
            home = root / "home"
            home.mkdir()

            shims = root / "shims"
            shims.mkdir()
            (shims / "uname").write_text("#!/bin/sh\necho MINGW64_NT-10.0\n", encoding="utf-8")
            (shims / "uname").chmod(0o755)

            env = os.environ.copy()
            env["HOME"] = bash_path(home)
            env["PATH"] = f"{shims}{os.pathsep}{env['PATH']}"
            env["LLM_WIKI_REPO_HOME"] = "/c/Users/tester/.my-llm-wiki/suite"
            completed = subprocess.run(
                [
                    BASH,
                    bash_path(script),
                    "--repo-url",
                    GITEE_REPO,
                    "--custom-target",
                    "/c/Users/tester/.workbuddy/skills",
                    "--dry-run",
                ],
                cwd=root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("Windows no longer supports", completed.stderr)
        self.assertIn("My-LLM-Wiki-Setup.exe", completed.stderr)
        self.assertNotIn("repo:", completed.stdout)
        self.assertNotIn("custom-target:", completed.stdout)

    def test_bootstrap_initializes_wiki_before_doctor_and_reports_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            target = root / "skills"
            env = os.environ.copy()
            env["HOME"] = bash_path(home)
            env["USERPROFILE"] = str(home.resolve())
            completed = subprocess.run(
                [
                    BASH,
                    bash_path(BOOTSTRAP),
                    "--repo",
                    bash_path(ROOT),
                    "--custom-target",
                    bash_path(target),
                    "cn-mirrors",
                ],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                encoding="utf-8",
            )

            registry = home / ".my-llm-wiki" / "wikis.json"
            wiki = home / "wikis" / "my-llm-wiki"
            stdout = completed.stdout

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(registry.is_file())
            self.assertTrue((wiki / "schema.md").is_file())
            self.assertTrue((wiki / "wiki").is_dir())

        self.assertLess(stdout.index("wiki-init: ready"), stdout.index("✓ wiki"))
        self.assertIn("status: installed", stdout)

    def test_local_installer_initializes_wiki_even_when_doctor_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            target = root / "skills"
            env = os.environ.copy()
            env["HOME"] = bash_path(home)
            env["USERPROFILE"] = str(home.resolve())
            completed = subprocess.run(
                [
                    BASH,
                    bash_path(INSTALL),
                    "--custom-target",
                    bash_path(target),
                    "--copy",
                    "--no-doctor",
                    "cn-mirrors",
                ],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            wiki = home / "wikis" / "my-llm-wiki"
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((wiki / "schema.md").is_file())
            self.assertIn("wiki-init: ready", completed.stdout)
            self.assertIn("status: installed-unverified", completed.stdout)
            self.assertNotIn("✓ wiki", completed.stdout)


if __name__ == "__main__":
    unittest.main()
