#!/usr/bin/env python3
"""Unit and integration tests for the Windows-native Setup controller."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import uuid
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

    def test_ensure_data_root_links_home_and_wikis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile"
            profile.mkdir()
            home_link = profile / ".my-llm-wiki"
            wikis_link = profile / "wikis"
            data_root = root / "drive-d" / "MyLLMWiki"
            with unittest.mock.patch.object(
                windows_setup, "DEFAULT_WIKIS", str(wikis_link)
            ):
                windows_setup.ensure_data_root(home_link, data_root)
                (home_link / "probe.txt").write_text("x", encoding="utf-8")
                self.assertTrue((data_root / "home" / "probe.txt").is_file())
                self.assertTrue((data_root / "wikis").is_dir())
                windows_setup.ensure_data_root(home_link, data_root)  # idempotent rerun
                self.assertTrue((home_link / "probe.txt").is_file())

    def test_ensure_data_root_refuses_existing_profile_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile"
            home_link = profile / ".my-llm-wiki"
            home_link.mkdir(parents=True)
            (home_link / "existing.txt").write_text("x", encoding="utf-8")
            with unittest.mock.patch.object(
                windows_setup, "DEFAULT_WIKIS", str(profile / "wikis")
            ):
                with self.assertRaisesRegex(windows_setup.SetupError, "already holds data"):
                    windows_setup.ensure_data_root(home_link, root / "drive-d")
            self.assertTrue((home_link / "existing.txt").is_file())

    def test_ensure_data_root_refuses_foreign_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile"
            profile.mkdir()
            home_link = profile / ".my-llm-wiki"
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            windows_setup.create_directory_link(home_link, elsewhere)
            with unittest.mock.patch.object(
                windows_setup, "DEFAULT_WIKIS", str(profile / "wikis")
            ):
                with self.assertRaisesRegex(windows_setup.SetupError, "already links"):
                    windows_setup.ensure_data_root(home_link, root / "drive-d")

    def test_cleanup_stale_workdirs_removes_only_uuid_leftovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            versions = home / "components" / "video" / "versions"
            versions.mkdir(parents=True)
            keep = versions / "2026.07.04+ffmpeg.8.1.2"
            keep.mkdir()
            stale = versions / f".2026.07.04+ffmpeg.8.1.2.{'a' * 32}.staging"
            stale.mkdir()
            (stale / "big.bin").write_bytes(b"x" * 128)
            downloads = home / windows_setup.SETUP_DIR / "downloads"
            downloads.mkdir(parents=True)
            part = downloads / f".pack.zip.{'b' * 32}.part"
            part.write_bytes(b"x")
            cached = downloads / (("c" * 64) + "-pack.zip")
            cached.write_bytes(b"x")
            windows_setup.cleanup_stale_workdirs(home)
            self.assertTrue(keep.is_dir())
            self.assertTrue(cached.is_file())
            self.assertFalse(stale.exists())
            self.assertFalse(part.exists())

    def test_preflight_disk_space_blocks_when_short(self) -> None:
        manifest = {
            "components": {
                "video": {"version": "1.0", "size": 200 * 1024 * 1024},
            }
        }
        usage = type("Usage", (), {"free": 500 * 1024 * 1024})()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with unittest.mock.patch.object(
                windows_setup.shutil, "disk_usage", return_value=usage
            ):
                with self.assertRaisesRegex(windows_setup.SetupError, "disk space"):
                    windows_setup.preflight_disk_space(home, manifest, ["video"])
            marker = home / "components" / "video" / "versions" / "1.0" / ".llm-wiki-component.json"
            marker.parent.mkdir(parents=True)
            marker.write_text("{}", encoding="utf-8")
            with unittest.mock.patch.object(
                windows_setup.shutil, "disk_usage", return_value=usage
            ):
                windows_setup.preflight_disk_space(home, manifest, ["video"])

    def test_python_file_command_restores_sibling_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp)
            (scripts / "helper_mod.py").write_text("VALUE = 42\n", encoding="utf-8")
            main = scripts / "main.py"
            main.write_text(
                "import sys\nimport helper_mod\nprint(helper_mod.VALUE, sys.argv[1])\n",
                encoding="utf-8",
            )
            command = windows_setup.python_file_command(sys.executable, [str(main), "tail"])
            self.assertEqual(command[1], "-c")
            # -I drops the script directory from sys.path exactly like the
            # embedded ._pth runtime; the plain file form must fail there
            # while the bootstrapped command succeeds.
            plain = subprocess.run(
                [sys.executable, "-I", str(main), "tail"],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(plain.returncode, 0)
            isolated = [command[0], "-I", *command[1:]]
            result = subprocess.run(isolated, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "42 tail")

    def test_python_file_command_passthrough_and_msys_paths(self) -> None:
        self.assertEqual(
            windows_setup.python_file_command("py", ["-c", "print(1)"]),
            ["py", "-c", "print(1)"],
        )
        self.assertEqual(
            windows_setup.python_file_command("py", ["C:/nope/x.py", "a"]),
            ["py", "C:/nope/x.py", "a"],
        )
        self.assertEqual(windows_setup.msys_to_windows_path("/c/Users/x"), "C:/Users/x")
        self.assertEqual(windows_setup.msys_to_windows_path("/D"), "D:/")
        self.assertIsNone(windows_setup.msys_to_windows_path("relative/x.py"))
        self.assertIsNone(windows_setup.msys_to_windows_path("C:/Users/x.py"))

    def test_run_doctor_capture_uses_receipt_and_returns_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(
                windows_setup.run_doctor_capture(home),
                (2, "Setup receipt is missing; run the install first."),
            )
            receipt = {
                "schema": 1,
                "platform": "windows",
                "home": str(home),
                "suite": str(home / "suite" / "versions" / "1.0"),
                "runtime": str(home / "runtime" / "python"),
                "hosts": ["claude"],
            }
            windows_setup.write_receipt(home, receipt)
            with unittest.mock.patch.object(windows_setup.subprocess, "run") as run:
                run.return_value = unittest.mock.Mock(
                    returncode=3, stdout="doctor says\n", stderr="warn\n"
                )
                code, output = windows_setup.run_doctor_capture(home)
            self.assertEqual(code, 3)
            self.assertIn("doctor says", output)
            self.assertIn("warn", output)
            argv = run.call_args[0][0]
            self.assertIn("--host", argv)
            self.assertIn("claude", argv)

    def test_installed_browser_executable_follows_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            self.assertIsNone(windows_setup.installed_browser_executable(home))
            suite = home / "suite" / "versions" / "1.0"
            (suite / "registry").mkdir(parents=True)
            browser_receipt = Path(tmp) / "browser-receipt.json"
            (suite / "registry" / "bootstrap.json").write_text(json.dumps({
                "browser": {"install_receipt": {"path": str(browser_receipt)}},
            }), encoding="utf-8")
            windows_setup.write_receipt(home, {
                "schema": 1,
                "platform": "windows",
                "home": str(home),
                "suite": str(suite),
            })
            self.assertIsNone(windows_setup.installed_browser_executable(home))
            exe = Path(tmp) / "Browser" / "browser.exe"
            exe.parent.mkdir()
            exe.write_bytes(b"MZ")
            browser_receipt.write_text(
                json.dumps({"target": str(exe)}), encoding="utf-8"
            )
            self.assertEqual(windows_setup.installed_browser_executable(home), exe)

    def test_browser_bridge_extension_dir_reads_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertIsNone(windows_setup.browser_bridge_extension_dir(home))
            component = home / "components" / "web" / "versions" / "1.0"
            (component / "extension").mkdir(parents=True)
            windows_setup.write_receipt(home, {
                "schema": 1,
                "platform": "windows",
                "home": str(home),
                "components": {"web": {"path": str(component)}},
            })
            self.assertEqual(
                windows_setup.browser_bridge_extension_dir(home),
                component / "extension",
            )

    def test_install_browser_app_runs_suite_installer_silently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            suite = Path(tmp) / "suite"
            runtime = Path(tmp) / "runtime"
            script = suite / "scripts" / "install-browser.py"
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")
            with unittest.mock.patch.object(windows_setup.subprocess, "run") as run:
                run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
                windows_setup.install_browser_app(suite, runtime)
                argv = run.call_args[0][0]
                self.assertEqual(argv[0], str(runtime / "python.exe"))
                self.assertEqual(argv[1], str(script))
                self.assertIn("--windows-silent", argv)
                run.return_value = unittest.mock.Mock(returncode=1, stdout="", stderr="boom")
                with self.assertRaisesRegex(windows_setup.SetupError, "exited with 1"):
                    windows_setup.install_browser_app(suite, runtime)

    def test_download_component_reports_progress(self) -> None:
        data = b"payload-bytes" * 1024
        digest = hashlib.sha256(data).hexdigest()

        class FakeResponse:
            def __init__(self) -> None:
                self.headers = {"Content-Length": str(len(data))}
                self._chunks = [data]

            def read(self, _size: int) -> bytes:
                return self._chunks.pop(0) if self._chunks else b""

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args) -> bool:
                return False

        seen: list[dict] = []
        spec = {"asset": "pack.zip", "sha256": digest}
        manifest = {"release_tag": "v1", "sources": ["https://example.invalid/{tag}/{asset}"]}
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            windows_setup.set_progress_hook(seen.append)
            try:
                with unittest.mock.patch.object(
                    windows_setup.urllib.request, "urlopen", return_value=FakeResponse()
                ):
                    cache = windows_setup.download_component(
                        "video", spec, manifest, home, None
                    )
            finally:
                windows_setup.set_progress_hook(None)
            self.assertEqual(cache.read_bytes(), data)
        phases = [info["phase"] for info in seen if "phase" in info]
        self.assertTrue(any("正在下载 video" in phase for phase in phases))
        finals = [info for info in seen if info.get("received") == len(data)]
        self.assertTrue(finals)
        self.assertEqual(finals[-1]["total"], len(data))

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
            guidance: list[str] = []
            status = windows_setup.install_flow(
                hosts=["codex"],
                components=[],
                home=home,
                payload=payload,
                asset_dir=None,
                allow_test_platform=True,
                skip_postcheck=True,
                guidance=guidance,
            )
            self.assertEqual(status, 0)
            self.assertTrue(any("知识库 root" in line for line in guidance))
            self.assertTrue(any("my-llm-wiki 技能" in line for line in guidance))
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


class WindowedExecutableHelperTests(unittest.TestCase):
    def test_hidden_console_flags_match_platform(self) -> None:
        flags = windows_setup.hidden_console_flags()
        if os.name != "nt":
            self.assertEqual(flags, 0)
        else:
            self.assertIn(flags, (0, subprocess.CREATE_NO_WINDOW))

    def test_attach_parent_console_keeps_usable_streams(self) -> None:
        before = (sys.stdout, sys.stderr, sys.stdin)
        windows_setup.attach_parent_console()
        self.assertEqual((sys.stdout, sys.stderr, sys.stdin), before)

    def test_dpi_awareness_is_noop_off_windows(self) -> None:
        windows_setup.enable_windows_dpi_awareness()

    def test_chrome_executable_is_windows_only(self) -> None:
        if os.name != "nt":
            self.assertIsNone(windows_setup.chrome_executable())

    def test_component_choices_fall_back_to_english_labels(self) -> None:
        manifest = {"components": {"demo": {"label": "Demo", "description": "d"}}}
        row = windows_setup.component_choices(manifest)[0]
        self.assertEqual(row["label_zh"], "Demo")
        self.assertEqual(row["description_zh"], "d")
        manifest["components"]["demo"]["label_zh"] = "示例"
        row = windows_setup.component_choices(manifest)[0]
        self.assertEqual(row["label_zh"], "示例")


def _write_pe(path, machine: int) -> None:
    header_offset = 0x40
    blob = bytearray(b"MZ")
    blob += b"\0" * (0x3C - len(blob))
    blob += header_offset.to_bytes(4, "little")
    blob += b"PE\0\0"
    blob += machine.to_bytes(2, "little")
    path.write_bytes(bytes(blob))


class VersionDirTests(unittest.TestCase):
    # Versions shipped by component-manifest.json as of v1.0.27.
    SHIPPED = {
        "documents": "0.1.6",
        "web": "1.8.6+ext.1.0.22",
        "video": "2026.07.04+ffmpeg.8.1.2",
        "asr-other": "faster-whisper.1.2.1",
    }
    ASR_ZH = (
        "funasr.1.3.16+torch.2.11.0+torchaudio.2.11.0"
        "+librosa.0.11.0+modelscope.1.38.1"
    )
    # Deepest member inside My-LLM-Wiki-ASR-ZH_x64.zip.
    DEEPEST_MEMBER = 152

    def test_short_versions_keep_their_exact_directory(self) -> None:
        # Renaming these would strand every already-installed component tree.
        for component, version in self.SHIPPED.items():
            with self.subTest(component=component):
                self.assertEqual(windows_setup.version_dir_name(version), version)

    def test_long_version_collapses_deterministically(self) -> None:
        name = windows_setup.version_dir_name(self.ASR_ZH)
        self.assertNotEqual(name, self.ASR_ZH)
        self.assertLessEqual(len(name), windows_setup.VERSION_DIR_MAX)
        self.assertEqual(name, windows_setup.version_dir_name(self.ASR_ZH))
        other = self.ASR_ZH.replace("funasr.1.3.16", "funasr.1.3.17")
        self.assertNotEqual(windows_setup.version_dir_name(other), name)

    def test_asr_zh_tree_fits_under_max_path(self) -> None:
        # The v1.0.27 failure: the full version name pushed extraction past
        # MAX_PATH, so Setup aborted with ENOENT partway through asr-zh.
        home = "C:\\Users\\Administrator\\.my-llm-wiki"
        root = (
            f"{home}\\components\\asr-zh\\versions\\"
            f"{windows_setup.version_dir_name(self.ASR_ZH)}"
        )
        self.assertLess(len(root) + 1 + self.DEEPEST_MEMBER, 260)

    def test_staging_names_stay_collectable_as_stale_workdirs(self) -> None:
        staging = f".{uuid.uuid4().hex}.staging"
        self.assertTrue(staging.startswith("."))
        self.assertTrue(windows_setup.STALE_HEX.search(staging))


class LongPathTests(unittest.TestCase):
    def test_windows_paths_get_extended_length_prefix(self) -> None:
        with unittest.mock.patch.object(windows_setup.os, "name", "nt"), \
                unittest.mock.patch.object(
                    windows_setup.os.path, "abspath", side_effect=lambda p: p
                ):
            self.assertEqual(
                windows_setup.long_path(Path("C:\\Users\\L\\x")),
                "\\\\?\\C:\\Users\\L\\x",
            )
            self.assertEqual(
                windows_setup.long_path(Path("\\\\server\\share\\x")),
                "\\\\?\\UNC\\server\\share\\x",
            )
            self.assertEqual(
                windows_setup.long_path(Path("\\\\?\\C:\\already")),
                "\\\\?\\C:\\already",
            )

    def test_posix_paths_are_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            if os.name != "nt":
                self.assertEqual(windows_setup.long_path(Path(tmp)), tmp)

    def test_remove_tree_deletes_a_populated_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "tree"
            (target / "a" / "b").mkdir(parents=True)
            (target / "a" / "b" / "f.txt").write_text("x", encoding="utf-8")
            windows_setup.remove_tree(target)
            self.assertFalse(target.exists())


class PlatformValidationTests(unittest.TestCase):
    def test_pe_machine_reads_machine_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "fake.exe"
            _write_pe(exe, windows_setup.IMAGE_FILE_MACHINE_AMD64)
            self.assertEqual(
                windows_setup.pe_machine(str(exe)),
                windows_setup.IMAGE_FILE_MACHINE_AMD64,
            )
            _write_pe(exe, windows_setup.IMAGE_FILE_MACHINE_ARM64)
            self.assertEqual(
                windows_setup.pe_machine(str(exe)),
                windows_setup.IMAGE_FILE_MACHINE_ARM64,
            )

    def test_pe_machine_rejects_non_pe_and_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            not_pe = Path(tmp) / "not_pe.bin"
            not_pe.write_bytes(b"#!/bin/sh\n")
            self.assertIsNone(windows_setup.pe_machine(str(not_pe)))
            self.assertIsNone(windows_setup.pe_machine(str(Path(tmp) / "absent.exe")))

    def test_validate_platform_accepts_x64_pe_self_check_on_arm64(self) -> None:
        # The real-device case: platform.machine() says ARM64, both WOW64
        # signals miss, but the executing image itself is an x64 PE.
        with unittest.mock.patch.object(
            windows_setup.platform, "system", return_value="Windows"
        ), unittest.mock.patch.object(
            windows_setup.platform, "machine", return_value="ARM64"
        ), unittest.mock.patch.object(
            windows_setup, "running_x64_build", return_value=True
        ), unittest.mock.patch.dict(os.environ, {}, clear=True):
            windows_setup.validate_platform()

    def test_platform_error_carries_probe_diagnostics(self) -> None:
        with unittest.mock.patch.object(
            windows_setup.platform, "system", return_value="Windows"
        ), unittest.mock.patch.object(
            windows_setup.platform, "machine", return_value="ARM64"
        ), unittest.mock.patch.object(
            windows_setup, "running_x64_build", return_value=False
        ), unittest.mock.patch.object(
            windows_setup, "wow64_process2_probe", return_value=(False, 0, 0)
        ), unittest.mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                windows_setup.SetupError, r"iswow64process2=fail"
            ):
                windows_setup.validate_platform()

    def test_emulation_probe_ignores_non_arm64_wow64_env(self) -> None:
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(windows_setup.x64_emulation_on_arm64())
        with unittest.mock.patch.dict(os.environ, {"PROCESSOR_ARCHITEW6432": "AMD64"}):
            self.assertFalse(windows_setup.x64_emulation_on_arm64())

    def test_emulation_probe_accepts_arm64_wow64_env(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"PROCESSOR_ARCHITEW6432": "ARM64"}):
            self.assertTrue(windows_setup.x64_emulation_on_arm64())

    def test_validate_platform_accepts_x64_emulation_on_arm64(self) -> None:
        with unittest.mock.patch.object(
            windows_setup.platform, "system", return_value="Windows"
        ), unittest.mock.patch.object(
            windows_setup.platform, "machine", return_value="ARM64"
        ), unittest.mock.patch.dict(
            os.environ, {"PROCESSOR_ARCHITEW6432": "ARM64"}
        ):
            windows_setup.validate_platform()

    def test_validate_platform_rejects_native_arm64(self) -> None:
        with unittest.mock.patch.object(
            windows_setup.platform, "system", return_value="Windows"
        ), unittest.mock.patch.object(
            windows_setup.platform, "machine", return_value="ARM64"
        ), unittest.mock.patch.object(
            windows_setup, "running_x64_build", return_value=False
        ), unittest.mock.patch.object(
            windows_setup, "wow64_process2_probe", return_value=(False, 0, 0)
        ), unittest.mock.patch.dict(
            os.environ, {}, clear=True
        ):
            with self.assertRaisesRegex(windows_setup.SetupError, "ARM64"):
                windows_setup.validate_platform()

    def test_validate_platform_still_accepts_x64(self) -> None:
        with unittest.mock.patch.object(
            windows_setup.platform, "system", return_value="Windows"
        ), unittest.mock.patch.object(
            windows_setup.platform, "machine", return_value="AMD64"
        ):
            windows_setup.validate_platform()


if __name__ == "__main__":
    unittest.main()
