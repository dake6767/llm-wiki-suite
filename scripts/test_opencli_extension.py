#!/usr/bin/env python3
"""Offline tests for the OpenCLI Browser Bridge extension stager."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import opencli_extension as ext  # noqa: E402


def make_extension_zip(path: Path, *, nested: str | None = "opencli-extension") -> None:
    with zipfile.ZipFile(path, "w") as bundle:
        prefix = f"{nested}/" if nested else ""
        bundle.writestr(f"{prefix}manifest.json", json.dumps({"manifest_version": 3}))
        bundle.writestr(f"{prefix}background.js", "// bridge\n")
        bundle.writestr(f"{prefix}assets/icon.png", "png")


class AssetSelectionTests(unittest.TestCase):
    def test_selects_the_extension_zip_among_other_assets(self) -> None:
        release = {
            "tag_name": "v1.8.6",
            "assets": [
                {"name": "opencli-1.8.6.tgz", "browser_download_url": "https://x/a.tgz"},
                {
                    "name": "opencli-extension-v1.0.22.zip",
                    "browser_download_url": "https://github.com/x/opencli-extension-v1.0.22.zip",
                },
            ],
        }
        asset = ext.select_asset(release)
        self.assertEqual(asset["name"], "opencli-extension-v1.0.22.zip")
        self.assertEqual(asset["version"], "1.0.22")

    def test_release_without_extension_asset_errors(self) -> None:
        with self.assertRaises(ext.ExtensionError):
            ext.select_asset({"tag_name": "v1.0.0", "assets": [{"name": "other.zip", "browser_download_url": "https://x/other.zip"}]})

    def test_multiple_extension_assets_error(self) -> None:
        release = {
            "assets": [
                {"name": "opencli-extension-v1.0.1.zip", "browser_download_url": "https://x/1.zip"},
                {"name": "opencli-extension-v1.0.2.zip", "browser_download_url": "https://x/2.zip"},
            ]
        }
        with self.assertRaises(ext.ExtensionError):
            ext.select_asset(release)

    def test_asset_name_parsing_rejects_unofficial_names(self) -> None:
        with self.assertRaises(ext.ExtensionError):
            ext.parse_asset_name("extension.zip")


class MirrorPrefixTests(unittest.TestCase):
    def test_prefix_is_prepended_to_the_full_https_url(self) -> None:
        self.assertEqual(
            ext.apply_mirror("https://github.com/a.zip", "https://mirror.example/"),
            "https://mirror.example/https://github.com/a.zip",
        )

    def test_non_https_prefix_is_rejected(self) -> None:
        with self.assertRaises(ext.ExtensionError):
            ext.apply_mirror("https://github.com/a.zip", "http://mirror.example")


class StagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.dest = self.base / "opencli-extension"

    def local_asset(self, name: str = "opencli-extension-v1.0.22.zip", **kwargs) -> str:
        archive = self.base / name
        make_extension_zip(archive, **kwargs)
        return archive.resolve().as_uri()

    def test_stage_from_nested_zip_writes_pointer_and_steps(self) -> None:
        report = ext.stage(self.dest, asset_url=self.local_asset())
        self.assertEqual(report["status"], "staged")
        staged = Path(report["path"])
        self.assertTrue((staged / "manifest.json").is_file())
        self.assertTrue((staged / "assets" / "icon.png").is_file())
        pointer = json.loads((self.dest / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["version"], "1.0.22")
        self.assertEqual(pointer["path"], str(staged))
        self.assertTrue(any("chrome://extensions" in step for step in report["load_steps"]))
        self.assertTrue(any("opencli doctor" in step for step in report["load_steps"]))
        leftovers = [p for p in self.dest.iterdir() if p.name.startswith(".staging-")]
        self.assertEqual(leftovers, [])

    def test_stage_with_manifest_at_zip_root(self) -> None:
        report = ext.stage(self.dest, asset_url=self.local_asset(nested=None))
        self.assertTrue((Path(report["path"]) / "manifest.json").is_file())

    def test_restage_same_version_is_idempotent(self) -> None:
        url = self.local_asset()
        first = ext.stage(self.dest, asset_url=url)
        second = ext.stage(self.dest, asset_url=url)
        self.assertEqual(second["status"], "already-staged")
        self.assertEqual(second["path"], first["path"])
        forced = ext.stage(self.dest, asset_url=url, force=True)
        self.assertEqual(forced["status"], "staged")

    def test_zip_slip_member_is_rejected(self) -> None:
        archive = self.base / "opencli-extension-v9.9.9.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("../evil.txt", "escape")
            bundle.writestr("manifest.json", "{}")
        with self.assertRaises(ext.ExtensionError):
            ext.stage(self.dest, asset_url=archive.resolve().as_uri())
        self.assertFalse((self.base / "evil.txt").exists())
        self.assertFalse((self.dest / "current.json").exists())

    def test_zip_without_manifest_errors(self) -> None:
        archive = self.base / "opencli-extension-v1.0.0.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("readme.txt", "no manifest here")
        with self.assertRaises(ext.ExtensionError):
            ext.stage(self.dest, asset_url=archive.resolve().as_uri())

    def test_ambiguous_manifest_layout_errors(self) -> None:
        archive = self.base / "opencli-extension-v1.0.0.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("one/manifest.json", "{}")
            bundle.writestr("two/manifest.json", "{}")
        with self.assertRaises(ext.ExtensionError):
            ext.stage(self.dest, asset_url=archive.resolve().as_uri())

    def test_asset_url_with_unofficial_basename_is_rejected(self) -> None:
        archive = self.base / "bundle.zip"
        make_extension_zip(archive)
        with self.assertRaises(ext.ExtensionError):
            ext.stage(self.dest, asset_url=archive.resolve().as_uri())

    def test_status_before_and_after_staging(self) -> None:
        before = ext.status_report(self.dest)
        self.assertEqual(before["status"], "not-staged")
        ext.stage(self.dest, asset_url=self.local_asset())
        after = ext.status_report(self.dest)
        self.assertEqual(after["status"], "staged")
        self.assertEqual(after["version"], "1.0.22")

    def test_status_detects_deleted_staged_folder(self) -> None:
        report = ext.stage(self.dest, asset_url=self.local_asset())
        import shutil

        shutil.rmtree(report["path"])
        self.assertEqual(ext.status_report(self.dest)["status"], "not-staged")

    def test_cli_status_exit_code_signals_action_required(self) -> None:
        self.assertEqual(ext.main(["--status", "--json", "--dest", str(self.dest)]), 3)
        ext.stage(self.dest, asset_url=self.local_asset())
        self.assertEqual(ext.main(["--status", "--dest", str(self.dest)]), 0)


if __name__ == "__main__":
    unittest.main()
