from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import build_distribution as builder
from scripts import build_updater_manifest as updater
from scripts import compose_distribution as composer
from scripts import merge_pack_indexes as merger
from scripts import pack_release


class DistributionBuilderTests(unittest.TestCase):
    def test_pack_input_digest_canonicalizes_checkout_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lf = root / "lf.txt"
            crlf = root / "crlf.txt"
            lf.write_bytes(b"one\ntwo\n")
            crlf.write_bytes(b"one\r\ntwo\r\n")

            self.assertEqual(
                pack_release.canonical_input_bytes(lf),
                pack_release.canonical_input_bytes(crlf),
            )

    def test_normalizes_generated_python_install_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary)
            cache = site / "package/__pycache__"
            scripts = site / "bin"
            metadata = site / "package-1.0.dist-info"
            cache.mkdir(parents=True)
            scripts.mkdir()
            metadata.mkdir()
            (cache / "module.pyc").write_bytes(b"temporary bytecode")
            (scripts / "package-cli").write_text("#!temporary/python\n")
            record = metadata / "RECORD"
            record.write_text(
                "../../bin/package-cli,sha256=temporary,1\r\n"
                "package/__pycache__/module.pyc,sha256=temporary,1\r\n"
                "package/module.py,sha256=stable,1\r\n"
                "package-1.0.dist-info/RECORD,,\r\n"
            )

            builder.normalize_python_install(site)

            self.assertFalse(cache.exists())
            self.assertFalse(scripts.exists())
            self.assertEqual(
                record.read_text(),
                "package/module.py,sha256=stable,1\n"
                "package-1.0.dist-info/RECORD,,\n",
            )

    def test_github_release_asset_payload_shapes(self) -> None:
        rows = [{"name": "bundle"}]
        self.assertEqual(updater.asset_rows(rows), rows)
        self.assertEqual(updater.asset_rows({"assets": rows}), rows)
        self.assertEqual(pack_release.asset_rows(rows), rows)
        self.assertEqual(pack_release.asset_rows({"assets": rows}), rows)
        with self.assertRaises(ValueError):
            updater.asset_rows({"assets": {}})
        with self.assertRaises(pack_release.PackReleaseError):
            pack_release.asset_rows({"assets": {}})

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
                "2.0.6",
                commands={"tool": ["{pack}/tool"]},
            )
            archive = root / "dist" / spec["asset"]
            self.assertEqual(
                archive.name,
                "My-LLM-Wiki-toolchain-base_2.0.6_darwin_arm64.zip",
            )
            self.assertEqual(spec["sha256"], builder.sha256(archive))
            self.assertEqual(spec["size"], archive.stat().st_size)
            self.assertEqual(spec["installed_size"], 4)

    def test_client_probes_only_start_lightweight_runtimes(self) -> None:
        probes = builder.toolchain_client_probes()
        self.assertEqual(
            probes,
            [
                {"command": "python-runtime", "args": ["--version"]},
                {"command": "node-runtime", "args": ["--version"]},
                {"command": "ffmpeg", "args": ["-version"]},
            ],
        )
        self.assertEqual(
            builder.python_client_probe(),
            [{"command": "python-runtime", "args": ["--version"]}],
        )
        managed_apps = {
            "markitdown",
            "opencli",
            "yt-dlp",
            "asr-zh-postcheck",
            "asr-other-postcheck",
        }
        self.assertTrue(managed_apps.isdisjoint(row["command"] for row in probes))

    def test_pack_spec_deep_checks_the_final_extracted_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            stage.mkdir()
            executable = stage / "probe"
            executable.write_text("verified", encoding="utf-8")
            executable.chmod(0o755)
            observed: list[list[str]] = []

            def verify(argv: list[str], **_kwargs: object) -> str:
                observed.append(argv)
                extracted = Path(argv[0])
                self.assertTrue(extracted.is_file())
                self.assertEqual(extracted.read_text(encoding="utf-8"), "verified")
                self.assertNotEqual(extracted, executable)
                return ""

            with mock.patch.object(builder, "checked", side_effect=verify):
                builder.pack_spec(
                    stage,
                    root / "dist",
                    "toolchain-base",
                    "darwin",
                    "arm64",
                    "2.0.6",
                    commands={"deep": ["{pack}/probe"]},
                    release_checks=[{"command": "deep", "args": ["--check"]}],
                )

            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0][1:], ["--check"])

    def test_linux_asr_uses_the_official_cpu_wheels(self) -> None:
        spec = {
            "packages": ["torch==2.11.0", "torchaudio==2.11.0"],
            "linux": {
                "extra_index_url": "https://download.pytorch.org/whl/cpu",
                "packages": ["torch==2.11.0+cpu", "torchaudio==2.11.0+cpu"],
            },
        }

        linux_packages, linux_index = builder.posix_asr_install(spec, "linux")
        darwin_packages, darwin_index = builder.posix_asr_install(spec, "darwin")

        self.assertEqual(
            linux_packages, ["torch==2.11.0+cpu", "torchaudio==2.11.0+cpu"]
        )
        self.assertEqual(linux_index, "https://download.pytorch.org/whl/cpu")
        self.assertEqual(darwin_packages, spec["packages"])
        self.assertIsNone(darwin_index)

    def test_asr_rejects_unapproved_package_index(self) -> None:
        spec = {
            "packages": ["torch==2.11.0"],
            "linux": {
                "extra_index_url": "https://packages.example.invalid/simple",
                "packages": ["torch==2.11.0+cpu"],
            },
        }

        with self.assertRaisesRegex(builder.BuildError, "unsupported ASR package index"):
            builder.posix_asr_install(spec, "linux")

    def test_release_asset_size_limit_is_enforced_before_upload(self) -> None:
        builder.validate_release_asset_size(
            "asr-zh", builder.MAX_RELEASE_ASSET_SIZE - 1
        )
        with self.assertRaisesRegex(builder.BuildError, "GitHub release asset limit"):
            builder.validate_release_asset_size(
                "asr-zh", builder.MAX_RELEASE_ASSET_SIZE
            )

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
                    "windows",
                    "x64",
                )

            self.assertTrue((stage / "THIRD-PARTY-NOTICES.txt").is_file())
            self.assertFalse((stage / "asr-zh").exists())

    def test_merge_rejects_duplicate_platform_pack(self) -> None:
        metadata = {
            "schema": 1,
            "version": "2.0.6",
            "input_sha256": "a" * 64,
        }
        row = {
            "schema": 1,
            "pack_version": "2.0.6",
            "input_sha256": "a" * 64,
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
                merger.merge(inputs, metadata)

    def test_distribution_uses_an_independent_pack_version(self) -> None:
        metadata = {
            "schema": 1,
            "version": "2.0.6",
            "input_sha256": "b" * 64,
        }
        artifacts = []
        for pack_id, platform, architecture in sorted(
            pack_release.expected_identities()
        ):
            name = pack_release.asset_name(
                pack_id, metadata["version"], platform, architecture
            )
            tag = pack_release.pack_tag(metadata["version"])
            artifacts.append(
                {
                    "id": pack_id,
                    "version": metadata["version"],
                    "platform": platform,
                    "architecture": architecture,
                    "sha256": "c" * 64,
                    "size": 1,
                    "installed_size": 1,
                    "urls": [
                        f"https://wiki.htmlgo.to/_update/dl/{tag}/{name}",
                        f"https://github.com/dake6767/llm-wiki-suite/releases/download/{tag}/{name}",
                    ],
                }
            )
        index = {
            "schema": 1,
            "pack_version": metadata["version"],
            "input_sha256": metadata["input_sha256"],
            "artifacts": artifacts,
        }

        distribution = composer.compose(index, "2.1.0", metadata)

        self.assertEqual(distribution["distribution_version"], "2.1.0")
        self.assertEqual(distribution["browser_version"], "2.1.0")
        self.assertEqual(distribution["skills_pack_version"], "2.1.0")
        self.assertEqual(distribution["pack_version"], "2.0.6")
        self.assertTrue(
            all(row["version"] == "2.0.6" for row in distribution["artifacts"])
        )

    def test_updater_manifest_is_assembled_once_from_exact_bundle_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            signatures = Path(temporary)
            names = set()
            for bundle, _keys in updater.bundle_layout("2.0.7").values():
                names.update({bundle, f"{bundle}.sig"})
                (signatures / f"{bundle}.sig").write_bytes(
                    f"signature:{bundle}".encode()
                )

            manifest = updater.build(
                "v2.0.7",
                "dake6767/llm-wiki-suite",
                names,
                signatures,
                "2026-07-24T00:00:00.000Z",
            )

        self.assertEqual(manifest["version"], "2.0.7")
        self.assertEqual(
            set(manifest["platforms"]),
            {
                "darwin-aarch64",
                "darwin-aarch64-app",
                "darwin-x86_64",
                "darwin-x86_64-app",
                "linux-x86_64",
                "linux-x86_64-appimage",
                "linux-x86_64-deb",
                "windows-x86_64",
                "windows-x86_64-nsis",
            },
        )
        linux = manifest["platforms"]["linux-x86_64"]
        self.assertEqual(
            linux["signature"],
            "signature:My.LLM.Wiki.Browser_2.0.7_amd64.AppImage",
        )

    def test_pack_release_metadata_covers_every_hashed_lock(self) -> None:
        metadata = pack_release.load_metadata()
        self.assertEqual(metadata["input_sha256"], pack_release.input_sha256())
        locks = sorted((builder.REQUIREMENTS).glob("*.txt"))
        self.assertEqual(len(locks), 14)
        for lock in locks:
            body = lock.read_text(encoding="utf-8")
            self.assertIn("==", body, lock.name)
            self.assertIn("--hash=sha256:", body, lock.name)


if __name__ == "__main__":
    unittest.main()
