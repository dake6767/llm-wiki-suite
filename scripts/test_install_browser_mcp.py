#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("install-browser.py")
SPEC = importlib.util.spec_from_file_location("install_browser", SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(installer)


class McpRegistrationTests(unittest.TestCase):
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
                    "url": "http://wiki.htmlgo.to/_update/dl/v1.0.14/browser.msi"
                },
                "windows-x86_64-nsis": {
                    "url": "http://wiki.htmlgo.to/_update/dl/v1.0.14/browser-setup.exe"
                },
            },
        }
        source = {"name": "htmlgo", "format": "tauri-latest",
                  "url": "https://wiki.htmlgo.to/_update/latest.json"}
        with mock.patch.object(installer, "json_url", return_value=manifest), \
             mock.patch.object(installer.platform, "system", return_value="Windows"), \
             mock.patch.object(installer.platform, "machine", return_value="AMD64"):
            asset = installer.tauri_manifest_asset(source)
        self.assertEqual(asset["name"], "browser-setup.exe")
        self.assertEqual(
            asset["browser_download_url"],
            "https://wiki.htmlgo.to/_update/dl/v1.0.14/browser-setup.exe",
        )

    def test_project_release_source_wins_before_github_fallback(self):
        config = {
            "browser": {
                "release_sources": [
                    {"name": "htmlgo", "format": "tauri-latest", "url": "https://example.test/latest.json"},
                    {"name": "github", "format": "github-release"},
                ],
                "asset_patterns": {},
            }
        }
        expected = {"name": "browser-setup.exe", "browser_download_url": "https://example.test/browser.exe"}
        with mock.patch.object(installer, "tauri_manifest_asset", return_value=expected), \
             mock.patch.object(installer, "release_metadata") as github_release:
            asset, source = installer.resolve_release_asset(config, "owner/repo", "latest")
        self.assertEqual((asset, source), (expected, "htmlgo"))
        github_release.assert_not_called()

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
            self.assertEqual(installer.blocked_github_release_hosts(), ["api.github.com"])

    def test_network_probe_failure_does_not_block_legacy_install(self):
        with mock.patch.object(installer, "NETWORK_PROBE", Path(__file__)), \
             mock.patch.object(installer.subprocess, "run", side_effect=OSError):
            self.assertEqual(installer.blocked_github_release_hosts(), [])

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
                config, root=root, python_executable="/path with space/python3"
            )[0]
            encoded = json.dumps(row["manual_config"])
            self.assertIn("/path with space/python3", encoded)
            self.assertNotIn("npx", encoded)


if __name__ == "__main__":
    unittest.main()
