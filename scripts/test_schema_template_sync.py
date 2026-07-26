"""Guard the two copies of the wiki schema template against silent divergence.

`my-llm-wiki/assets/schema.md` is what `init_wiki.py` copies into every new
wiki; `my-llm-wiki-maintainer/assets/templates/schema.md` backs the maintainer's
own Initialize flow. They are two files because skills ship as independent
packs, but they must hold identical content.

They diverged once for ~3.5 weeks: a Tag & Domain Policy fix landed only in the
maintainer copy, which no code path reads, so every wiki initialized in that
window still received the pre-fix schema and ingest kept emitting `tags: []`.
Nothing caught it because nothing compared the two. This does.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CAPTURE_COPY = REPO / "skills" / "my-llm-wiki" / "assets" / "schema.md"
MAINTAINER_COPY = REPO / "skills" / "my-llm-wiki-maintainer" / "assets" / "templates" / "schema.md"
VERSION_RE = re.compile(r"<!--\s*llm-wiki-schema-version:\s*(\d+)\s*-->")


class SchemaTemplateSyncTests(unittest.TestCase):
    def test_both_copies_exist(self) -> None:
        for path in (CAPTURE_COPY, MAINTAINER_COPY):
            self.assertTrue(path.is_file(), f"missing schema template: {path}")

    def test_copies_are_byte_identical(self) -> None:
        capture = CAPTURE_COPY.read_text(encoding="utf-8")
        maintainer = MAINTAINER_COPY.read_text(encoding="utf-8")
        self.assertEqual(
            capture, maintainer,
            "schema templates have diverged — edit both or neither.\n"
            f"  init copies:      {CAPTURE_COPY.relative_to(REPO)}\n"
            f"  maintainer holds: {MAINTAINER_COPY.relative_to(REPO)}\n"
            "Only the first one reaches a newly initialized wiki.")

    def test_carries_a_version_marker(self) -> None:
        # wiki_ops.py `schema-upgrade` / `health` read this marker to tell an
        # existing wiki how far behind the bundled template it is. Without it
        # every wiki reads as v1 forever and upgrades never surface.
        match = VERSION_RE.search(CAPTURE_COPY.read_text(encoding="utf-8"))
        self.assertIsNotNone(match, "schema template lost its version marker")
        self.assertGreaterEqual(int(match.group(1)), 2)

    def test_bumping_content_without_bumping_version_is_visible(self) -> None:
        # Not a content assertion — a reminder encoded as a test name: the
        # marker must rise whenever the template's rules change, or existing
        # wikis silently stay behind. Asserts the marker is parseable as int.
        version = VERSION_RE.search(MAINTAINER_COPY.read_text(encoding="utf-8"))
        self.assertIsNotNone(version)
        self.assertTrue(version.group(1).isdigit())


if __name__ == "__main__":
    unittest.main()
