#!/usr/bin/env python3
"""Fail when skill command examples reintroduce avoidable approval triggers."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SKILLS = (
    ROOT / "skills" / "my-llm-wiki",
    ROOT / "skills" / "my-llm-wiki-video",
    ROOT / "skills" / "my-llm-wiki-x",
    ROOT / "skills" / "my-llm-wiki-maintainer",
)
EXECUTABLE_FENCE_LANGS = {"python", "py", "javascript", "js", "node", "ruby", "perl"}
SHELL_FENCE_LANGS = {"", "bash", "sh", "shell", "zsh", "console"}


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    rule: str
    detail: str


RISKY_SHELL_RULES = (
    (
        "remote-pipe-interpreter",
        re.compile(
            r"\b(?:curl|wget|yt-dlp|opencli|agent-reach|tvly)\b"
            r"[^\n|]*(?:\\\s*\n[^\n|]*)*\|\s*(?:\\\s*\n\s*)?"
            r"(?:[/\w.-]*/)?(?:python[23]?|node|ruby|perl|ba?sh|zsh)\b",
            re.IGNORECASE,
        ),
        "stage remote/tool output as data or call a shipped parser",
    ),
    (
        "inline-interpreter-code",
        re.compile(r"\b(?:python[23]?|node|ruby|perl)\s+-(?:c|e)\b", re.IGNORECASE),
        "move deterministic code into a bundled script",
    ),
    (
        "remote-command-substitution",
        re.compile(
            r"(?:\$\(|`)[^)\n`]*\b(?:curl|wget|yt-dlp|opencli|agent-reach|tvly)\b",
            re.IGNORECASE,
        ),
        "stage retrieval output in a data file instead of shell substitution",
    ),
    (
        "script-heredoc",
        re.compile(
            r"(?:\b(?:python[23]?|node|ruby|perl|ba?sh|zsh)\s+<<|"
            r"\bcat\s+>[^\n]*\.(?:py|js|rb|pl|sh)\s*<<)",
            re.IGNORECASE,
        ),
        "ship the script instead of generating it at runtime",
    ),
    (
        "recursive-delete",
        re.compile(r"\brm\s+(?:-[^\s]*r[^\s]*f|--recursive\b[^\n]*--force\b)", re.IGNORECASE),
        "allocate a fresh scoped temp directory or use a bounded cleanup helper",
    ),
    (
        "tls-verification-disabled",
        re.compile(r"--no-check-certificates\b|-k\s+https?://", re.IGNORECASE),
        "keep TLS verification enabled",
    ),
    (
        "plain-http-url",
        re.compile(r"http://(?!(?:127\.0\.0\.1|localhost|\[::1\])(?::|/))\S+", re.IGNORECASE),
        "use HTTPS for external URLs",
    ),
)


def markdown_fences(text: str):
    active = False
    language = ""
    start = 0
    body: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = re.match(r"^\s*```\s*([\w+-]*)", line)
        if match and not active:
            active = True
            language = match.group(1).lower()
            start = line_number + 1
            body = []
            continue
        if active and re.match(r"^\s*```\s*$", line):
            yield language, start, "\n".join(body)
            active = False
            continue
        if active:
            body.append(line)


def scan_file(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    text = path.read_text(encoding="utf-8")
    for language, start, body in markdown_fences(text):
        if language in EXECUTABLE_FENCE_LANGS:
            findings.append(Finding(
                path, start - 1, "inline-executable-fence",
                f"{language} example should be a bundled script",
            ))
            continue
        if language not in SHELL_FENCE_LANGS:
            continue
        if any("\ufe00" <= char <= "\ufe0f" or "\U000e0100" <= char <= "\U000e01ef"
               for char in body):
            findings.append(Finding(
                path, start, "unicode-variation-selector",
                "escape or remove variation selectors from command examples",
            ))
        for rule, pattern, detail in RISKY_SHELL_RULES:
            match = pattern.search(body)
            if match:
                line = start + body[:match.start()].count("\n")
                findings.append(Finding(path, line, rule, detail))
    return findings


def scan_paths(paths: list[Path] | tuple[Path, ...]) -> list[Finding]:
    findings: list[Finding] = []
    for root in paths:
        candidates = [root] if root.is_file() else sorted(root.rglob("*.md"))
        for path in candidates:
            findings.extend(scan_file(path))
    return findings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="files/directories; defaults to wiki skills")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = args.paths or list(DEFAULT_SKILLS)
    findings = scan_paths(paths)
    for finding in findings:
        try:
            shown = finding.path.relative_to(ROOT)
        except ValueError:
            shown = finding.path
        print(f"{shown}:{finding.line}: {finding.rule}: {finding.detail}")
    if findings:
        print(f"approval safety: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print(f"approval safety: ok ({len(paths)} roots)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
