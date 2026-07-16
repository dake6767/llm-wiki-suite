#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import install  # noqa: E402
import install_state  # noqa: E402


class InstallProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads(
            (ROOT / "registry" / "skills.json").read_text(encoding="utf-8")
        )

    def config(self, root: Path) -> dict:
        return {
            "version": 4,
            "agent_hosts": {
                "demo": {
                    "detect_dir": str(root / "agent"),
                    "skills_dir": str(root / "agent" / "skills"),
                }
            },
            "install": {
                "lock_file": str(root / "install.lock"),
                "backup_dir_name": ".llm-wiki-backups",
            },
        }

    def test_named_host_resolves_only_registry_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            targets = install.resolve_targets(self.config(root), ["demo"], [])
        self.assertEqual(targets[0]["path"], (root / "agent" / "skills").resolve())

    def test_unknown_host_fails_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(install.InstallError, "unknown host"):
                install.build_plan(
                    self.config(root), self.registry, ["missing"], [],
                    ["cn-mirrors"], "link", False,
                )
            self.assertFalse((root / "agent").exists())

    def test_custom_target_inside_source_checkout_is_rejected(self) -> None:
        with self.assertRaisesRegex(install.InstallError, "unsafe custom target"):
            install.resolve_targets(self.config(ROOT), [], [str(ROOT / "generated")])

    def test_skill_digest_is_computed_once_for_multiple_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            config["agent_hosts"]["other"] = {
                "detect_dir": str(root / "other"),
                "skills_dir": str(root / "other" / "skills"),
            }
            with mock.patch.object(
                install, "content_digest", wraps=install.content_digest
            ) as digest:
                install.build_plan(
                    config, self.registry, ["demo", "other"], [],
                    ["cn-mirrors"], "link", False,
                )
            self.assertEqual(digest.call_count, 1)

    def test_concurrent_operation_fails_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "operation.lock"
            with install_state.advisory_lock(lock):
                with self.assertRaises(install_state.LockUnavailable):
                    with install_state.advisory_lock(lock):
                        self.fail("second lock must not be acquired")

    def test_conflict_preflight_makes_no_partial_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "skills"
            conflict = target / "cn-mirrors"
            conflict.mkdir(parents=True)
            marker = conflict / "owned-by-user"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(install.InstallError, "no destination changes"):
                install.build_plan(
                    self.config(root), self.registry, [], [str(target)],
                    ["cn-mirrors"], "link", False,
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_copy_is_current_only_while_content_matches_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "skills"
            config = self.config(root)
            plan = install.build_plan(
                config, self.registry, [], [str(target)], ["cn-mirrors"], "copy", False
            )
            install.apply_plan(config, plan)
            current = install.build_plan(
                config, self.registry, [], [str(target)], ["cn-mirrors"], "copy", False
            )
            self.assertEqual(current["actions"][0]["state"], "current")
            (target / "cn-mirrors" / "SKILL.md").write_text("mutated", encoding="utf-8")
            with self.assertRaisesRegex(install.InstallError, "--replace"):
                install.build_plan(
                    config, self.registry, [], [str(target)], ["cn-mirrors"], "copy", False
                )

    def test_failure_rolls_back_every_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "skills"
            config = self.config(root)
            plan = install.build_plan(
                config,
                self.registry,
                [],
                [str(target)],
                ["my-llm-wiki-video"],
                "link",
                False,
            )
            calls = 0

            def fail_second(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated link failure")
                destination.symlink_to(source, target_is_directory=True)

            with mock.patch.object(install, "make_link", side_effect=fail_second):
                with self.assertRaisesRegex(
                    install.InstallOperationError, "all changes rolled back"
                ):
                    install.apply_plan(config, plan)
            for action in plan["actions"]:
                self.assertFalse(Path(action["destination"]).exists())
                self.assertFalse(Path(action["destination"]).is_symlink())

    def test_replacement_backup_stays_on_target_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "skills"
            old = target / "cn-mirrors"
            old.mkdir(parents=True)
            (old / "owned-by-user").write_text("keep", encoding="utf-8")
            config = self.config(root)
            plan = install.build_plan(
                config, self.registry, [], [str(target)],
                ["cn-mirrors"], "link", True,
            )
            install.apply_plan(config, plan)
            action = plan["actions"][0]
            backup = Path(action["backup"])
            self.assertEqual(backup.parent, target.resolve() / ".llm-wiki-backups")
            self.assertEqual(
                (backup / "owned-by-user").read_text(encoding="utf-8"), "keep"
            )
            self.assertTrue(install_state.is_linklike(Path(action["destination"])))

    def test_later_failure_restores_replaced_user_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "skills"
            original = target / "my-llm-wiki"
            original.mkdir(parents=True)
            marker = original / "owned-by-user"
            marker.write_text("keep", encoding="utf-8")
            config = self.config(root)
            plan = install.build_plan(
                config, self.registry, [], [str(target)],
                ["my-llm-wiki-video"], "link", True,
            )
            calls = 0

            def fail_second(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated link failure")
                destination.symlink_to(source, target_is_directory=True)

            with mock.patch.object(install, "make_link", side_effect=fail_second):
                with self.assertRaises(install.InstallOperationError):
                    install.apply_plan(config, plan)

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertFalse((target / "my-llm-wiki-video").exists())
            self.assertFalse((target / ".llm-wiki-backups").exists())


if __name__ == "__main__":
    unittest.main()
