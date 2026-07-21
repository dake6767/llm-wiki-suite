#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts import managed_components


class ManagedComponentTests(unittest.TestCase):
    def manifest(self, asset: Path) -> dict:
        spec = {
            "version": "1.0",
            "asset": asset.name,
            "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
            "size": asset.stat().st_size,
            "installed_size": 2,
        }
        return {
            "schema": 2,
            "protocol": 5,
            "release_tag": "v-test",
            "platform": "darwin",
            "architecture": "arm64",
            "sources": ["https://example.invalid/{tag}/{asset}"],
            "runtime": spec,
            "components": {"documents": {**spec, "tools": {}}},
        }

    def test_manifest_rejects_wrong_platform_and_unsafe_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "pack.zip"
            with zipfile.ZipFile(asset, "w") as bundle:
                bundle.writestr("file", "ok")
            value = self.manifest(asset)
            with mock.patch.object(managed_components, "platform_id", return_value=("darwin", "arm64")):
                managed_components.validate_manifest(value)
                value["components"]["documents"]["asset"] = "../pack.zip"
                with self.assertRaisesRegex(managed_components.ComponentError, "unsafe"):
                    managed_components.validate_manifest(value)

    def test_manifest_validates_multipart_total_and_unique_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "pack.zip"
            with zipfile.ZipFile(asset, "w") as bundle:
                bundle.writestr("file", "ok")
            value = self.manifest(asset)
            component = value["components"]["documents"]
            raw = asset.read_bytes()
            midpoint = len(raw) // 2
            chunks = (raw[:midpoint], raw[midpoint:])
            component["parts"] = [
                {
                    "asset": f"pack.zip.part{index:03d}",
                    "sha256": hashlib.sha256(chunk).hexdigest(),
                    "size": len(chunk),
                }
                for index, chunk in enumerate(chunks, 1)
            ]
            with mock.patch.object(managed_components, "platform_id", return_value=("darwin", "arm64")):
                managed_components.validate_manifest(value)
                component["parts"][1]["asset"] = component["parts"][0]["asset"]
                with self.assertRaisesRegex(managed_components.ComponentError, "unsafe"):
                    managed_components.validate_manifest(value)

    def test_manifest_requires_oversized_release_assets_to_be_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "pack.zip"
            asset.write_bytes(b"pack")
            value = self.manifest(asset)
            value["components"]["documents"]["size"] = (
                managed_components.MAX_RELEASE_PART_BYTES + 1
            )
            with mock.patch.object(managed_components, "platform_id", return_value=("darwin", "arm64")):
                with self.assertRaisesRegex(managed_components.ComponentError, "must be split"):
                    managed_components.validate_manifest(value)

    def test_safe_extract_rejects_traversal_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../outside", "bad")
            with self.assertRaisesRegex(managed_components.ComponentError, "unsafe"):
                managed_components._safe_extract(archive, root / "out")
            self.assertFalse((root / "outside").exists())

    def test_activation_rejects_expanded_size_mismatch_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "pack.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("payload", "four")
            spec = {
                "version": "1",
                "asset": archive.name,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "size": archive.stat().st_size,
                "installed_size": 5,
            }
            target = root / "components" / "documents" / "1"
            with self.assertRaisesRegex(managed_components.ComponentError, "expanded size"):
                managed_components._activate_archive(archive, target, spec, "component")
            self.assertFalse(target.exists())

    def test_local_asset_requires_exact_size_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = root / "pack.zip"
            asset.write_bytes(b"pack")
            spec = {
                "version": "1",
                "asset": asset.name,
                "size": 4,
                "sha256": hashlib.sha256(b"pack").hexdigest(),
                "installed_size": 4,
            }
            plan = {"release_tag": "v", "sources": []}
            with mock.patch.dict(
                managed_components.os.environ,
                {"LLM_WIKI_COMPONENT_ASSET_DIR": str(root)},
            ):
                self.assertEqual(managed_components._download(spec, plan, root / "home"), asset)
                spec["sha256"] = "0" * 64
                with self.assertRaisesRegex(managed_components.ComponentError, "verification"):
                    managed_components._download(spec, plan, root / "home")

    def test_multipart_local_assets_are_streamed_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunks = (b"first-part", b"second-part")
            content = b"".join(chunks)
            parts = []
            for index, chunk in enumerate(chunks, 1):
                path = root / f"pack.zip.part{index:03d}"
                path.write_bytes(chunk)
                parts.append(
                    {
                        "asset": path.name,
                        "sha256": hashlib.sha256(chunk).hexdigest(),
                        "size": len(chunk),
                    }
                )
            spec = {
                "version": "1",
                "asset": "pack.zip",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "installed_size": 1,
                "parts": parts,
            }
            plan = {"release_tag": "v", "sources": []}
            with mock.patch.dict(
                managed_components.os.environ,
                {"LLM_WIKI_COMPONENT_ASSET_DIR": str(root)},
            ):
                assembled = managed_components._download(spec, plan, root / "home")
            self.assertEqual(assembled.read_bytes(), content)
            self.assertTrue(all((root / row["asset"]).is_file() for row in parts))

    def test_remove_owned_refuses_unmarked_component(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            component = home / "components" / "documents" / "1"
            component.mkdir(parents=True)
            (component / "user-file").write_text("keep", encoding="utf-8")
            receipt = {
                "components": {
                    "documents": {
                        "version": "1",
                        "path": str(component),
                        "sha256": "0" * 64,
                    }
                }
            }
            self.assertEqual(managed_components.remove_owned(receipt, home), [])
            self.assertTrue((component / "user-file").is_file())

    def test_install_rejects_insufficient_disk_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            spec = {
                "id": "documents",
                "version": "1",
                "asset": "documents.zip",
                "size": 40,
                "installed_size": 60,
                "sha256": "0" * 64,
                "tools": {},
            }
            plan = {
                "release_tag": "v",
                "sources": [],
                "runtime": None,
                "items": [spec],
            }
            usage = shutil._ntuple_diskusage(total=1000, used=950, free=50)
            with mock.patch.object(managed_components.shutil, "disk_usage", return_value=usage), \
                    mock.patch.object(managed_components, "_download") as download:
                with self.assertRaisesRegex(managed_components.ComponentError, "insufficient"):
                    managed_components.install_selected(plan, home)
            download.assert_not_called()

    def test_disk_preflight_accounts_for_multipart_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            plan = {
                "runtime": None,
                "items": [
                    {
                        "id": "asr-zh",
                        "version": "1",
                        "size": 20,
                        "installed_size": 30,
                        "parts": [{"asset": "one"}, {"asset": "two"}],
                    }
                ],
            }
            self.assertEqual(
                managed_components._required_space(plan, home),
                {
                    "download_bytes": 20,
                    "assembly_bytes": 20,
                    "installed_bytes": 30,
                    "required_bytes": 70,
                },
            )


if __name__ == "__main__":
    unittest.main()
