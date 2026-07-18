#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from unittest import mock

from scripts import check_windows_toolchain_updates as checker


class WindowsToolchainUpdateTests(unittest.TestCase):
    def test_numeric_version_comparison_handles_dates_and_semver(self) -> None:
        self.assertTrue(checker.is_newer("2026.08.01", "2026.07.04"))
        self.assertTrue(checker.is_newer("1.10.0", "1.9.9"))
        self.assertFalse(checker.is_newer("1.8.6", "1.8.6"))

    def test_collect_reports_candidates_without_editing_lock(self) -> None:
        lock = json.loads(checker.LOCK.read_text(encoding="utf-8"))
        pypi_versions = {
            "pyinstaller": lock["build"]["pyinstaller"],
            "markitdown": "0.1.7",
            **{
                package.split("==", 1)[0]: package.split("==", 1)[1]
                for component in ("asr-zh", "asr-other")
                for package in lock["components"][component]["packages"]
            },
        }

        def fake_json(url: str):
            if url == "https://nodejs.org/dist/index.json":
                return [{
                    "version": "v" + lock["components"]["web"]["node"]["version"],
                    "files": ["win-x64-zip"],
                }]
            if "registry.npmjs.org" in url:
                opencli = lock["components"]["web"]["opencli"]
                return {"version": opencli["version"], "dist": {"integrity": opencli["integrity"]}}
            if "api.github.com/repos/jackwener/OpenCLI" in url:
                extension = lock["components"]["web"]["extension"]["version"]
                return {"assets": [{"name": f"opencli-extension-v{extension}.zip"}]}
            if "api.github.com/repos/yt-dlp/yt-dlp" in url:
                return {"tag_name": lock["components"]["video"]["yt_dlp"]["version"]}
            if "pypi.org/pypi/" in url:
                project = url.split("/pypi/", 1)[1].split("/", 1)[0]
                if url.endswith("/json") and project in {"torch", "torchaudio"}:
                    version = pypi_versions[project]
                    releases = {
                        version: [{"filename": f"{project}-{version}-cp312-cp312-win_amd64.whl"}]
                    }
                    if project == "torch":
                        releases["9.9.0"] = [
                            {"filename": "torch-9.9.0-cp312-cp312-win_amd64.whl"}
                        ]
                    return {"releases": releases}
                return {"info": {"version": pypi_versions[project]}}
            raise AssertionError(f"unexpected URL: {url}")

        def fake_request(url: str, *, timeout: int = 20):
            del timeout
            if url == "https://www.python.org/ftp/python/":
                version = lock["python"]["version"]
                return f'<a href="{version}/">{version}</a>'.encode()
            if url.startswith("https://www.python.org/ftp/python/3.12"):
                version = lock["python"]["version"]
                return f'python-{version}-embeddable-amd64.zip'.encode()
            if url.endswith("release-version"):
                return lock["components"]["video"]["ffmpeg"]["version"].encode()
            raise AssertionError(f"unexpected URL: {url}")

        before = checker.LOCK.read_bytes()
        with mock.patch.object(checker, "request_json", side_effect=fake_json), \
                mock.patch.object(checker, "request", side_effect=fake_request):
            report = checker.collect(lock)
        self.assertEqual(checker.LOCK.read_bytes(), before)
        self.assertEqual([row["id"] for row in report["updates"]], ["markitdown"])
        self.assertEqual(report["errors"], [])
        rendered = checker.markdown(report)
        self.assertIn("markitdown", rendered)
        self.assertIn("never replace an existing tag", rendered)


if __name__ == "__main__":
    unittest.main()
