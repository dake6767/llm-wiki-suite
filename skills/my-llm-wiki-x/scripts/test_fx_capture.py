#!/usr/bin/env python3
"""Unit tests for the deterministic fxtwitter fallback adapter.

Run: python3 scripts/test_fx_capture.py
Covers the rich-text -> Markdown converter (UTF-16 offsets, entity shapes),
media resolution, the plain-tweet path, and the CLI end-to-end via --from-json
with file:// media URLs — no network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fx_capture as fx  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "fx_capture.py"


def run_cli(fx_payload, tmp, extra=None):
    fx_json = os.path.join(tmp, "fx.json")
    with open(fx_json, "w", encoding="utf-8") as f:
        json.dump(fx_payload, f, ensure_ascii=False)
    out_dir = os.path.join(tmp, "out")
    cmd = [sys.executable, str(SCRIPT), "--tweet", "123", "--out", out_dir,
           "--from-json", fx_json] + (extra or [])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc, out_dir


class TestTweetId(unittest.TestCase):
    def test_url_and_bare_forms(self):
        self.assertEqual(fx.tweet_id_from("https://x.com/a/status/99"), "99")
        self.assertEqual(fx.tweet_id_from("https://twitter.com/a/statuses/7"), "7")
        self.assertEqual(fx.tweet_id_from("https://x.com/i/article/55"), "55")
        self.assertEqual(fx.tweet_id_from("12345"), "12345")
        self.assertIsNone(fx.tweet_id_from("not a tweet"))


class TestRichText(unittest.TestCase):
    def test_utf16_offsets_with_cjk_and_emoji(self):
        # "宽" chars are 1 UTF-16 unit, the emoji is 2; ranges use UTF-16 units.
        text = "看👀这个链接吧"
        # emoji at offset 1 occupies units 1-2, so "链接" sits at offset 5, len 2
        block = {"text": text, "type": "unstyled",
                 "entityRanges": [{"offset": 5, "length": 2, "key": 0}]}
        emap = {"0": {"type": "LINK", "data": {"url": "https://e.com"}}}
        out = fx._block_text_md(block, emap, [], [])
        self.assertEqual(out, "看👀这个[链接](https://e.com)吧")

    def test_entity_map_list_shape(self):
        article = {"content": {"entityMap": [
            {"key": "0", "value": {"type": "LINK", "data": {"url": "https://l.io"}}},
        ]}}
        emap = fx._entity_map(article)
        self.assertIn("0", emap)

    def test_blocks_headers_lists_code_unknown(self):
        warnings = []
        article = {"content": {"blocks": [
            {"text": "Title", "type": "header-one"},
            {"text": "item", "type": "unordered-list-item"},
            {"text": "x = 1", "type": "code-block"},
            {"text": "mystery", "type": "future-block-type"},
        ], "entityMap": {}}}
        body, _ = fx.article_to_markdown(article, {}, warnings)
        self.assertIn("# Title", body)
        self.assertIn("- item", body)
        self.assertIn("```\nx = 1\n```", body)
        self.assertIn("mystery", body)
        self.assertTrue(any("future-block-type" in w for w in warnings))

    def test_atomic_media_placeholder(self):
        article = {"content": {"blocks": [
            {"text": " ", "type": "atomic",
             "entityRanges": [{"offset": 0, "length": 1, "key": 0}]},
        ], "entityMap": {"0": {"type": "MEDIA", "data": {"mediaItems": [{"mediaId": "m1"}]}}}}}
        body, order = fx.article_to_markdown(article, fx._entity_map(article), [])
        self.assertIn("@@MEDIA:m1@@", body)
        self.assertEqual(order, ["m1"])


class TestBestMp4(unittest.TestCase):
    def test_picks_highest_bitrate_mp4(self):
        info = {"video_info": {"variants": [
            {"content_type": "application/x-mpegURL", "url": "u.m3u8"},
            {"content_type": "video/mp4", "bitrate": 320, "url": "lo.mp4"},
            {"content_type": "video/mp4", "bitrate": 2176, "url": "hi.mp4"},
        ]}}
        self.assertEqual(fx._best_mp4(info), "hi.mp4")

    def test_no_variants(self):
        self.assertIsNone(fx._best_mp4({"original_img_url": "a.jpg"}))


class TestCliEndToEnd(unittest.TestCase):
    def test_article_with_local_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = os.path.join(tmp, "pic.jpg")
            with open(img, "wb") as f:
                f.write(b"\xff\xd8fakejpg")
            payload = {"code": 200, "tweet": {
                "url": "https://x.com/u/status/123",
                "created_at": "Fri Jun 05 14:26:41 +0000 2026",
                "author": {"name": "作者", "screen_name": "u"},
                "text": "t.co/x",
                "article": {
                    "title": "AI名词入门",
                    "content": {
                        "blocks": [
                            {"text": "第一段", "type": "unstyled"},
                            {"text": " ", "type": "atomic",
                             "entityRanges": [{"offset": 0, "length": 1, "key": 0}]},
                        ],
                        "entityMap": {"0": {"type": "MEDIA",
                                            "data": {"mediaItems": [{"mediaId": "9"}]}}},
                    },
                    "media_entities": [
                        {"media_id": "9",
                         "media_info": {"original_img_url": "file://" + img}},
                    ],
                },
            }}
            proc, out_dir = run_cli(payload, tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads(proc.stdout)
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["title"], "AI名词入门")
            self.assertEqual(summary["author"], "作者 (@u)")
            self.assertTrue(summary["is_article"])
            self.assertEqual(summary["images_saved"], 1)
            md = open(os.path.join(out_dir, "tweet.md"), encoding="utf-8").read()
            self.assertIn("# AI名词入门", md)
            self.assertIn("第一段", md)
            self.assertIn("![image](images/image-9.jpg)", md)
            self.assertNotIn("@@MEDIA", md)
            self.assertTrue(os.path.exists(os.path.join(out_dir, "images", "image-9.jpg")))
            # compact summary: the 49KB-class payload must stay on disk
            self.assertLess(len(proc.stdout), 2000)

    def test_plain_tweet_no_media_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"code": 200, "tweet": {
                "url": "https://x.com/u/status/123",
                "author": {"name": "N", "screen_name": "u"},
                "text": "hello world\nsecond line",
                "media": {"photos": [{"url": "https://img/p.jpg"}]},
            }}
            proc, out_dir = run_cli(payload, tmp, extra=["--no-media"])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads(proc.stdout)
            self.assertFalse(summary["is_article"])
            self.assertEqual(summary["images_saved"], 0)
            md = open(os.path.join(out_dir, "tweet.md"), encoding="utf-8").read()
            self.assertIn("hello world", md)
            self.assertIn("![image](images/image-1.jpg)", md)

    def test_api_error_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, _ = run_cli({"code": 404, "message": "NOT_FOUND"}, tmp)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(json.loads(proc.stdout)["status"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
