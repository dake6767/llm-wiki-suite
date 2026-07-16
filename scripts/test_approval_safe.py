#!/usr/bin/env python3
"""Regression tests for approval-clean wiki skill helpers and command examples."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


approval_safety = load_module("approval_safety", ROOT / "scripts" / "check_approval_safety.py")
video_probe = load_module(
    "video_probe", ROOT / "skills" / "my-llm-wiki-video" / "scripts" / "video_probe.py"
)
sensevoice = load_module(
    "sensevoice_to_srt",
    ROOT / "skills" / "my-llm-wiki-video" / "scripts" / "sensevoice_to_srt.py",
)
faster_whisper_runner = load_module(
    "faster_whisper_to_srt",
    ROOT / "skills" / "my-llm-wiki-video" / "scripts" / "faster_whisper_to_srt.py",
)
douyin_probe = load_module(
    "douyin_probe", ROOT / "skills" / "my-llm-wiki-video" / "scripts" / "douyin_probe.py"
)
html_to_text = load_module(
    "html_to_text", ROOT / "skills" / "my-llm-wiki" / "scripts" / "html_to_text.py"
)
wiki_ops = load_module(
    "wiki_ops", ROOT / "skills" / "my-llm-wiki-maintainer" / "scripts" / "wiki_ops.py"
)


class HelperTests(unittest.TestCase):
    def test_video_metadata_normalization(self) -> None:
        record = video_probe.normalize_metadata({
            "id": "BV123",
            "title": "标题",
            "channel": "作者",
            "duration": 123.4,
            "subtitles": {"zh": []},
            "automatic_captions": {"en": []},
        })
        self.assertEqual(record["uploader"], "作者")
        self.assertEqual(record["subtitle_languages"], ["zh"])
        self.assertEqual(record["automatic_caption_languages"], ["en"])

    def test_srt_timestamp_and_rich_marker_cleanup(self) -> None:
        self.assertEqual(sensevoice.srt_timestamp(61.234), "00:01:01,234")
        self.assertEqual(faster_whisper_runner.srt_timestamp(61.234), "00:01:01,234")
        self.assertEqual(sensevoice.RICH_MARKERS.sub("", "你好😊\ufe0f"), "你好")

    def test_html_extraction_drops_active_markup(self) -> None:
        text = html_to_text.html_to_text(
            "<html><style>secret</style><h1>标题</h1><script>bad()</script><p>正文</p></html>"
        )
        self.assertIn("标题", text)
        self.assertIn("正文", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("bad", text)

    def test_html_reader_enforces_size_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.html"
            path.write_bytes(b"x" * 11)
            with self.assertRaises(ValueError):
                html_to_text.read_bounded(path, 10)

    def test_douyin_fixture_parser(self) -> None:
        item = {
            "aweme_id": "123456789",
            "desc": "标题",
            "author": {"nickname": "作者", "sec_uid": "sec"},
            "create_time": 123,
            "video": {
                "duration": 456,
                "play_addr": {"url_list": ["https://cdn/play"]},
                "cover": {"url_list": ["https://cdn/cover"]},
            },
        }
        payload = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": [item]}}}}
        html = f"<script>window._ROUTER_DATA = {json.dumps(payload)} </script>"
        result = douyin_probe.extract_metadata(html)
        self.assertEqual(result["title"], "标题")
        self.assertEqual(result["play_url"], "https://cdn/play")


class ApprovalSafetyTests(unittest.TestCase):
    def test_suite_docs_are_approval_clean(self) -> None:
        findings = approval_safety.scan_paths(list(approval_safety.DEFAULT_SKILLS))
        self.assertEqual(findings, [], "\n".join(map(str, findings)))

    def test_linter_catches_remote_pipe_and_inline_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.md"
            path.write_text(
                "```bash\ncurl https://example.com/data | python3 -c 'print(1)'\n```\n",
                encoding="utf-8",
            )
            rules = {finding.rule for finding in approval_safety.scan_file(path)}
            self.assertIn("remote-pipe-interpreter", rules)
            self.assertIn("inline-interpreter-code", rules)

    def test_linter_does_not_join_separate_shell_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "safe.md"
            path.write_text(
                "```bash\ncurl -o page.html https://example.com/\n"
                "python3 scripts/parse_page.py page.html\n```\n",
                encoding="utf-8",
            )
            rules = {finding.rule for finding in approval_safety.scan_file(path)}
            self.assertNotIn("remote-pipe-interpreter", rules)

    def test_linter_catches_remote_substitution_and_plain_http(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.md"
            path.write_text(
                "```bash\nurl=$(curl http://example.com/share)\n```\n",
                encoding="utf-8",
            )
            rules = {finding.rule for finding in approval_safety.scan_file(path)}
            self.assertIn("remote-command-substitution", rules)
            self.assertIn("plain-http-url", rules)


class BackgroundLifecycleTests(unittest.TestCase):
    def test_self_managed_video_jobs_disable_async_completion_pushes(self) -> None:
        docs = (
            ROOT / "skills" / "my-llm-wiki" / "SKILL.md",
            ROOT / "skills" / "my-llm-wiki-video" / "SKILL.md",
            ROOT / "skills" / "my-llm-wiki-video" / "references" / "video-asr.md",
            ROOT / "skills" / "my-llm-wiki-maintainer" / "references" / "video-ingest-workflow.md",
        )
        for path in docs:
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertIn("notify_on_complete=false", text)

    def test_video_sop_requires_process_reap_before_final_report(self) -> None:
        path = (
            ROOT / "skills" / "my-llm-wiki-video" / "references" / "video-asr.md"
        )
        text = path.read_text(encoding="utf-8")
        self.assertIn("Reap before the final answer", text)
        self.assertIn("`process poll` is intentionally read-only", text)


class CacheFilesFileTests(unittest.TestCase):
    def test_cache_save_accepts_json_files_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "wiki"
            (root / "wiki").mkdir(parents=True)
            (root / "raw" / "sources" / "video").mkdir(parents=True)
            (root / "purpose.md").write_text("# Purpose\n", encoding="utf-8")
            (root / "schema.md").write_text("# Schema\n", encoding="utf-8")
            raw = root / "raw" / "sources" / "video" / "中文来源.md"
            raw.write_text("# source\n", encoding="utf-8")
            pages = ["wiki/sources/中文来源.md", "wiki/concepts/概念.md"]
            pages_file = Path(directory) / "pages.json"
            pages_file.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = wiki_ops.main([
                    "cache", "save", str(root), str(raw),
                    "--files-file", str(pages_file),
                ])
            self.assertEqual(rc, 0)
            saved = json.loads(output.getvalue())
            self.assertEqual(saved["filesWritten"], pages)
            cache = json.loads((root / ".llm-wiki" / "agent" / "ingest-cache.json").read_text())
            self.assertIn("raw/sources/video/中文来源.md", cache["entries"])


if __name__ == "__main__":
    unittest.main()
