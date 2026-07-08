#!/usr/bin/env python3
"""Print the source ids already captured in a wiki, one per line.

RAW frontmatter is the source of truth for "what have I already ingested". Use
this as a fetch-filter for batch syncs (X bookmarks especially): list the remote
items cheaply, subtract this set, and only do the expensive per-item media
download for the ids that remain. That single discipline both prevents duplicate
captures and makes any large sync resumable — if it dies, re-running skips
everything already on disk and downloads nothing twice.

Scans raw/sources/<source>/*.md frontmatter (the llm_wiki / Obsidian layout).

Usage:
  captured_ids.py --wiki <wiki-root> [--source x]
"""
import argparse, glob, os, re, sys

ap = argparse.ArgumentParser()
ap.add_argument("--wiki", required=True, help="wiki root (the dir with schema.md + raw/sources/)")
ap.add_argument("--source", default="x", help="source_type bucket under raw/sources (default: x)")
a = ap.parse_args()

ids = set()
# Match raw/sources/<source>/*.md and any nested .md under it.
roots = [
    os.path.join(a.wiki, "raw", "sources", a.source, "*.md"),
    os.path.join(a.wiki, "raw", "sources", a.source, "**", "*.md"),
]
for pattern in roots:
    for idx in glob.glob(pattern, recursive=True):
        try:
            text = open(idx, encoding="utf-8").read()
        except OSError:
            continue
        m = re.match(r"^---\n(.*?)\n---", text, re.S)
        if not m:
            continue
        for line in m.group(1).splitlines():
            mm = re.match(r"^original_id:\s*(.*)$", line.strip())
            if mm:
                v = mm.group(1).strip().strip("\"'")
                if v:
                    ids.add(v)
                break

for i in sorted(ids):
    print(i)
print(f"# {len(ids)} captured ids under raw/sources/{a.source}/", file=sys.stderr)
