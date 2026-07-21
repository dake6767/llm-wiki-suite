#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import argparse
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import agent_install  # noqa: E402


class AgentInstallProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads(
            (ROOT / "registry" / "skills.json").read_text(encoding="utf-8")
        )

    @staticmethod
    def config(root: Path) -> dict:
        return {
            "version": 5,
            "home": str(root / "home"),
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
            "wiki_registry_path": str(root / "state" / "wikis.json"),
            "default_wiki_root": str(root / "wikis" / "my-llm-wiki"),
        }

    @staticmethod
    def toolchain_all_satisfied() -> dict:
        return {
            "status": "ok",
            "network": {"ecosystems": {"system": "global"}},
            "profiles": [],
            "tools": {
                name: {"status": "ok", "path": f"/tools/{name}"}
                for names in agent_install.COMPONENT_TOOLS.values()
                for name in names
            },
            "capabilities": {},
            "recommendations": [],
        }

    def inspection(self, root: Path, requested=None) -> dict:
        with mock.patch.object(
            agent_install.capture_preflight,
            "build_report",
            return_value=self.toolchain_all_satisfied(),
        ), mock.patch.object(
            agent_install.doctor,
            "check_browser",
            return_value={"status": "ok", "detail": "running"},
        ), mock.patch.object(
            agent_install.doctor,
            "browser_recommendation",
            return_value=None,
        ), mock.patch.object(
            agent_install.doctor,
            "check_opencli_extension",
            return_value={"status": "ok", "detail": "staged"},
        ), mock.patch.object(
            agent_install.doctor,
            "check_wiki",
            return_value={"status": "warn", "detail": "not initialized"},
        ):
            return agent_install.build_inspection(
                self.config(root), self.registry, requested
            )

    def selection(
        self,
        inspection: dict,
        *,
        replacements: list[str] | None = None,
    ) -> dict:
        return {
            "schema": 1,
            "inspection_id": inspection["inspection_id"],
            "hosts": ["demo"],
            "custom_targets": [],
            "skills": ["cn-mirrors"],
            "mode": "copy",
            "replace_destinations": replacements or [],
            "components": [],
            "browser": False,
            "host_configuration": {"hermes_hardening": False},
            "failure_policy": {
                "optional_components": "continue",
                "browser": "continue",
            },
        }

    def build_plan(self, root: Path, inspection: dict, selection: dict) -> dict:
        with mock.patch.object(
            agent_install.capture_preflight,
            "build_report",
            return_value=self.toolchain_all_satisfied(),
        ), mock.patch.object(
            agent_install.doctor,
            "check_browser",
            return_value={"status": "ok", "detail": "running"},
        ), mock.patch.object(
            agent_install.doctor,
            "browser_recommendation",
            return_value=None,
        ), mock.patch.object(
            agent_install.doctor,
            "check_opencli_extension",
            return_value={"status": "ok", "detail": "staged"},
        ):
            return agent_install.build_plan_document(
                self.config(root), self.registry, inspection, selection
            )

    def test_inspection_is_read_only_and_selects_no_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inspection = self.inspection(root, ["cn-mirrors"])
            host = inspection["hosts"][0]
            self.assertFalse(host["detected"])
            self.assertFalse(host["selected_by_default"])
            self.assertFalse((root / "agent").exists())
            self.assertFalse((root / "home").exists())
            self.assertFalse((root / "wikis").exists())

    def test_selection_rejects_unknown_authority_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inspection = self.inspection(root, ["cn-mirrors"])
            selection = self.selection(inspection)
            selection["replace_everything"] = True
            with self.assertRaisesRegex(agent_install.PlanningError, "unknown field"):
                agent_install.validate_selection(selection, inspection)

    def test_plan_requires_exact_conflict_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conflict = root / "agent" / "skills" / "cn-mirrors"
            conflict.mkdir(parents=True)
            (conflict / "user-data").write_text("keep", encoding="utf-8")
            inspection = self.inspection(root, ["cn-mirrors"])

            with self.assertRaisesRegex(agent_install.PlanningError, "--replace"):
                self.build_plan(root, inspection, self.selection(inspection))

            plan = self.build_plan(
                root,
                inspection,
                self.selection(inspection, replacements=[str(conflict)]),
            )
            self.assertEqual(
                plan["skills"]["used_replacements"],
                [agent_install._normalise_exact_path(str(conflict))],
            )
            self.assertEqual(plan["skills"]["actions"][0]["state"], "replace")
            self.assertEqual((conflict / "user-data").read_text(encoding="utf-8"), "keep")

    def test_plan_rejects_unused_replacement_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inspection = self.inspection(root, ["cn-mirrors"])
            unrelated = root / "agent" / "skills" / "not-a-conflict"
            with self.assertRaisesRegex(agent_install.PlanningError, "does not match"):
                self.build_plan(
                    root,
                    inspection,
                    self.selection(inspection, replacements=[str(unrelated)]),
                )

    def test_plan_hash_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inspection = self.inspection(root, ["cn-mirrors"])
            plan = self.build_plan(root, inspection, self.selection(inspection))
            agent_install.validate_plan(plan)
            plan["selection"]["browser"] = True
            with self.assertRaisesRegex(agent_install.ApplyError, "hash mismatch"):
                agent_install.validate_plan(plan)

    def test_command_output_redacts_tokens(self) -> None:
        value = agent_install._tail(
            "Authorization: Bearer secret-value "
            "http://127.0.0.1/?token=another-secret&x=1"
        )
        self.assertNotIn("secret-value", value)
        self.assertNotIn("another-secret", value)
        self.assertIn("<redacted>", value)

    def test_apply_creates_receipt_and_terminal_session_without_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            inspection = self.inspection(root, ["cn-mirrors"])
            plan = self.build_plan(root, inspection, self.selection(inspection))
            doctor_result = {"returncode": 0, "report": {"state": "ready"}}
            with mock.patch.dict(
                os.environ,
                {
                    "LLM_WIKI_INSTALL_HOME": str(root / "home"),
                    "LLM_WIKI_INSTALL_SESSION_ROOT": str(root / "sessions"),
                    "LLM_WIKI_REGISTRY": str(root / "state" / "wikis.json"),
                },
                clear=False,
            ), mock.patch.object(
                agent_install, "_run_doctor", return_value=doctor_result
            ), mock.patch(
                "builtins.input", side_effect=AssertionError("must not prompt")
            ):
                result, result_path = agent_install.apply_plan_document(
                    config, self.registry, plan
                )

            self.assertEqual(result["state"], "complete")
            self.assertTrue(result_path.is_file())
            self.assertTrue((root / "home" / "install-state.json").is_file())
            installed = root / "agent" / "skills" / "cn-mirrors"
            manifest = json.loads(
                (installed / ".llm-wiki-install.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["installer"], "agent-install-protocol-5")
            self.assertEqual(manifest["distribution"], "managed-pack")
            self.assertTrue((root / "wikis" / "my-llm-wiki" / "schema.md").is_file())

    def test_wiki_failure_rolls_back_new_skill_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            inspection = self.inspection(root, ["cn-mirrors"])
            plan = self.build_plan(root, inspection, self.selection(inspection))
            with mock.patch.dict(
                os.environ,
                {
                    "LLM_WIKI_INSTALL_HOME": str(root / "home"),
                    "LLM_WIKI_INSTALL_SESSION_ROOT": str(root / "sessions"),
                },
                clear=False,
            ), mock.patch.object(
                agent_install.initialize_wiki,
                "ensure_wiki",
                side_effect=RuntimeError("simulated wiki failure"),
            ):
                result, _ = agent_install.apply_plan_document(
                    config, self.registry, plan
                )

            self.assertEqual(result["state"], "rolled-back")
            self.assertFalse((root / "agent" / "skills" / "cn-mirrors").exists())
            self.assertFalse((root / "home" / "install-state.json").exists())

    def test_interrupted_session_recovery_removes_owned_partial_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            session = root / "sessions" / "interrupted"
            destination = root / "agent" / "skills" / "cn-mirrors"
            destination.mkdir(parents=True)
            agent_install.atomic_write_json(
                destination / ".llm-wiki-install.json",
                {"protocol": 5, "install_id": "install-one"},
            )
            agent_install.atomic_write_json(
                session / "journal.json",
                {
                    "schema": 1,
                    "protocol": 5,
                    "session_id": "interrupted",
                    "plan_id": "plan-one",
                    "plan_hash": "hash-one",
                    "state": "applying",
                    "started_at": agent_install.now(),
                },
            )
            agent_install.atomic_write_json(
                session / "skill-execution.json",
                {
                    "copy_manifest": {"protocol": 5, "install_id": "install-one"},
                    "actions": [
                        {
                            "state": "installed",
                            "destination": str(destination),
                            "backup": None,
                        }
                    ],
                },
            )
            with mock.patch.dict(
                os.environ,
                {
                    "LLM_WIKI_INSTALL_HOME": str(root / "home"),
                    "LLM_WIKI_INSTALL_SESSION_ROOT": str(root / "sessions"),
                },
                clear=False,
            ):
                recovered = agent_install._recover_interrupted_sessions(config, "active")
            self.assertEqual(recovered, [{"session_id": "interrupted", "state": "rolled-back"}])
            self.assertFalse(destination.exists())
            result = json.loads((session / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["state"], "rolled-back")

    def test_hermes_hardening_reuses_original_backup_on_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            config["agent_hosts"]["hermes"] = {
                "detect_dir": str(root / "hermes"),
                "skills_dir": str(root / "hermes" / "skills"),
            }
            hermes = root / "hermes" / "config.yaml"
            hermes.parent.mkdir(parents=True)
            hermes.write_text("approvals:\n  mode: manual\n", encoding="utf-8")
            first = agent_install._apply_hermes_hardening(config, "install-one")
            second = agent_install._apply_hermes_hardening(config, "install-one", first)
            self.assertTrue(first["changed_in_session"])
            self.assertFalse(second["changed_in_session"])
            self.assertEqual(second["backup"], first["backup"])
            self.assertEqual(len(list((root / "hermes" / ".llm-wiki-backups").iterdir())), 1)
            agent_install._rollback_host_configuration(second)
            self.assertIn("mode: smart", hermes.read_text(encoding="utf-8"))

    def test_repair_authorizes_only_mutated_receipt_owned_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            inspection = self.inspection(root, ["cn-mirrors"])
            plan = self.build_plan(root, inspection, self.selection(inspection))
            doctor_result = {"returncode": 0, "report": {"state": "ready"}}
            with mock.patch.dict(os.environ, {
                "LLM_WIKI_INSTALL_HOME": str(root / "home"),
                "LLM_WIKI_INSTALL_SESSION_ROOT": str(root / "sessions"),
                "LLM_WIKI_REGISTRY": str(root / "state" / "wikis.json"),
            }, clear=False), mock.patch.object(agent_install, "_run_doctor", return_value=doctor_result):
                result, _ = agent_install.apply_plan_document(config, self.registry, plan)
                self.assertEqual(result["state"], "complete")
                installed = root / "agent" / "skills" / "cn-mirrors"
                (installed / "SKILL.md").write_text("mutated", encoding="utf-8")
                next_inspection = self.inspection(root, ["cn-mirrors"])
                receipt = agent_install.read_receipt(config)
                selection = agent_install._repair_selection(next_inspection, receipt, None)
            self.assertEqual(
                selection["replace_destinations"],
                [agent_install._normalise_exact_path(str(installed))],
            )

    def test_uninstall_all_removes_owned_copy_but_preserves_wiki(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            inspection = self.inspection(root, ["cn-mirrors"])
            plan = self.build_plan(root, inspection, self.selection(inspection))
            doctor_result = {"returncode": 0, "report": {"state": "ready"}}
            with mock.patch.dict(os.environ, {
                "LLM_WIKI_INSTALL_HOME": str(root / "home"),
                "LLM_WIKI_INSTALL_SESSION_ROOT": str(root / "sessions"),
                "LLM_WIKI_REGISTRY": str(root / "state" / "wikis.json"),
            }, clear=False), mock.patch.object(agent_install, "_run_doctor", return_value=doctor_result), \
                    mock.patch.object(agent_install, "load_sources", return_value=(config, self.registry)):
                agent_install.apply_plan_document(config, self.registry, plan)
                code = agent_install.command_uninstall(
                    argparse.Namespace(all=True, selection=None, json=True)
                )
            self.assertEqual(code, 0)
            self.assertFalse((root / "agent" / "skills" / "cn-mirrors").exists())
            self.assertFalse((root / "home" / "install-state.json").exists())
            self.assertTrue((root / "wikis" / "my-llm-wiki" / "schema.md").is_file())


if __name__ == "__main__":
    unittest.main()
