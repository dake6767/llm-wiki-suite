from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import build_distribution as builder
from scripts import merge_distribution_manifests as merger


class DistributionBuilderTests(unittest.TestCase):
    def test_target_names_are_manifest_stable(self) -> None:
        with mock.patch.object(builder.platform, "system", return_value="Darwin"), mock.patch.object(
            builder.platform, "machine", return_value="arm64"
        ):
            self.assertEqual(builder.target(), ("darwin", "arm64"))
        with mock.patch.object(builder.platform, "system", return_value="Windows"), mock.patch.object(
            builder.platform, "machine", return_value="AMD64"
        ):
            self.assertEqual(builder.target(), ("windows", "x64"))

    def test_pack_spec_hashes_the_exact_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            stage.mkdir()
            executable = stage / "tool"
            executable.write_text("tool", encoding="utf-8")
            executable.chmod(0o755)
            spec = builder.pack_spec(
                stage,
                root / "dist",
                "toolchain-base",
                "darwin",
                "arm64",
                commands={"tool": ["{pack}/tool"]},
            )
            archive = root / "dist" / spec["asset"]
            self.assertEqual(spec["sha256"], builder.sha256(archive))
            self.assertEqual(spec["size"], archive.stat().st_size)
            self.assertEqual(spec["installed_size"], 4)

    def test_windows_asr_notice_is_written_at_pack_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def fake_extract(_archive: Path, destination: Path) -> None:
                destination.mkdir(parents=True)

            with mock.patch.object(builder, "extract", side_effect=fake_extract), mock.patch.object(
                builder, "enable_embedded_python"
            ), mock.patch.object(builder, "pip_target"), mock.patch.object(builder, "checked"):
                stage = builder.build_windows_asr(
                    "asr-zh",
                    {"packages": ["example==1"], "postcheck": ["-c", "pass"]},
                    root / "python.zip",
                    root,
                )

            self.assertTrue((stage / "THIRD-PARTY-NOTICES.txt").is_file())
            self.assertFalse((stage / "asr-zh").exists())

    def test_merge_rejects_duplicate_platform_pack(self) -> None:
        row = {
            "schema": 1,
            "channel": "stable",
            "distribution_version": "2.0.0",
            "browser_version": "2.0.0",
            "skills_pack_version": "2.0.0",
            "artifacts": [{"id": "toolchain-base", "platform": "linux", "architecture": "x64"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = []
            for index in range(2):
                path = root / f"part-{index}.json"
                path.write_text(json.dumps(row), encoding="utf-8")
                inputs.append(path)
            with self.assertRaisesRegex(merger.MergeError, "duplicate"):
                merger.merge(inputs)


if __name__ == "__main__":
    unittest.main()
