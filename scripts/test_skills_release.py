from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts import build_skills_archive as archiver
from scripts import check_skills_release_version as guard
from scripts import gen_skills_version as generator
from scripts import skills_release


class SkillsReleaseMetadataTests(unittest.TestCase):
    def test_repository_metadata_is_valid(self) -> None:
        metadata = skills_release.load_metadata()
        self.assertEqual(metadata["schema"], 1)
        skills_release.version_key(metadata["pack_version"])

    def test_rejects_a_non_semver_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "skills-release.json"
            path.write_text(
                json.dumps({"schema": 1, "pack_version": "2.1"}), encoding="utf-8"
            )
            with self.assertRaises(skills_release.SkillsReleaseError):
                skills_release.load_metadata(path)

    def test_rejects_oversized_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "skills-release.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "pack_version": "2.1.0",
                        "pack_notes": "x" * (skills_release.NOTES_MAX + 1),
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(skills_release.SkillsReleaseError):
                skills_release.load_metadata(path)


class SkillsArchiveTests(unittest.TestCase):
    """The archive's contents are defined by the repository, not by the disk.

    These build a real (tiny) git repo laid out like the actual one, because the
    thing under test is precisely the git listing. A plain temp directory would
    exercise a code path the release never takes.
    """

    def _repo(self, root: Path) -> Path:
        """Repo root with a `skills/` tree plus the junk a real checkout carries."""
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        (root / ".gitignore").write_text(
            "skills/my-llm-wiki/data/\n.DS_Store\n__pycache__/\n", encoding="utf-8")
        skill = root / "skills" / "my-llm-wiki"
        (skill / "scripts" / "__pycache__").mkdir(parents=True)
        (skill / "data").mkdir(parents=True)
        (skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")
        (skill / "scripts" / "tool.py").write_text("print(1)\n", encoding="utf-8")
        # None of the following may ever reach the archive.
        (skill / "scripts" / "__pycache__" / "tool.pyc").write_bytes(b"bytecode")
        (skill / "data" / "personal-notes.md").write_text("private\n", encoding="utf-8")
        (skill / ".DS_Store").write_bytes(b"\x00junk")
        (skill / "scratch.md").write_text("untracked draft\n", encoding="utf-8")
        subprocess.run(["git", "add", "skills/my-llm-wiki/SKILL.md",
                        "skills/my-llm-wiki/scripts/tool.py"], cwd=root, check=True)
        return root / "skills"

    def test_archive_holds_tracked_files_only(self) -> None:
        # The regression: a directory walk shipped gitignored `data/` notes and
        # `.DS_Store` from whatever working tree happened to run the build.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._repo(root)
            destination = archiver.build(root / "skills-2.1.0.zip", source)
            with zipfile.ZipFile(destination) as archive:
                names = sorted(archive.namelist())
            self.assertEqual(
                names, ["my-llm-wiki/SKILL.md", "my-llm-wiki/scripts/tool.py"])

    def test_archive_is_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._repo(root)
            first = archiver.build(root / "first.zip", source).read_bytes()
            second = archiver.build(root / "second.zip", source).read_bytes()
            self.assertEqual(first, second)

    def test_empty_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            source = root / "skills"
            source.mkdir()
            with self.assertRaises(skills_release.SkillsReleaseError):
                archiver.build(root / "out.zip", source)

    def test_non_git_source_is_refused_rather_than_walked(self) -> None:
        # Falling back to a walk here would quietly restore the leak.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "skills" / "my-llm-wiki"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("# skill\n", encoding="utf-8")
            with self.assertRaises(skills_release.SkillsReleaseError):
                archiver.build(root / "out.zip", root / "skills")

    def test_tracked_but_deleted_file_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._repo(root)
            (source / "my-llm-wiki" / "SKILL.md").unlink()
            with self.assertRaises(skills_release.SkillsReleaseError):
                archiver.build(root / "out.zip", source)


class SkillsVersionSignalTests(unittest.TestCase):
    def _payload(self, temporary: str, **metadata: object) -> dict:
        root = Path(temporary)
        # A real repo, because the archive builder lists tracked files.
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        source = root / "skills"
        (source / "my-llm-wiki").mkdir(parents=True)
        (source / "my-llm-wiki" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
        subprocess.run(["git", "add", "skills/my-llm-wiki/SKILL.md"], cwd=root, check=True)
        archive = archiver.build(root / "skills.zip", source)
        path = root / "skills-release.json"
        path.write_text(
            json.dumps({"schema": 1, "pack_version": "2.1.0", **metadata}),
            encoding="utf-8",
        )
        return generator.build(
            archive,
            source_commit="a" * 40,
            released_at="2026-07-25T00:00:00+00:00",
            metadata_path=path,
        )

    def test_payload_pins_the_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = self._payload(temporary)
            self.assertEqual(payload["schema"], generator.SCHEMA)
            self.assertEqual(payload["pack_version"], "2.1.0")
            self.assertEqual(payload["source_commit"], "a" * 40)
            self.assertEqual(len(payload["sha256"]), 64)
            self.assertGreater(payload["size"], 0)
            self.assertGreater(payload["installed_size"], 0)

    def test_a_malformed_commit_is_dropped_rather_than_published(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            source = root / "skills"
            (source / "my-llm-wiki").mkdir(parents=True)
            (source / "my-llm-wiki" / "SKILL.md").write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "add", "skills/my-llm-wiki/SKILL.md"], cwd=root, check=True)
            archive = archiver.build(root / "skills.zip", source)
            path = root / "skills-release.json"
            path.write_text(
                json.dumps({"schema": 1, "pack_version": "2.1.0"}), encoding="utf-8"
            )
            payload = generator.build(
                archive, source_commit="not-a-sha", metadata_path=path
            )
            self.assertNotIn("source_commit", payload)

    def test_optional_fields_pass_through_only_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = self._payload(temporary)
            self.assertNotIn("min_app_version", payload)
            self.assertNotIn("pack_notes", payload)
        with tempfile.TemporaryDirectory() as temporary:
            payload = self._payload(
                temporary, min_app_version="2.0.14", pack_notes="  fixed preflight  "
            )
            self.assertEqual(payload["min_app_version"], "2.0.14")
            self.assertEqual(payload["pack_notes"], "fixed preflight")


class VersionBumpGuardTests(unittest.TestCase):
    """The guard is what keeps a shipped skill fix reachable, so exercise it
    against a real repository rather than a mocked ``git``."""

    def _repository(self, root: Path) -> None:
        def run(*args: str) -> None:
            subprocess.run(
                ["git", *args], cwd=root, check=True, capture_output=True, text=True
            )

        run("init", "-q", "-b", "main")
        run("config", "user.email", "test@example.invalid")
        run("config", "user.name", "test")
        (root / "registry").mkdir()
        (root / "skills" / "my-llm-wiki").mkdir(parents=True)
        self._write_version(root, "2.1.0")
        (root / "skills" / "my-llm-wiki" / "SKILL.md").write_text(
            "# skill\n", encoding="utf-8"
        )
        run("add", "-A")
        run("commit", "-qm", "base")

    def _write_version(self, root: Path, version: str) -> None:
        (root / "registry" / "skills-release.json").write_text(
            json.dumps({"schema": 1, "pack_version": version}) + "\n",
            encoding="utf-8",
        )

    def _guard(self, root: Path, base: str) -> int:
        with mock.patch.object(guard, "ROOT", root), mock.patch.object(
            guard, "SKILLS_RELEASE", root / "registry" / "skills-release.json"
        ), mock.patch(
            "sys.argv", ["check_skills_release_version", "--base", base]
        ):
            return guard.main()

    def test_changed_skill_without_a_bump_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._repository(root)
            (root / "skills" / "my-llm-wiki" / "SKILL.md").write_text(
                "# skill, fixed\n", encoding="utf-8"
            )
            with self.assertRaises(SystemExit) as raised:
                self._guard(root, "HEAD")
            self.assertIn("without a higher", str(raised.exception))

    def test_changed_skill_with_a_bump_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._repository(root)
            (root / "skills" / "my-llm-wiki" / "SKILL.md").write_text(
                "# skill, fixed\n", encoding="utf-8"
            )
            self._write_version(root, "2.2.0")
            self.assertEqual(self._guard(root, "HEAD"), 0)

    def test_a_lower_version_is_not_mistaken_for_a_bump(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._repository(root)
            (root / "skills" / "my-llm-wiki" / "SKILL.md").write_text(
                "# skill, fixed\n", encoding="utf-8"
            )
            self._write_version(root, "2.0.9")
            with self.assertRaises(SystemExit):
                self._guard(root, "HEAD")

    def test_untouched_skills_need_no_bump(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._repository(root)
            (root / "README.md").write_text("unrelated\n", encoding="utf-8")
            self.assertEqual(self._guard(root, "HEAD"), 0)


if __name__ == "__main__":
    unittest.main()
