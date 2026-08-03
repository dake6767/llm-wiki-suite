#!/usr/bin/env python3
"""Unit tests for deterministic helpers in wiki_ops.py.

Run: python3 scripts/test_chunker.py
Validates the highest-risk pieces of the map-reduce and Browser-share paths
before they reach an LLM.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wiki_ops as w  # noqa: E402


def body_of(chunks):
    return "".join(c["text"] for c in chunks)


class TestSplitMarkdown(unittest.TestCase):
    def test_heading_aligned_packing(self):
        # Many small H2 sections should pack into far fewer sub-budget chunks.
        secs = [f"## Section {i}\n" + ("lorem ipsum dolor sit amet. " * 8) + "\n" for i in range(20)]
        body = "# Report\n\nintro paragraph.\n\n" + "".join(secs)
        chunks = w.split_markdown(body, chunk_chars=800, overlap=100)
        self.assertGreater(len(chunks), 1)
        self.assertLess(len(chunks), 20, "small sections must be packed, not 1-per-chunk")
        for c in chunks:
            # packed chunks may slightly exceed budget only by one trailing segment
            self.assertIsNone(c["part"])
            self.assertIn("breadcrumb", c)

    def test_breadcrumb_nesting(self):
        body = (
            "# Top\n\nlead\n\n"
            "## Chapter A\n\na body\n\n"
            "### A.1 detail\n\n" + ("x " * 50) + "\n\n"
            "## Chapter B\n\nb body\n"
        )
        chunks = w.split_markdown(body, chunk_chars=60, overlap=10)
        crumbs = [c["breadcrumb"] for c in chunks]
        joined = " || ".join(crumbs)
        self.assertIn("Top > Chapter A > A.1 detail", joined)
        self.assertTrue(any(cb.endswith("Chapter B") for cb in crumbs))

    def test_oversized_headingless_block_windows(self):
        big = "word " * 4000  # ~20k chars, one heading, no internal headings
        body = "## Lonely\n\n" + big + "\n"
        chunks = w.split_markdown(body, chunk_chars=4000, overlap=200)
        self.assertGreater(len(chunks), 1, "oversized segment must be windowed")
        parts = [c["part"] for c in chunks]
        self.assertEqual(parts, list(range(len(chunks))), "part indices must be sequential")
        for c in chunks:
            self.assertTrue(c["breadcrumb"].endswith("Lonely"), "windows keep parent breadcrumb")

    def test_overlap_present_between_windows(self):
        # Realistic newline-separated prose (the windower breaks at line
        # boundaries, never mid-line, so words/sentences stay intact).
        big = "".join(f"sentence number {i} goes right here.\n" for i in range(1000))
        body = "## Solo\n\n" + big
        chunks = w.split_markdown(body, chunk_chars=4000, overlap=300)
        self.assertGreater(len(chunks), 1)
        # The last full line of window N should reappear in window N+1 (overlap).
        last_line = chunks[0]["text"].strip().splitlines()[-1]
        self.assertIn(last_line, chunks[1]["text"], "overlap tail should carry into next window")

    def test_fence_never_split(self):
        # A fenced code block longer than the budget must stay intact.
        code = "```\n" + "\n".join(f"code_line_{i}" for i in range(800)) + "\n```\n"
        body = "## Code\n\nintro\n\n" + code + "\ntrailer\n"
        chunks = w.split_markdown(body, chunk_chars=2000, overlap=100)
        # Exactly one chunk must contain the opening fence, and that same chunk
        # must also contain the closing fence (fence not split across chunks).
        owners = [i for i, c in enumerate(chunks) if "```\ncode_line_0" in c["text"] or "code_line_0" in c["text"]]
        self.assertTrue(owners, "fence content must exist somewhere")
        owner = owners[0]
        self.assertIn("code_line_799", chunks[owner]["text"], "closing of fenced block must stay with its opening")

    def test_heading_inside_fence_ignored(self):
        body = (
            "## Real\n\nbody\n\n"
            "```\n# not a heading\n## also not\n```\n\n"
            "## AlsoReal\n\nmore\n"
        )
        # Verify at the segmentation layer: fenced `#`/`##` lines must NOT become
        # heading boundaries, so exactly two real H2 segments exist.
        titles = [s["title"] for s in w._scan_segments(body) if s["level"] > 0]
        self.assertEqual(titles, ["Real", "AlsoReal"])

    def test_determinism(self):
        body = "# A\n\n" + ("para. " * 100) + "\n\n## B\n\n" + ("text " * 100) + "\n"
        a = w.split_markdown(body, chunk_chars=500, overlap=50)
        b = w.split_markdown(body, chunk_chars=500, overlap=50)
        self.assertEqual([c["text"] for c in a], [c["text"] for c in b])
        self.assertEqual([w.hash_text(c["text"]) for c in a], [w.hash_text(c["text"]) for c in b])

    def test_no_content_lost_in_packed_chunks(self):
        # When no segment is oversized (no windowing/overlap), concatenated chunk
        # text must reconstruct the body exactly.
        body = "# A\n\n" + ("para one. " * 20) + "\n\n## B\n\n" + ("para two. " * 20) + "\n"
        chunks = w.split_markdown(body, chunk_chars=300, overlap=0)
        self.assertTrue(all(c["part"] is None for c in chunks))
        self.assertEqual(body_of(chunks), body)

    def test_empty_body(self):
        self.assertEqual(w.split_markdown("", chunk_chars=100, overlap=10), [])


class TestSizeGateHelpers(unittest.TestCase):
    def test_cjk_ratio_extremes(self):
        self.assertLess(w.cjk_ratio("the quick brown fox jumps"), 0.05)
        self.assertGreater(w.cjk_ratio("这是一份很长的中文报告内容"), 0.9)

    def test_chars_per_token_blend(self):
        self.assertAlmostEqual(w.chars_per_token(0.0), 4.0, places=6)
        self.assertAlmostEqual(w.chars_per_token(1.0), 1.6, places=6)
        # clamped
        self.assertAlmostEqual(w.chars_per_token(2.0), 1.6, places=6)

    def test_cjk_doc_estimates_more_tokens_than_latin_same_chars(self):
        n = 5000
        latin = "a " * (n // 2)
        cjk = "中" * n
        est_latin = len(latin) / w.chars_per_token(w.cjk_ratio(latin))
        est_cjk = len(cjk) / w.chars_per_token(w.cjk_ratio(cjk))
        self.assertGreater(est_cjk, est_latin, "dense CJK estimates more tokens per char")


class TestBrowserShareHelpers(unittest.TestCase):
    def test_normalize_browser_page_path(self):
        self.assertEqual(
            w._normalize_browser_page_path("wiki/sources/WorkBuddy-从入门到榨干.md"),
            "sources/WorkBuddy-从入门到榨干",
        )
        self.assertEqual(
            w._normalize_browser_page_path("page/sources/example.md"),
            "sources/example",
        )
        self.assertIsNone(w._normalize_browser_page_path("../escape.md"))

    def test_online_page_url_uses_opaque_ref_when_supported(self):
        url = w._online_page_url(
            "https://wiki.htmlgo.to/DEMO_UID/?token=secret",
            "llm-wiki",
            "sources/WorkBuddy-从入门到榨干",
            opaque_ref=True,
        )
        self.assertEqual(
            url,
            "https://wiki.htmlgo.to/DEMO_UID/w/llm-wiki/open/"
            f"{w._browser_page_ref('sources/WorkBuddy-从入门到榨干')}"
            "?token=secret",
        )
        self.assertNotIn("%E4", url)

    def test_browser_page_ref_is_stable_for_cjk_title(self):
        self.assertEqual(
            w._browser_page_ref("sources/汕头失落的特区经济学人7月长文"),
            "c291cmNlcy_msZXlpLTlpLHokL3nmoTnibnljLrnu4_mtY7lrabkuro35pyI6ZW_5paH",
        )

    def test_online_page_url_encodes_spaces_and_question_marks(self):
        url = w._online_page_url(
            "https://wiki.htmlgo.to/DEMO_UID/?token=secret",
            "llm-wiki",
            "sources/大家都能用 - AI - 写代码了程序员的价值在哪儿？",
        )
        self.assertIn("%20-%20AI%20-%20", url)
        self.assertIn("%EF%BC%9F?token=secret", url)
        self.assertNotIn("？token=", url)

    def test_markdown_link_uses_short_label(self):
        self.assertEqual(
            w._markdown_link("https://wiki.example/p?token=secret", "点击查看总结"),
            "[点击查看总结](https://wiki.example/p?token=secret)",
        )

    def test_browser_page_exists_is_confined_to_derived_wiki(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "wiki" / "sources" / "中文总结.md"
            page.parent.mkdir(parents=True)
            page.write_text("# 中文总结\n", encoding="utf-8")
            self.assertTrue(w._browser_page_exists(root, "sources/中文总结"))
            self.assertFalse(w._browser_page_exists(root, "sources/不存在"))
            self.assertFalse(w._browser_page_exists(root, "../outside"))
            self.assertIsNone(w._browser_page_exists(None, "sources/中文总结"))


class TestRetrievalSearch(unittest.TestCase):
    def setUp(self):
        self.args = Namespace(q="乾隆", top=8)

    def test_browser_result_wins_without_local_scan(self):
        browser = {"available": True, "backend": "browser", "hits": [{"file": "wiki/a.md"}]}
        with mock.patch.object(w, "_browser_search_result", return_value=browser), \
             mock.patch.object(w, "_local_search_result") as local:
            result = w._retrieval_search_result(self.args)
        self.assertEqual(result, browser)
        local.assert_not_called()

    def test_unavailable_browser_falls_back_to_local(self):
        local_result = {"available": True, "backend": "local", "hits": []}
        with mock.patch.object(
            w, "_browser_search_result",
            return_value={"available": False, "reason": "browser-unavailable", "hits": []},
        ), mock.patch.object(w, "_local_search_result", return_value=local_result):
            result = w._retrieval_search_result(self.args)
        self.assertEqual(result["backend"], "local")
        self.assertEqual(result["fallbackFrom"], "browser")
        self.assertEqual(result["fallbackReason"], "browser-unavailable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
