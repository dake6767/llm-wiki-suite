#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import initialize_wiki  # noqa: E402


class InitializeWikiTests(unittest.TestCase):
    @staticmethod
    def config(root: Path) -> dict:
        return {
            "wiki_registry_path": str(root / "state" / "wikis.json"),
            "default_wiki_root": str(root / "wikis" / "my-llm-wiki"),
        }

    def test_missing_registry_initializes_and_registers_default_wiki(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            registry = Path(config["wiki_registry_path"])
            with mock.patch.dict(
                os.environ, {"LLM_WIKI_REGISTRY": str(registry)}, clear=False
            ):
                initialized = initialize_wiki.ensure_wiki(config)

            data = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(initialized, Path(config["default_wiki_root"]).resolve())
            self.assertTrue((initialized / "schema.md").is_file())
            self.assertTrue((initialized / "wiki").is_dir())
            self.assertEqual(len(data["wikis"]), 1)
            self.assertEqual(Path(data["wikis"][0]["path"]), initialized)
            self.assertTrue(data["wikis"][0]["default"])

    def test_existing_usable_wiki_is_reused_without_creating_another(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            existing = root / "existing"
            (existing / "wiki").mkdir(parents=True)
            (existing / "schema.md").write_text("schema", encoding="utf-8")
            registry = Path(config["wiki_registry_path"])
            registry.parent.mkdir(parents=True)
            registry.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "wikis": [
                            {
                                "path": str(existing),
                                "name": "existing",
                                "description": "",
                                "default": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            selected = initialize_wiki.ensure_wiki(config)

            self.assertEqual(selected, existing.resolve())
            self.assertFalse(Path(config["default_wiki_root"]).exists())

    def test_dry_run_reports_default_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)

            selected = initialize_wiki.ensure_wiki(config, dry_run=True)

            self.assertEqual(selected, Path(config["default_wiki_root"]).resolve())
            self.assertFalse(Path(config["wiki_registry_path"]).exists())
            self.assertFalse(selected.exists())

    def test_malformed_registry_fails_without_overwriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            registry = Path(config["wiki_registry_path"])
            registry.parent.mkdir(parents=True)
            registry.write_text("not-json", encoding="utf-8")

            with self.assertRaises(initialize_wiki.WikiInitializationError):
                initialize_wiki.ensure_wiki(config)

            self.assertEqual(registry.read_text(encoding="utf-8"), "not-json")
            self.assertFalse(Path(config["default_wiki_root"]).exists())


if __name__ == "__main__":
    unittest.main()
