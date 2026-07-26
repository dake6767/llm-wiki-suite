#!/usr/bin/env python3
"""Regression tests for collection-aware, routing-complete Wiki creation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
INIT = SCRIPTS / "init_wiki.py"
WIKIS = SCRIPTS / "wikis.py"
MAINTAINER_OPS = (
    SCRIPTS.parent.parent
    / "my-llm-wiki-maintainer"
    / "scripts"
    / "wiki_ops.py"
)


class WikiCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.suite_home = self.root / "suite-home"
        self.registry = self.suite_home / "wikis.json"
        self.env = {**os.environ, "LLM_WIKI_REGISTRY": str(self.registry)}

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def make_wiki(path: Path) -> None:
        (path / "wiki").mkdir(parents=True)
        (path / "schema.md").write_text("# schema\n", encoding="utf-8")

    def write_registry(self, entries: list[dict]) -> None:
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        self.registry.write_text(
            json.dumps({"version": 1, "wikis": entries}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_collection_root_prefers_the_wiki_recorded_by_setup(self) -> None:
        initialized = self.root / "chosen-collection" / "my-llm-wiki"
        other = self.root / "other-place" / "work-wiki"
        self.make_wiki(initialized)
        self.make_wiki(other)
        self.write_registry(
            [
                {
                    "path": str(initialized),
                    "name": "初始",
                    "description": "通用",
                    "default": False,
                },
                {
                    "path": str(other),
                    "name": "工作",
                    "description": "工作",
                    "default": True,
                },
            ]
        )
        (self.suite_home / "setup-state.json").write_text(
            json.dumps({"wiki_path": str(initialized)}),
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, str(WIKIS), "collection-root", "--json"],
            capture_output=True,
            text=True,
            env=self.env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual(Path(info["collection_root"]), initialized.parent)
        self.assertEqual(info["source"], "setup-state")

    def test_slug_creates_a_sibling_and_registers_its_topic(self) -> None:
        initialized = self.root / "wikis" / "my-llm-wiki"
        self.make_wiki(initialized)
        self.write_registry(
            [
                {
                    "path": str(initialized),
                    "name": "默认",
                    "description": "通用知识",
                    "default": True,
                }
            ]
        )
        self.suite_home.joinpath("setup-state.json").write_text(
            json.dumps({"wiki_path": str(initialized)}),
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                sys.executable,
                str(INIT),
                "--slug",
                "history-wiki",
                "--name",
                "中外历史",
                "--description",
                "中国史 / 世界史 / 历史人物 / 制度与事件",
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        created = initialized.parent / "history-wiki"
        self.assertTrue(created.joinpath("schema.md").is_file())
        self.assertTrue(created.joinpath("wiki", "index.md").is_file())
        self.assertIn("中国史 / 世界史", created.joinpath("purpose.md").read_text(encoding="utf-8"))
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        entry = next(item for item in registry["wikis"] if item["path"] == str(created))
        self.assertEqual(entry["name"], "中外历史")
        self.assertEqual(entry["description"], "中国史 / 世界史 / 历史人物 / 制度与事件")
        self.assertFalse(entry["default"])

    def test_initializer_requires_a_routing_description(self) -> None:
        result = subprocess.run(
            [sys.executable, str(INIT), "--path", str(self.root / "unrouted")],
            capture_output=True,
            text=True,
            env=self.env,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--description", result.stderr)
        self.assertFalse(self.root.joinpath("unrouted").exists())

        empty = subprocess.run(
            [
                sys.executable,
                str(INIT),
                "--path",
                str(self.root / "empty-description"),
                "--description",
                "   ",
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertNotEqual(empty.returncode, 0)
        self.assertIn("non-empty topical routing scope", empty.stderr)
        self.assertFalse(self.root.joinpath("empty-description").exists())

    def test_registration_failure_is_not_reported_as_success(self) -> None:
        blocker = self.root / "not-a-directory"
        blocker.write_text("occupied", encoding="utf-8")
        target = self.root / "partially-created"
        result = subprocess.run(
            [
                sys.executable,
                str(INIT),
                "--path",
                str(target),
                "--description",
                "测试路由注册失败",
            ],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "LLM_WIKI_REGISTRY": str(blocker / "wikis.json"),
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("routing registration failed", result.stderr)
        self.assertTrue(target.joinpath("schema.md").is_file())
        self.assertNotIn("status: initialized", result.stdout)

    def test_maintainer_legacy_init_delegates_to_registered_initializer(self) -> None:
        target = self.root / "explicit" / "history-wiki"
        result = subprocess.run(
            [
                sys.executable,
                str(MAINTAINER_OPS),
                "init",
                str(target),
                "--name",
                "历史",
                "--description",
                "中外历史",
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertEqual(registry["wikis"][0]["path"], str(target))
        self.assertEqual(registry["wikis"][0]["description"], "中外历史")


if __name__ == "__main__":
    unittest.main(verbosity=2)
