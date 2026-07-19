#!/usr/bin/env python3
"""Unit and integration tests for the Windows-native Setup controller."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

from scripts import windows_setup


ROOT = Path(__file__).resolve().parent.parent


def write_zip(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(source.rglob("*")):
            if path.is_file() and not any(
                part in {"__pycache__", ".git", "data", "reports"}
                for part in path.relative_to(source).parts
            ):
                bundle.write(path, path.relative_to(source).as_posix())


class WindowsSetupTests(unittest.TestCase):
    def make_payload(self, root: Path, *, foreign: bool = False) -> tuple[Path, Path, Path]:
        payload = root / "payload"
        payload.mkdir()
        suite = root / "suite"
        shutil.copytree(ROOT / "registry", suite / "registry")
        shutil.copytree(ROOT / "scripts", suite / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "skills", suite / "skills", ignore=shutil.ignore_patterns(
            "__pycache__", "data", "reports"
        ))
        target = root / "agent" / "skills"
        wiki = root / "wiki"
        wiki.mkdir()
        (wiki / "schema.md").write_text("schema", encoding="utf-8")
        (wiki / "wiki").mkdir()
        registry_path = root / "state" / "wikis.json"
        registry_path.parent.mkdir()
        registry_path.write_text(json.dumps({
            "version": 1,
            "wikis": [{"path": str(wiki), "name": "test", "default": True}],
        }), encoding="utf-8")
        bootstrap_path = suite / "registry" / "bootstrap.json"
        bootstrap = json.loads(bootstrap_path.read_text(encoding="utf-8"))
        bootstrap["agent_hosts"] = {
            "codex": {
                "detect_dir": str(target.parent),
                "skills_dir": str(target),
            }
        }
        bootstrap["install"]["lock_file"] = str(root / "state" / "install.lock")
        bootstrap["wiki_registry_path"] = str(registry_path)
        bootstrap["default_wiki_root"] = str(root / "default-wiki")
        bootstrap_path.write_text(json.dumps(bootstrap), encoding="utf-8")
        write_zip(suite, payload / "suite.zip")

        python_stage = root / "python"
        python_stage.mkdir()
        (python_stage / "python.exe").write_bytes(b"MZ-test-python")
        (python_stage / "python312._pth").write_text("python312.zip\n.\n", encoding="utf-8")
        write_zip(python_stage, payload / "python.zip")
        python_hash = hashlib.sha256((payload / "python.zip").read_bytes()).hexdigest()
        lock = {
            "schema": 1,
            "setup_version": "test",
            "architecture": "x86_64",
            "python": {"version": "3.12.10", "sha256": python_hash},
        }
        (payload / "windows-toolchain.lock.json").write_text(
            json.dumps(lock), encoding="utf-8"
        )
        (payload / "component-manifest.json").write_text(json.dumps({
            "schema": 1,
            "setup_version": "test",
            "release_tag": "v-test",
            "sources": [],
            "components": {},
        }), encoding="utf-8")
        payload_files = [
            "suite.zip",
            "python.zip",
            "windows-toolchain.lock.json",
            "component-manifest.json",
        ]
        (payload / "setup-payload.json").write_text(json.dumps({
            "schema": 1,
            "files": {
                name: hashlib.sha256((payload / name).read_bytes()).hexdigest()
                for name in payload_files
            },
        }), encoding="utf-8")
        if foreign:
            destination = target / "my-llm-wiki"
            destination.mkdir(parents=True)
            (destination / "sentinel.txt").write_text("foreign", encoding="utf-8")
        return payload, target, wiki

    def test_safe_extract_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escape.txt", "bad")
            with self.assertRaisesRegex(windows_setup.SetupError, "unsafe zip member"):
                windows_setup.safe_extract(archive, root / "out")
            self.assertFalse((root / "escape.txt").exists())

    def test_replace_with_retries_survives_transient_locks(self) -> None:
        calls = {"count": 0}
        real_replace = windows_setup.os.replace

        def flaky(source, target):
            calls["count"] += 1
            if calls["count"] < 3:
                raise PermissionError(5, "Access is denied")
            real_replace(source, target)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "staging").mkdir()
            with unittest.mock.patch.object(windows_setup.os, "replace", flaky), \
                    unittest.mock.patch.object(windows_setup.time, "sleep"):
                windows_setup.replace_with_retries(root / "staging", root / "final")
            self.assertEqual(calls["count"], 3)
            self.assertTrue((root / "final").is_dir())

    def test_replace_with_retries_gives_up_with_guidance(self) -> None:
        def locked(source, target):
            raise PermissionError(5, "Access is denied")

        clock = {"now": 0.0}

        def tick(seconds):
            clock["now"] += seconds

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "staging").mkdir()
            with unittest.mock.patch.object(windows_setup.os, "replace", locked), \
                    unittest.mock.patch.object(windows_setup.time, "sleep", tick), \
                    unittest.mock.patch.object(
                        windows_setup.time, "monotonic", lambda: clock["now"]
                    ):
                with self.assertRaisesRegex(windows_setup.SetupError, "antivirus"):
                    windows_setup.replace_with_retries(root / "staging", root / "final")

    def test_payload_manifest_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload, _, _ = self.make_payload(root)
            self.assertEqual(windows_setup.payload_dir(payload), payload.resolve())
            with (payload / "component-manifest.json").open("ab") as handle:
                handle.write(b" ")
            with self.assertRaisesRegex(windows_setup.SetupError, "payload hash mismatch"):
                windows_setup.payload_dir(payload)

    def test_core_install_tags_copies_and_uninstall_removes_only_owned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload, target, wiki = self.make_payload(root)
            home = root / "managed-home"
            status = windows_setup.install_flow(
                hosts=["codex"],
                components=[],
                home=home,
                payload=payload,
                asset_dir=None,
                allow_test_platform=True,
                skip_postcheck=True,
            )
            self.assertEqual(status, 0)
            receipt = windows_setup.read_receipt(home)
            self.assertEqual(receipt["hosts"], ["codex"])
            self.assertEqual(receipt["components"], {})
            self.assertEqual(
                Path(receipt["runtime"]),
                home / "runtime" / "python",
            )
            manifest = json.loads(
                (target / "my-llm-wiki" / ".llm-wiki-install.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["installer"], "windows-setup")
            self.assertEqual(manifest["install_id"], receipt["install_id"])
            self.assertTrue((wiki / "schema.md").is_file())

            self.assertEqual(windows_setup.uninstall_hosts(["codex"], home, payload), 0)
            self.assertFalse((target / "my-llm-wiki").exists())
            self.assertTrue(wiki.exists(), "uninstall must preserve user Wikis")
            self.assertTrue((home / "runtime").is_dir())
            self.assertEqual(
                windows_setup.uninstall_hosts([], home, payload, purge=True), 0
            )
            self.assertIsNone(windows_setup.read_receipt(home))
            self.assertFalse((home / "runtime").exists())
            self.assertTrue(wiki.exists(), "purge must preserve user Wikis")

    def test_foreign_skill_stops_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload, target, _ = self.make_payload(root, foreign=True)
            home = root / "managed-home"
            with self.assertRaisesRegex(windows_setup.SetupError, "foreign skill"):
                windows_setup.install_flow(
                    hosts=["codex"],
                    components=[],
                    home=home,
                    payload=payload,
                    asset_dir=None,
                    allow_test_platform=True,
                    skip_postcheck=True,
                )
            self.assertEqual(
                (target / "my-llm-wiki" / "sentinel.txt").read_text(encoding="utf-8"),
                "foreign",
            )
            self.assertIsNone(windows_setup.read_receipt(home))

    def test_embedded_python_hash_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload, _, _ = self.make_payload(root)
            lock = windows_setup.embedded_json(payload, "windows-toolchain.lock.json")
            (payload / "python.zip").write_bytes(b"tampered")
            with self.assertRaisesRegex(windows_setup.SetupError, "Python hash mismatch"):
                windows_setup.ensure_runtime(payload, root / "home", lock)

    @unittest.skipIf(windows_setup.os.name == "nt", "POSIX runner fixture")
    def test_receipt_managed_tool_runner_does_not_use_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            tool = home / "components" / "video" / "yt-dlp.exe"
            tool.parent.mkdir(parents=True)
            tool.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
            windows_setup.write_receipt(home, {
                "schema": 1,
                "platform": "windows",
                "home": str(home.resolve()),
                "tools": {"yt-dlp": {"argv": [str(tool)]}},
                "python_profiles": {},
            })
            self.assertEqual(windows_setup.run_managed_tool("yt-dlp", [], home), 7)


if __name__ == "__main__":
    unittest.main()
