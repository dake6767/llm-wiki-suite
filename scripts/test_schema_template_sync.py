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

import hashlib
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CAPTURE_COPY = REPO / "skills" / "my-llm-wiki" / "assets" / "schema.md"
MAINTAINER_COPY = REPO / "skills" / "my-llm-wiki-maintainer" / "assets" / "templates" / "schema.md"
VERSION_RE = re.compile(r"<!--\s*llm-wiki-schema-version:\s*(\d+)\s*-->")

# sha256 of the schema template at each published version. Adding an entry is
# the deliberate act that says "this is a new schema version"; changing content
# under an existing version is what the digest test refuses.
KNOWN_SCHEMA_DIGESTS = {
    2: "48e6a8dc9afee9c89d87ae18458dd3eda46566bf48ae9b77d5503da3911fe188",
}


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

    def test_content_change_requires_a_version_bump(self) -> None:
        # The gate that actually bites. Editing the template's rules without
        # raising the marker leaves every existing wiki silently behind —
        # `schema-upgrade` compares versions, not content, so a same-version
        # edit is invisible to it and reaches nobody. Binding the content
        # digest to the version makes that impossible to do by accident: change
        # the template and this fails until you bump the marker AND record the
        # new digest below.
        #
        # An earlier version of this test only asserted the marker was numeric,
        # which its own name claimed was a bump gate. It was not — the template
        # could be rewritten wholesale and it still passed.
        text = CAPTURE_COPY.read_text(encoding="utf-8")
        version = int(VERSION_RE.search(text).group(1))
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        expected = KNOWN_SCHEMA_DIGESTS.get(version)
        self.assertIsNotNone(
            expected,
            f"schema template is v{version} with no recorded digest. If you bumped "
            f"the version, add it:\n    {version}: \"{digest}\",")
        self.assertEqual(
            digest, expected,
            f"schema template content changed but the version marker is still "
            f"v{version}. Existing wikis compare versions, not content, so this "
            f"edit would reach none of them. Bump the marker in BOTH templates and "
            f"record the new digest:\n    {version + 1}: \"{digest}\",")


if __name__ == "__main__":
    unittest.main()
