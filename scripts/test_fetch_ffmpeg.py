#!/usr/bin/env python3
"""Tests for the portable ffmpeg fetcher (Windows cn route)."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import fetch_ffmpeg  # noqa: E402


def essentials_zip(version: str = "8.1.2") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        prefix = f"ffmpeg-{version}-essentials_build"
        bundle.writestr(f"{prefix}/README.txt", "fake build")
        bundle.writestr(f"{prefix}/bin/ffmpeg.exe", "MZ fake ffmpeg")
        bundle.writestr(f"{prefix}/bin/ffprobe.exe", "MZ fake ffprobe")
    return buffer.getvalue()


class ParserTests(unittest.TestCase):
    def test_parse_version_accepts_release_and_rejects_html(self) -> None:
        self.assertEqual(fetch_ffmpeg.parse_version("8.1.2\n"), "8.1.2")
        for bad in ("v8.1.2", "<!DOCTYPE HTML>", "", "8.1.2-full"):
            with self.subTest(bad=bad):
                with self.assertRaises(fetch_ffmpeg.FetchError):
                    fetch_ffmpeg.parse_version(bad)

    def test_parse_sha256_accepts_bare_and_bsd_style(self) -> None:
        digest = "a" * 64
        self.assertEqual(fetch_ffmpeg.parse_sha256(f"{digest}\n"), digest)
        self.assertEqual(fetch_ffmpeg.parse_sha256(f"{digest} *file.zip"), digest)
        for bad in ("", "deadbeef", "<html>303 See Other</html>"):
            with self.subTest(bad=bad):
                with self.assertRaises(fetch_ffmpeg.FetchError):
                    fetch_ffmpeg.parse_sha256(bad)

    def test_apply_mirror_requires_https_on_both_sides(self) -> None:
        prefixed = fetch_ffmpeg.apply_mirror("https://a/b.zip", "https://mirror/")
        self.assertEqual(prefixed, "https://mirror/https://a/b.zip")
        with self.assertRaises(fetch_ffmpeg.FetchError):
            fetch_ffmpeg.apply_mirror("http://a/b.zip", "https://mirror")
        with self.assertRaises(fetch_ffmpeg.FetchError):
            fetch_ffmpeg.apply_mirror("https://a/b.zip", "http://mirror")


class DownloadTests(unittest.TestCase):
    def test_download_verifies_sha256(self) -> None:
        payload = essentials_zip()
        good = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src.zip"
            source.write_bytes(payload)
            url = source.as_uri()
            target = Path(tmp) / "out.zip"
            fetch_ffmpeg.download(url, target, good, timeout=5)
            self.assertEqual(target.read_bytes(), payload)
            with self.assertRaisesRegex(fetch_ffmpeg.FetchError, "sha256 mismatch"):
                fetch_ffmpeg.download(url, Path(tmp) / "bad.zip", "b" * 64, timeout=5)


class StageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = essentials_zip()
        self.sha256 = hashlib.sha256(self.payload).hexdigest()
        self.asset = {
            "version": "8.1.2",
            "asset": "ffmpeg-8.1.2-essentials_build.zip",
            "url": "https://example.invalid/ffmpeg.zip",
            "sha256": self.sha256,
        }
        # The staged "binary" is fake zip content; on Windows run_postcheck
        # would execute it for real (WinError 216). Neutralize it everywhere
        # so the suite behaves identically across platforms.
        patcher = mock.patch.object(fetch_ffmpeg, "run_postcheck", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_download(self, url: str, target: Path, sha256: str, timeout: float) -> None:
        self.assertEqual(sha256, self.sha256)
        target.write_bytes(self.payload)

    def test_stage_unpacks_bin_and_writes_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "ffmpeg"
            with mock.patch.object(fetch_ffmpeg, "download", self.fake_download):
                report = fetch_ffmpeg._stage_resolved(
                    dest, dict(self.asset), "gyan.dev", False, 5.0
                )
            self.assertEqual(report["status"], "staged")
            self.assertTrue((dest / "bin" / "ffmpeg.exe").is_file())
            pointer = json.loads((dest / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(pointer["version"], "8.1.2")
            self.assertEqual(pointer["sha256"], self.sha256)
            self.assertEqual(pointer["channel"], "gyan.dev")
            status = fetch_ffmpeg.status_report(dest)
            self.assertEqual(status["status"], "staged")

    def test_stage_is_idempotent_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "ffmpeg"
            calls = []

            def counting_download(url, target, sha256, timeout):
                calls.append(url)
                target.write_bytes(self.payload)

            with mock.patch.object(fetch_ffmpeg, "download", counting_download):
                first = fetch_ffmpeg._stage_resolved(
                    dest, dict(self.asset), "gyan.dev", False, 5.0
                )
                second = fetch_ffmpeg._stage_resolved(
                    dest, dict(self.asset), "gyan.dev", False, 5.0
                )
            self.assertEqual(first["status"], "staged")
            self.assertEqual(second["status"], "already-staged")
            self.assertEqual(len(calls), 1)

    def test_stage_falls_back_to_project_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "ffmpeg"
            with mock.patch.object(
                fetch_ffmpeg, "resolve_via_gyan",
                side_effect=fetch_ffmpeg.FetchError("blocked"),
            ), mock.patch.object(
                fetch_ffmpeg, "resolve_via_project_mirror",
                return_value=dict(self.asset),
            ), mock.patch.object(fetch_ffmpeg, "download", self.fake_download):
                report = fetch_ffmpeg.stage(dest)
            self.assertEqual(report["status"], "staged")
            self.assertEqual(report["channel"], "project-mirror")

    def test_stage_reports_every_failed_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "ffmpeg"
            with mock.patch.object(
                fetch_ffmpeg, "resolve_via_gyan",
                side_effect=fetch_ffmpeg.FetchError("gyan blocked"),
            ), mock.patch.object(
                fetch_ffmpeg, "resolve_via_project_mirror",
                side_effect=fetch_ffmpeg.FetchError("mirror down"),
            ):
                with self.assertRaisesRegex(
                    fetch_ffmpeg.FetchError, "gyan blocked.*mirror down"
                ):
                    fetch_ffmpeg.stage(dest)


class CliTests(unittest.TestCase):
    def test_asset_url_requires_sha256(self) -> None:
        code = fetch_ffmpeg.main(["--asset-url", "https://a/b.zip"])
        self.assertEqual(code, 1)

    def test_status_on_empty_dest_exits_3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = fetch_ffmpeg.main(["--status", "--json", "--dest", tmp])
        self.assertEqual(code, 3)


class ResolveTests(unittest.TestCase):
    def test_resolve_via_gyan_composes_asset(self) -> None:
        responses = {
            fetch_ffmpeg.GYAN_VERSION_URL: "8.1.2\n",
            fetch_ffmpeg.GYAN_SHA256_URL: "c" * 64 + "\n",
        }
        with mock.patch.object(
            fetch_ffmpeg, "http_text",
            side_effect=lambda url, timeout, limit=4096: responses[url],
        ):
            asset = fetch_ffmpeg.resolve_via_gyan(5.0)
        self.assertEqual(asset["version"], "8.1.2")
        self.assertEqual(asset["asset"], "ffmpeg-8.1.2-essentials_build.zip")
        self.assertEqual(asset["url"], fetch_ffmpeg.GYAN_ZIP_URL)
        self.assertEqual(asset["sha256"], "c" * 64)

    def test_resolve_via_project_mirror_validates_manifest(self) -> None:
        manifest = {
            "version": "8.1.2",
            "asset": "ffmpeg-8.1.2-essentials_build.zip",
            "url": "https://wiki.htmlgo.to/_mirror/ffmpeg/download/x.zip",
            "sha256": "d" * 64,
        }
        with mock.patch.object(fetch_ffmpeg, "http_json", return_value=dict(manifest)):
            asset = fetch_ffmpeg.resolve_via_project_mirror(5.0)
        self.assertEqual(asset["sha256"], "d" * 64)
        broken = dict(manifest, url="http://insecure/x.zip")
        with mock.patch.object(fetch_ffmpeg, "http_json", return_value=broken):
            with self.assertRaises(fetch_ffmpeg.FetchError):
                fetch_ffmpeg.resolve_via_project_mirror(5.0)


if __name__ == "__main__":
    unittest.main()
