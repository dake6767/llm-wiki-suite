#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("install-browser.py")
SPEC = importlib.util.spec_from_file_location("install_browser", SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(installer)


class McpRegistrationTests(unittest.TestCase):
    def test_windows_setup_is_launched_without_waiting_or_verification(self):
        setup = Path("C:/Users/Test/Downloads/browser-setup.exe")
        with mock.patch.object(installer.subprocess, "Popen") as popen, \
             mock.patch.object(installer.subprocess, "run") as run, \
             mock.patch.object(installer, "_windows_registry_install_location") as verify:
            result = installer.install_windows_artifact({}, setup, dry_run=False)
        self.assertIsInstance(result, installer.InstallerLaunch)
        self.assertEqual(result.artifact, setup)
        popen.assert_called_once_with(
            [str(setup)],
            stdin=installer.subprocess.DEVNULL,
            stdout=installer.subprocess.DEVNULL,
            stderr=installer.subprocess.DEVNULL,
            close_fds=True,
        )
        run.assert_not_called()
        verify.assert_not_called()

    def test_windows_silent_install_waits_and_verifies_registration(self):
        setup = Path("C:/Users/Test/Downloads/browser-setup.exe")
        exe = Path("C:/Users/Test/AppData/Local/Browser/browser.exe")
        with mock.patch.object(installer.subprocess, "Popen") as popen, \
             mock.patch.object(installer.subprocess, "run") as run, \
             mock.patch.object(
                 installer, "_windows_registry_install_location", return_value=exe
             ) as verify:
            run.return_value = mock.Mock(returncode=0)
            result = installer.install_windows_artifact(
                {}, setup, dry_run=False, silent=True
            )
        self.assertEqual(result, exe)
        popen.assert_not_called()
        self.assertEqual(run.call_args[0][0], [str(setup), "/S"])
        verify.assert_called_once()

    def test_windows_silent_install_raises_on_installer_failure(self):
        setup = Path("C:/Users/Test/Downloads/browser-setup.exe")
        with mock.patch.object(installer.subprocess, "run") as run, \
             mock.patch.object(
                 installer, "_windows_registry_install_location"
             ) as verify:
            run.return_value = mock.Mock(returncode=5)
            with self.assertRaisesRegex(RuntimeError, "exit code 5"):
                installer.install_windows_artifact(
                    {}, setup, dry_run=False, silent=True
                )
        verify.assert_not_called()

    def test_windows_msi_uses_native_installer_and_returns_immediately(self):
        msi = Path("C:/Users/Test/Downloads/browser.msi")
        with mock.patch.object(installer.subprocess, "Popen") as popen:
            result = installer.install_windows_artifact({}, msi)
        self.assertIsInstance(result, installer.InstallerLaunch)
        popen.assert_called_once_with(
            ["msiexec.exe", "/i", str(msi)],
            stdin=installer.subprocess.DEVNULL,
            stdout=installer.subprocess.DEVNULL,
            stderr=installer.subprocess.DEVNULL,
            close_fds=True,
        )

    def test_windows_installer_launch_failure_is_reported(self):
        setup = Path("C:/Downloads/browser-setup.exe")
        with mock.patch.object(
            installer.subprocess, "Popen", side_effect=OSError("blocked")
        ):
            with self.assertRaisesRegex(RuntimeError, "could not launch"):
                installer.install_windows_artifact({}, setup)

    def test_infers_github_fallback_repo_from_gitee_origin(self):
        with mock.patch.object(
            installer, "run", return_value="https://gitee.com/dake6767/llm-wiki-suite.git"
        ):
            self.assertEqual(installer.infer_repo(), "dake6767/llm-wiki-suite")

    def test_htmlgo_tauri_manifest_prefers_windows_setup_and_keeps_https(self):
        manifest = {
            "version": "1.0.14",
            "platforms": {
                "windows-x86_64": {
                    "url": "http://wiki.htmlgo.to/_update/dl/v1.0.14/browser.msi",
                    "signature": "msi-signature",
                },
                "windows-x86_64-nsis": {
                    "url": "http://wiki.htmlgo.to/_update/dl/v1.0.14/browser-setup.exe",
                    "signature": "setup-signature",
                },
            },
        }
        source = {"name": "htmlgo", "format": "tauri-latest",
                  "url": "https://wiki.htmlgo.to/_update/latest.json"}
        with mock.patch.object(installer, "json_url", return_value=manifest), \
             mock.patch.object(installer, "tauri_updater_public_key", return_value="public-key"), \
             mock.patch.object(installer.platform, "system", return_value="Windows"), \
             mock.patch.object(installer.platform, "machine", return_value="AMD64"):
            asset = installer.tauri_manifest_asset(source)
        self.assertEqual(asset["name"], "browser-setup.exe")
        self.assertEqual(
            asset["browser_download_url"],
            "https://wiki.htmlgo.to/_update/dl/v1.0.14/browser-setup.exe",
        )
        self.assertEqual(asset["signature"], "setup-signature")
        self.assertEqual(asset["public_key"], "public-key")

    def test_htmlgo_unsigned_asset_is_rejected(self):
        manifest = {
            "version": "1.0.14",
            "platforms": {"darwin-aarch64": {"url": "https://example.test/app.tar.gz"}},
        }
        source = {
            "name": "htmlgo",
            "format": "tauri-latest",
            "url": "https://example.test/latest.json",
        }
        with mock.patch.object(installer, "json_url", return_value=manifest), \
             mock.patch.object(installer.platform, "system", return_value="Darwin"), \
             mock.patch.object(installer.platform, "machine", return_value="arm64"):
            with self.assertRaisesRegex(RuntimeError, "unsigned"):
                installer.tauri_manifest_asset(source)

    def test_asset_download_failure_advances_to_github_fallback(self):
        config = {
            "browser": {
                "release_sources": [
                    {"name": "htmlgo", "format": "tauri-latest", "url": "https://example.test/latest.json"},
                    {"name": "github", "format": "github-release"},
                ],
                "asset_patterns": {},
            }
        }
        first = {"name": "browser.exe", "browser_download_url": "https://example.test/browser.exe"}
        second = {"name": "browser.dmg", "browser_download_url": "https://github.com/browser.dmg"}
        destination = Path("/tmp/browser.dmg")
        with mock.patch.object(
            installer, "resolve_source_asset", side_effect=[first, second]
        ) as resolve, mock.patch.object(
            installer, "download_asset", side_effect=[RuntimeError("CDN stalled"), destination]
        ):
            result, installed, source = installer.install_release(
                config, "owner/repo", "latest", Path("/tmp")
            )
        self.assertEqual((result, installed, source), (destination, destination, "github"))
        self.assertEqual(resolve.call_count, 2)

    def test_artifact_prepare_failure_advances_to_next_source(self):
        config = {
            "browser": {
                "release_sources": [
                    {"name": "htmlgo", "format": "tauri-latest"},
                    {"name": "github", "format": "github-release"},
                ],
                "asset_patterns": {},
            }
        }
        first = Path("/tmp/browser.app.tar.gz")
        second = Path("/tmp/browser.dmg")
        prepare = mock.Mock(side_effect=[RuntimeError("bad archive"), second])
        with mock.patch.object(
            installer, "resolve_source_asset", side_effect=[{"name": first.name}, {"name": second.name}]
        ) as resolve, mock.patch.object(
            installer, "download_asset", side_effect=[first, second]
        ):
            downloaded, installed, source = installer.install_release(
                config, "owner/repo", "latest", Path("/tmp"), prepare=prepare
            )
        self.assertEqual((downloaded, installed, source), (second, second, "github"))
        self.assertEqual(resolve.call_count, 2)

    def test_launched_windows_installer_writes_no_receipt_and_is_not_opened(self):
        artifact = Path("C:/Downloads/browser-setup.exe")
        args = mock.Mock(
            repo="owner/repo",
            version="latest",
            dry_run=False,
            download_dir="C:/Downloads",
            fallback_source=False,
            open=True,
        )
        config = {"browser": {"release_sources": []}}
        with mock.patch.object(
            installer,
            "install_release",
            return_value=(artifact, installer.InstallerLaunch(artifact), "htmlgo"),
        ), mock.patch.object(installer, "write_install_receipt") as receipt, \
             mock.patch.object(installer, "maybe_launch_installed") as launch:
            result = installer.perform_browser_install(config, args)
        self.assertEqual(result, 0)
        receipt.assert_not_called()
        launch.assert_not_called()

    def test_receipt_is_valid_only_while_installed_target_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "My.AppImage"
            target.write_bytes(b"app")
            target.chmod(0o755)
            artifact = root / "release.AppImage"
            artifact.write_bytes(b"release")
            config = {
                "browser": {
                    "install_receipt": {
                        "path": str(root / "install.json"),
                        "schema": 1,
                        "windows_main_executable": "llm-wiki-desktop.exe",
                    }
                }
            }
            with mock.patch.object(installer.platform, "system", return_value="Linux"), \
                 mock.patch.object(installer.platform, "machine", return_value="x86_64"):
                installer.write_install_receipt(
                    config,
                    artifact=artifact,
                    target=target,
                    source="htmlgo",
                    requested_version="latest",
                )
                self.assertTrue(installer.browser_install_state(config)["ok"])
                target.unlink()
                self.assertFalse(installer.browser_install_state(config)["ok"])

    def test_blocked_release_hosts_are_reported_from_cn_probe(self):
        probe = {
            "dev": [
                {"host": "api.github.com", "status": "blocked"},
                {"host": "objects.githubusercontent.com", "status": "ok"},
            ]
        }
        completed = mock.Mock(stdout=json.dumps(probe))
        with mock.patch.object(installer, "NETWORK_PROBE", Path(__file__)), \
             mock.patch.object(installer.subprocess, "run", return_value=completed):
            self.assertEqual(installer.unavailable_github_release_hosts(), ["api.github.com"])

    def test_network_probe_failure_does_not_invent_a_network_verdict(self):
        with mock.patch.object(installer, "NETWORK_PROBE", Path(__file__)), \
             mock.patch.object(installer.subprocess, "run", side_effect=OSError):
            self.assertEqual(installer.unavailable_github_release_hosts(), [])

    def test_non_github_download_never_receives_github_token(self):
        class Response(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        opener = mock.Mock()
        opener.open.return_value = Response(b"asset")
        asset = {
            "name": "browser.dmg",
            "browser_download_url": "https://wiki.htmlgo.to/browser.dmg",
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"GITHUB_TOKEN": "top-secret"}
        ), mock.patch.object(installer.urllib.request, "build_opener", return_value=opener):
            installer.download_asset(asset, Path(tmp))
        request = opener.open.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))

    def test_release_patterns_match_tauri_canonical_asset_names(self):
        patterns = installer.load_bootstrap()["browser"]["asset_patterns"]
        cases = {
            "darwin-arm64": "My.LLM.Wiki.Browser_1.0.6_aarch64.dmg",
            "darwin-x64": "My.LLM.Wiki.Browser_1.0.6_x64.dmg",
            "windows-x64": "My.LLM.Wiki.Browser_1.0.6_x64-setup.exe",
            "linux-x64": "My.LLM.Wiki.Browser_1.0.6_amd64.AppImage",
        }
        for platform_key, asset_name in cases.items():
            with self.subTest(platform=platform_key), mock.patch.object(
                installer, "platform_keys", return_value=[platform_key]
            ):
                selected = installer.pick_asset(
                    {"assets": [{"name": asset_name}]}, patterns
                )
                self.assertEqual(selected["name"], asset_name)

    def test_unsupported_linux_architecture_has_no_x64_fallback(self):
        with mock.patch.object(installer.platform, "system", return_value="Linux"), \
             mock.patch.object(installer.platform, "machine", return_value="aarch64"):
            self.assertEqual(installer.platform_keys(), [])
            self.assertEqual(installer.tauri_platform_keys(), [])

    def test_windows_arm64_emulation_selects_x64_assets(self):
        with mock.patch.object(installer.platform, "system", return_value="Windows"), \
             mock.patch.object(installer.platform, "machine", return_value="ARM64"), \
             mock.patch.dict(os.environ, {"PROCESSOR_ARCHITEW6432": "ARM64"}):
            self.assertEqual(installer.platform_keys(), ["windows-x64"])
            self.assertEqual(
                installer.tauri_platform_keys(),
                ["windows-x86_64-nsis", "windows-x86_64", "windows-x86_64-msi"],
            )

    def test_windows_arm64_x64_interpreter_selects_x64_assets(self):
        # Real-device case: both WOW64 signals miss the emulation, but the
        # interpreter binary itself is an x64 PE, which proves x64 runs.
        with mock.patch.object(installer.platform, "system", return_value="Windows"), \
             mock.patch.object(installer.platform, "machine", return_value="ARM64"), \
             mock.patch.object(installer, "running_x64_build", return_value=True), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(installer.platform_keys(), ["windows-x64"])

    def test_windows_native_arm64_has_no_assets(self):
        with mock.patch.object(installer.platform, "system", return_value="Windows"), \
             mock.patch.object(installer.platform, "machine", return_value="ARM64"), \
             mock.patch.object(installer, "running_x64_build", return_value=False), \
             mock.patch.object(installer, "x64_emulation_on_arm64", return_value=False), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(installer.platform_keys(), [])
            self.assertEqual(installer.tauri_platform_keys(), [])

    def test_portable_zip_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / "browser.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.exe", b"bad")
            with self.assertRaisesRegex(RuntimeError, "unsafe archive member"):
                installer.maybe_extract_zip(archive_path, dry_run=False)
            self.assertFalse((root / "outside.exe").exists())

    def test_macos_app_tar_installs_single_bundle_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / "payload"
            executable = payload / "My LLM Wiki Browser.app" / "Contents" / "MacOS" / "browser"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"new")
            archive_path = root / "browser.app.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(payload / "My LLM Wiki Browser.app", arcname="My LLM Wiki Browser.app")
            install_dir = root / "Applications"
            old = install_dir / "My LLM Wiki Browser.app" / "Contents" / "MacOS" / "browser"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"old")

            installed = installer.install_macos_app_archive(archive_path, install_dir)

            self.assertEqual(installed, install_dir / "My LLM Wiki Browser.app")
            self.assertEqual((installed / "Contents" / "MacOS" / "browser").read_bytes(), b"new")
            self.assertFalse(any(install_dir.glob(".*.backup-*")))

    def test_macos_app_tar_rejects_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / "browser.app.tar.gz"
            link = tarfile.TarInfo("My LLM Wiki Browser.app/escape")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../outside"
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.addfile(link)
            with self.assertRaisesRegex(RuntimeError, "unsupported archive member"):
                installer.install_macos_app_archive(archive_path, root / "Applications")
            self.assertFalse((root / "outside").exists())

    def test_registry_defaults_every_local_host_to_suite_bridge(self):
        mcp = installer.load_bootstrap()["mcp"]
        self.assertEqual(mcp["local_default_transport"], "stdio")
        self.assertEqual(mcp["remote_default_transport"], "http")
        self.assertEqual(mcp["stdio_bridge_script"], "scripts/mcp-stdio-bridge.py")
        encoded = json.dumps(mcp)
        self.assertNotIn("mcp-remote", encoded)
        self.assertNotIn('"npx"', encoded)
        for name in ("claude", "hermes", "codex"):
            recipe = mcp["hosts"][name]["register_argv"]
            self.assertIn("{python}", recipe)
            self.assertIn("{bridge_script}", recipe)
        workbuddy = mcp["hosts"]["workbuddy"]["manual_config"]
        self.assertEqual(
            workbuddy["mcpServers"]["my-llm-wiki"]["args"], ["{bridge_script}"]
        )

    def test_posix_paths_with_spaces_remain_single_argv_items(self):
        with tempfile.TemporaryDirectory(prefix="suite with spaces ") as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "mcp-stdio-bridge.py").write_text("", encoding="utf-8")
            host_dir = root / "host dir"
            host_dir.mkdir()
            config = {
                "mcp": {
                    "server_name": "my-llm-wiki",
                    "stdio_bridge_script": "scripts/mcp-stdio-bridge.py",
                    "hosts": {
                        "demo": {
                            "detect_dir": str(host_dir),
                            "cli": "python3",
                            "config_path": str(root / "missing.json"),
                            "registered_check": {"format": "json", "pointer": ["mcpServers", "my-llm-wiki"]},
                            "register_argv": ["python3", "host-cli.py", "--", "{python}", "{bridge_script}"],
                            "unregister_argv": ["python3", "host-cli.py", "remove"],
                        }
                    },
                }
            }
            row = installer.build_mcp_commands(
                config,
                hosts=["demo"],
                root=root,
                python_executable="/opt/Python Stable/bin/python3",
                system="Darwin",
            )[0]
            self.assertEqual(row["argv"][-2], "/opt/Python Stable/bin/python3")
            self.assertEqual(row["argv"][-1], str((root / "scripts" / "mcp-stdio-bridge.py").resolve()))
            self.assertIn("'/opt/Python Stable/bin/python3'", row["command"])
            self.assertIn("'" + row["argv"][-1] + "'", row["command"])

    def test_windows_display_quotes_drive_and_space_paths(self):
        argv = [
            "C:\\Program Files\\Python\\python.exe",
            "C:\\Users\\A User\\.my-llm-wiki\\suite\\scripts\\mcp-stdio-bridge.py",
        ]
        rendered = installer.display_argv(argv, system="Windows")
        self.assertEqual(
            rendered,
            '"C:\\Program Files\\Python\\python.exe" '
            '"C:\\Users\\A User\\.my-llm-wiki\\suite\\scripts\\mcp-stdio-bridge.py"',
        )

    def test_manual_config_keeps_paths_as_json_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "mcp-stdio-bridge.py").write_text("", encoding="utf-8")
            host_dir = root / "workbuddy"
            host_dir.mkdir()
            config = {
                "mcp": {
                    "stdio_bridge_script": "scripts/mcp-stdio-bridge.py",
                    "hosts": {
                        "workbuddy": {
                            "detect_dir": str(host_dir),
                            "cli": None,
                            "config_path": str(root / "mcp.json"),
                            "registered_check": {"format": "text", "marker": "my-llm-wiki"},
                            "manual_config": {
                                "mcpServers": {
                                    "my-llm-wiki": {"command": "{python}", "args": ["{bridge_script}"]}
                                }
                            },
                        }
                    },
                }
            }
            row = installer.build_mcp_commands(
                config, hosts=["workbuddy"], root=root,
                python_executable="/path with space/python3"
            )[0]
            encoded = json.dumps(row["manual_config"])
            self.assertIn("/path with space/python3", encoded)
            self.assertNotIn("npx", encoded)


class ExpandShellPathTests(unittest.TestCase):
    """CLI path args may arrive shell-expanded as MSYS /c/... from Git Bash."""

    def test_msys_path_follows_the_actual_runner_platform(self):
        expected = (
            Path("C:/Users/x/.my-llm-wiki/browser")
            if os.name == "nt"
            else Path("/c/Users/x/.my-llm-wiki/browser")
        )
        self.assertEqual(
            installer.expand("/c/Users/x/.my-llm-wiki/browser"), expected
        )


class McpNonInteractiveTests(unittest.TestCase):
    def test_explicit_host_registration_never_reads_input(self):
        row = {
            "host": "hermes",
            "registered": False,
            "transport": "",
            "command": "hermes mcp add my-llm-wiki",
            "manual_config": None,
            "config_path": "",
            "note": "",
            "bridge_available": True,
            "bridge_script": "bridge.py",
            "cli_available": True,
            "cli": "hermes",
            "argv": ["hermes", "mcp", "add"],
            "unregister_argv": [],
        }
        completed = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(installer, "build_mcp_commands", return_value=[row]), \
             mock.patch.object(installer, "browser_install_state", return_value={"ok": True}), \
             mock.patch("builtins.input", side_effect=AssertionError("must not prompt")), \
             mock.patch.object(installer, "_run_host_command", return_value=completed) as run:
            result = installer.register_mcp({}, ["hermes"], dry_run=False)
        self.assertEqual(result, 0)
        run.assert_called_once_with(row["argv"])

    def test_failed_registration_restores_original_host_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            original = b"[mcp_servers.my-llm-wiki]\nurl='http://127.0.0.1:8800/mcp'\n"
            config_path.write_bytes(original)
            row = {
                "host": "codex",
                "registered": False,
                "transport": None,
                "command": "codex mcp add",
                "manual_config": None,
                "config_path": str(config_path),
                "note": "",
                "bridge_available": True,
                "bridge_script": "bridge.py",
                "cli_available": True,
                "cli": "codex",
                "argv": ["codex", "mcp", "add"],
                "unregister_argv": ["codex", "mcp", "remove"],
            }
            failed = mock.Mock(returncode=1, stderr="bad config")
            with mock.patch.object(installer, "build_mcp_commands", return_value=[row]), \
                 mock.patch.object(installer, "browser_install_state", return_value={"ok": True}), \
                 mock.patch.object(
                     installer, "_run_host_command", return_value=failed
                 ):
                result = installer.register_mcp({}, ["codex"])
            self.assertEqual(result, 1)
            self.assertEqual(config_path.read_bytes(), original)

    def test_conflicting_registration_is_not_rewritten(self):
        row = {
            "host": "codex",
            "registered": True,
            "transport": "http-loopback",
            "command": "codex mcp add",
            "manual_config": None,
            "config_path": "/tmp/config.toml",
            "note": "",
            "bridge_available": True,
            "bridge_script": "bridge.py",
            "cli_available": True,
            "cli": "codex",
            "argv": ["codex", "mcp", "add"],
            "unregister_argv": ["codex", "mcp", "remove"],
            "unregister_command": "codex mcp remove my-llm-wiki",
        }
        with mock.patch.object(installer, "build_mcp_commands", return_value=[row]), \
             mock.patch.object(installer, "browser_install_state", return_value={"ok": True}), \
             mock.patch.object(installer, "_run_host_command") as run:
            result = installer.register_mcp({}, ["codex"])
        self.assertEqual(result, 1)
        run.assert_not_called()

    def test_registration_is_refused_without_verified_install_receipt(self):
        with mock.patch.object(
            installer,
            "browser_install_state",
            return_value={"ok": False, "detail": "Browser install receipt is missing"},
        ), mock.patch.object(installer, "build_mcp_commands") as build:
            result = installer.register_mcp({}, ["codex"])
        self.assertEqual(result, 1)
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
