#!/usr/bin/env python3
"""Convert staged HTML into bounded plain text without executing its content."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path


DEFAULT_MAX_BYTES = 5 * 1024 * 1024


class TextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
        "dt", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4",
        "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
        "section", "table", "td", "th", "tr", "ul",
    }
    SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts).replace("\xa0", " ")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in joined.splitlines()]
        output: list[str] = []
        for line in lines:
            if line:
                output.append(line)
            elif output and output[-1] != "":
                output.append("")
        return "\n".join(output).strip() + "\n"


def html_to_text(html: str) -> str:
    parser = TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def excerpt(text: str, needle: str | None, max_chars: int) -> str:
    if max_chars <= 0:
        raise ValueError("--max-chars must be positive")
    if not needle:
        return text[:max_chars].rstrip() + "\n"
    position = text.casefold().find(needle.casefold())
    if position < 0:
        raise ValueError(f"needle not found: {needle}")
    start = max(0, position - max_chars // 4)
    end = min(len(text), start + max_chars)
    return text[start:end].strip() + "\n"


def read_bounded(path: Path, max_bytes: int) -> str:
    if max_bytes <= 0:
        raise ValueError("--max-bytes must be positive")
    with path.open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"input exceeds {max_bytes} bytes")
    return payload.decode("utf-8", errors="replace")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="HTML file already staged by a retrieval tool")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--needle")
    parser.add_argument("--max-chars", type=int, default=20_000)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        html = read_bounded(args.input, args.max_bytes)
        result = excerpt(html_to_text(html), args.needle, args.max_chars)
        if args.output:
            atomic_write(args.output, result)
        print(result, end="")
    except (OSError, ValueError) as exc:
        print(f"html_to_text: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
