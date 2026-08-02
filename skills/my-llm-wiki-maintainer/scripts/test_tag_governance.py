"""Regression tests for tag governance detection and safe rewrite transactions."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SPEC = importlib.util.spec_from_file_location(
    "wiki_ops", Path(__file__).resolve().parent / "wiki_ops.py")
wo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wo)


def make_wiki(tmp: Path, pages: dict[str, list[str]]) -> Path:
    (tmp / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp / "raw" / "sources").mkdir(parents=True, exist_ok=True)
    (tmp / "purpose.md").write_text("# purpose\n", encoding="utf-8")
    (tmp / "schema.md").write_text("# schema\n", encoding="utf-8")
    for rel, tags in pages.items():
        path = tmp / "wiki" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(tags, ensure_ascii=False)
        path.write_text(
            f"---\ntype: source\ntitle: {path.stem}\ntags: {rendered}\n"
            "related: []\n---\n\n# Page\n\nBody.\n",
            encoding="utf-8",
        )
    return tmp


class FormatVariantTests(unittest.TestCase):
    def test_spacing_hyphen_case_variants_share_a_key(self) -> None:
        self.assertEqual(wo.tag_format_key("AI Workflow"), wo.tag_format_key("ai-workflow"))
        self.assertEqual(wo.tag_format_key("Agent 架构"), wo.tag_format_key("agent_架构"))
        self.assertEqual(
            wo._tag_near_duplicate("AI Workflow", "ai-workflow"), "format-variant")

    def test_semantic_punctuation_is_preserved(self) -> None:
        self.assertNotEqual(wo.tag_format_key("C++"), wo.tag_format_key("C#"))
        self.assertNotEqual(wo.tag_format_key("v2.1"), wo.tag_format_key("v21"))

    def test_audit_groups_candidates_by_executability(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = make_wiki(Path(td), {
                "sources/a.md": ["AI Workflow", "检索"],
                "sources/b.md": ["ai-workflow", "检索增强"],
            })
            vocab = wo.tag_vocabulary(root)
            self.assertEqual(len(vocab["candidateGroups"]["formatVariants"]), 1)
            self.assertEqual(len(vocab["candidateGroups"]["containment"]), 1)


class FrontmatterListTests(unittest.TestCase):
    def test_quoted_comma_is_one_tag_and_quote_style_survives(self) -> None:
        text = (
            "---\ntype: source\ntags: [\"RAG\", \"cost, quality\", \"AI Workflow\"]\n"
            "related: []\n---\nbody\n"
        )
        self.assertEqual(
            wo.extract_frontmatter_list(text, "tags"), ["RAG", "cost, quality", "AI Workflow"])
        updated = wo.set_frontmatter_list_preserving_style(
            text, "tags", ["RAG", "cost, quality", "ai-workflow"])
        self.assertIn('tags: ["RAG", "cost, quality", "ai-workflow"]', updated)

    def test_block_list_is_replaced_as_one_complete_field(self) -> None:
        text = (
            "---\ntype: source\ntags:\n  - 'AI Workflow'\n  - 'RAG'\n"
            "related: []\n---\nbody\n"
        )
        updated = wo.set_frontmatter_list_preserving_style(
            text, "tags", ["ai-workflow", "RAG"])
        self.assertIn("tags:\n  - 'ai-workflow'\n  - 'RAG'\nrelated:", updated)
        self.assertNotIn("'AI Workflow'", updated)


class GovernanceLintTests(unittest.TestCase):
    def test_lint_reports_limit_format_duplicates_and_page_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = make_wiki(Path(td), {
                "concepts/claude-code.md": ["tooling"],
                "sources/example.md": [
                    "Claude Code", "claude-code", "one", "two", "three", "four"
                ],
            })
            issue_types = {issue["type"] for issue in wo.collect_lint_issues(root)}
            self.assertIn("too-many-tags", issue_types)
            self.assertIn("duplicate-tag-format", issue_types)
            self.assertIn("tag-shadows-page", issue_types)


class TagRewriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_wiki(Path(self._tmp.name), {
            "sources/alpha.md": ["AI Workflow", "RAG"],
            "sources/beta.md": ["ai-workflow", "RAG"],
        })
        self.mapping = {
            "schemaVersion": 1,
            "root": str(self.root),
            "rules": [{
                "from": "AI Workflow",
                "to": "ai-workflow",
                "kind": "format",
                "expectedCount": 1,
                "expectedPages": ["sources/alpha.md"],
            }],
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_plan_apply_and_hash_guarded_rollback(self) -> None:
        plan = wo.build_tags_rewrite_plan(self.root, self.mapping)
        self.assertEqual(plan["pages"][0]["beforeTags"], ["AI Workflow", "RAG"])
        self.assertEqual(plan["pages"][0]["afterTags"], ["ai-workflow", "RAG"])
        plan_file = self.root / "plan.json"
        plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        alpha = self.root / "wiki" / "sources" / "alpha.md"
        if os.name != "nt":
            alpha.chmod(0o640)
            expected_mode = stat.S_IMODE(alpha.stat().st_mode)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = wo.cmd_tags_rewrite_apply(SimpleNamespace(
                project_root=str(self.root), plan=str(plan_file)))
        self.assertEqual(rc, 0)
        applied = json.loads(stdout.getvalue())
        manifest = Path(applied["manifest"])
        self.assertEqual(wo.extract_frontmatter_list(alpha.read_text(encoding="utf-8"), "tags"),
                         ["ai-workflow", "RAG"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(alpha.stat().st_mode), expected_mode)
        self.assertTrue(manifest.is_file())

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = wo.cmd_tags_rewrite_rollback(SimpleNamespace(
                project_root=str(self.root), manifest=str(manifest)))
        self.assertEqual(rc, 0)
        self.assertEqual(wo.extract_frontmatter_list(alpha.read_text(encoding="utf-8"), "tags"),
                         ["AI Workflow", "RAG"])

    def test_apply_refuses_page_changed_after_plan(self) -> None:
        plan = wo.build_tags_rewrite_plan(self.root, self.mapping)
        plan_file = self.root / "plan.json"
        plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        alpha = self.root / "wiki" / "sources" / "alpha.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            wo.cmd_tags_rewrite_apply(SimpleNamespace(
                project_root=str(self.root), plan=str(plan_file)))

    def test_rollback_refuses_page_changed_after_apply(self) -> None:
        plan = wo.build_tags_rewrite_plan(self.root, self.mapping)
        plan_file = self.root / "plan.json"
        plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            wo.cmd_tags_rewrite_apply(SimpleNamespace(
                project_root=str(self.root), plan=str(plan_file)))
        manifest = Path(json.loads(stdout.getvalue())["manifest"])
        alpha = self.root / "wiki" / "sources" / "alpha.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "newer user edit\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            wo.cmd_tags_rewrite_rollback(SimpleNamespace(
                project_root=str(self.root), manifest=str(manifest)))

    def test_plan_refuses_expected_page_drift(self) -> None:
        drifted = json.loads(json.dumps(self.mapping))
        drifted["rules"][0]["expectedPages"] = ["sources/not-alpha.md"]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            wo.build_tags_rewrite_plan(self.root, drifted)

    def test_plan_rejects_canonical_that_shadows_a_page(self) -> None:
        concept = self.root / "wiki" / "concepts" / "ai-workflow.md"
        concept.parent.mkdir(parents=True, exist_ok=True)
        concept.write_text(
            "---\ntype: concept\ntitle: AI Workflow\ntags: [tooling]\nrelated: []\n---\nbody\n",
            encoding="utf-8",
        )
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            wo.build_tags_rewrite_plan(self.root, self.mapping)


if __name__ == "__main__":
    unittest.main()
