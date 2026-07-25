#!/usr/bin/env python3
"""Execute one external capture tool through the platform runtime policy."""

from __future__ import annotations

import argparse
import subprocess
import sys

from path_compat import reject_ambiguous_windows_temp_path
from tool_runtime import ToolRuntimeError, resolve_command_argv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capability")
    parser.add_argument("--provider", help="explicit Provider id for this task only")
    parser.add_argument("tool")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        for value in args.args:
            reject_ambiguous_windows_temp_path(value)
    except ValueError as exc:
        print(f"tool-exec: {exc}", file=sys.stderr)
        return 2
    try:
        command = [
            *resolve_command_argv(
                args.tool, capability=args.capability, provider=args.provider
            ),
            *args.args,
        ]
    except ToolRuntimeError as exc:
        print(f"tool-exec: {exc}", file=sys.stderr)
        return 3
    try:
        return subprocess.run(command, stdin=None, check=False).returncode
    except OSError as exc:
        print(f"tool-exec: cannot run {args.tool}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
