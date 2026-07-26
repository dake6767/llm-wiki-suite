"""Regression tests for the tag vocabulary: tokenizing, scoping, promotion.

Every bug these cover was found by hand, one review round at a time, and each
was introduced by the fix for the previous one. That pattern is the reason this
file exists — the mechanism has enough moving parts (corpus-wide counts, a
scoped view, two failure modes for near-duplicates, three ways to pass a scope)
that manual spot-checks kept missing a case.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "wiki_ops", Path(__file__).resolve().parent / "wiki_ops.py")
wo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wo)


def make_wiki(tmp: Path, pages: dict[str, list[str]]) -> Path:
    """Minimal project root: {`entities/x.md`: [tags]} plus required files."""
    (tmp / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp / "raw" / "sources").mkdir(parents=True, exist_ok=True)
    (tmp / "purpose.md").write_text("# purpose\n", encoding="utf-8")
    (tmp / "schema.md").write_text("# schema\n", encoding="utf-8")
    for rel, tags in pages.items():
        path = tmp / "wiki" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = "[" + ", ".join(tags) + "]"
        path.write_text(
            f"---\ntype: concept\ntitle: {path.stem}\ntags: {rendered}\n---\n\n"
            f"# {path.stem}\n\nBody about {path.stem}.\n", encoding="utf-8")
    return tmp


class TokenizerTests(unittest.TestCase):
    def test_punctuation_is_a_separator_not_a_term(self) -> None:
        # `/` used to become its own term, and it occurs in every body
        # wikilink, so any query containing a slash matched the whole corpus.
        self.assertEqual(wo.tag_query_terms("alpha / beta"), ["alpha", "beta"])
        self.assertNotIn("/", wo.tag_query_terms("alpha / beta"))

    def test_cjk_punctuation_splits(self) -> None:
        # No whitespace, so a whitespace-only split made this one dead term.
        self.assertEqual(wo.tag_query_terms("检索增强，向量库"), ["检索增强", "向量库"])
        self.assertEqual(wo.tag_query_terms("检索增强、向量库；嵌入"),
                         ["检索增强", "向量库", "嵌入"])

    def test_spacing_and_punctuation_agree(self) -> None:
        self.assertEqual(wo.tag_query_terms("检索增强 向量库"),
                         wo.tag_query_terms("检索增强，向量库"))

    def test_hyphens_and_dots_stay_inside_tokens(self) -> None:
        self.assertEqual(wo.tag_query_terms("agent-skills claude-code v2.1"),
                         ["agent-skills", "claude-code", "v2.1"])

    def test_trailing_plus_and_hash_survive(self) -> None:
        # Stripping these turned both into a bare `c`, which the length filter
        # then dropped, rejecting two ordinary technical subjects outright.
        self.assertEqual(wo.tag_query_terms("C++"), ["c++"])
        self.assertEqual(wo.tag_query_terms("C#"), ["c#"])
        self.assertEqual(wo.tag_query_terms("C++ 模板"), ["c++", "模板"])

    def test_trailing_sentence_punctuation_is_trimmed(self) -> None:
        self.assertEqual(wo.tag_query_terms("alpha. beta-"), ["alpha", "beta"])

    def test_single_characters_and_pure_punctuation_yield_nothing(self) -> None:
        self.assertEqual(wo.tag_query_terms("/ , 。"), [])
        self.assertEqual(wo.tag_query_terms("a 的"), [])
        self.assertEqual(wo.tag_query_terms(""), [])


class NearDuplicateTests(unittest.TestCase):
    def test_case_variants(self) -> None:
        self.assertEqual(wo._tag_near_duplicate("AI", "ai"), "case-variant")

    def test_cjk_expansion_is_caught_by_overlap_not_substring(self) -> None:
        # The shared characters are not contiguous ("大…模型"), so containment
        # cannot see this pair at all — character-set overlap is what catches it.
        self.assertNotIn("大模型", "大语言模型")
        self.assertNotIn("大语言模型", "大模型")
        self.assertEqual(wo._tag_near_duplicate("大模型", "大语言模型"), "char-overlap 0.60")

    def test_cjk_specialization_is_caught_by_containment(self) -> None:
        # Jaccard is only 0.50 here — below threshold — so containment carries it.
        self.assertEqual(wo._tag_near_duplicate("检索", "检索增强"), "containment")

    def test_merely_adjacent_terms_are_left_alone(self) -> None:
        self.assertIsNone(wo._tag_near_duplicate("大模型", "模型压缩"))
        self.assertIsNone(wo._tag_near_duplicate("数据标注", "模型训练"))

    def test_short_ascii_is_not_swallowed_by_containment(self) -> None:
        self.assertIsNone(wo._tag_near_duplicate("ai", "chair"))

    def test_semantic_synonyms_are_out_of_scope(self) -> None:
        # Documented limitation: a lexical test cannot see this one.
        self.assertIsNone(wo._tag_near_duplicate("大模型", "LLM"))


class VocabularyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_wiki(Path(self._tmp.name), {
            "concepts/alpha.md": ["shared", "alpha-only"],
            "concepts/beta.md": ["shared", "beta-only"],
            "entities/gamma.md": ["gamma-only"],
        })

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_established_and_singletons_split_on_page_count(self) -> None:
        vocab = wo.tag_vocabulary(self.root)
        self.assertEqual([e["tag"] for e in vocab["established"]], ["shared"])
        self.assertEqual(sorted(vocab["singletons"]),
                         ["alpha-only", "beta-only", "gamma-only"])

    def test_scoped_view_exposes_singletons_for_promotion(self) -> None:
        # The promotion path: a tag coined on one page must be visible to the
        # next ingest, or it can never reach two pages and the vocabulary only
        # ever recycles what is already popular.
        vocab = wo.tag_vocabulary(self.root, scope_paths=["concepts/alpha.md"])
        by_tag = {r["tag"]: r for r in vocab["relevant"]}
        self.assertIn("alpha-only", by_tag)
        self.assertTrue(by_tag["alpha-only"]["new"])
        self.assertFalse(by_tag["shared"]["new"])
        self.assertNotIn("beta-only", by_tag)

    def test_scope_accepts_paths_with_or_without_wiki_prefix(self) -> None:
        bare = wo.tag_vocabulary(self.root, scope_paths=["concepts/alpha.md"])
        prefixed = wo.tag_vocabulary(self.root, scope_paths=["wiki/concepts/alpha.md"])
        self.assertEqual([r["tag"] for r in bare["relevant"]],
                         [r["tag"] for r in prefixed["relevant"]])

    def test_counts_stay_corpus_wide_inside_a_scope(self) -> None:
        vocab = wo.tag_vocabulary(self.root, scope_paths=["concepts/alpha.md"])
        shared = next(r for r in vocab["relevant"] if r["tag"] == "shared")
        self.assertEqual(shared["count"], 2, "scoping must narrow selection, not counts")

    def test_empty_scope_is_not_the_same_as_no_scope(self) -> None:
        unscoped = wo.tag_vocabulary(self.root)
        empty = wo.tag_vocabulary(self.root, scope_paths=[])
        self.assertEqual(unscoped["relevant"], [])
        self.assertEqual(empty["relevant"], [],
                         "an empty scope must yield nothing, never the global list")
        self.assertTrue(unscoped["established"])

    def test_duplicate_scan_is_skippable(self) -> None:
        self.assertFalse(wo.tag_vocabulary(self.root, with_duplicates=False)["duplicatesComputed"])
        self.assertTrue(wo.tag_vocabulary(self.root)["duplicatesComputed"])

    def test_untagged_pages_are_reported(self) -> None:
        (self.root / "wiki" / "concepts" / "bare.md").write_text(
            "---\ntype: concept\ntitle: bare\ntags: []\n---\n\n# bare\n", encoding="utf-8")
        self.assertIn("concepts/bare.md", wo.tag_vocabulary(self.root)["untaggedPages"])


class MissingTagsTests(unittest.TestCase):
    def test_all_empty_shapes_are_defects(self) -> None:
        for frontmatter in ("tags: []", "tags:", ""):
            with self.subTest(frontmatter=frontmatter):
                self.assertTrue(wo.missing_tags(f"---\ntype: concept\n{frontmatter}\n---\nbody"))

    def test_real_tags_pass(self) -> None:
        self.assertFalse(wo.missing_tags("---\ntype: concept\ntags: [a, b]\n---\nbody"))
        self.assertFalse(wo.missing_tags("---\ntype: concept\ntags:\n  - a\n  - b\n---\nbody"))

    def test_does_not_double_report_with_raw_tag_leaks(self) -> None:
        empty = "---\ntype: concept\ntags: []\n---\nbody"
        self.assertTrue(wo.missing_tags(empty))
        self.assertEqual(wo.raw_tag_leaks(empty), ([], []),
                         "empty tags must be reported once, by missing_tags only")


if __name__ == "__main__":
    unittest.main()
