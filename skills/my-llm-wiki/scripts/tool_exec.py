#!/usr/bin/env python3
"""Execute one external capture tool through the platform runtime policy."""

from __future__ import annotations

import argparse
import subprocess
import sys

from tool_runtime import ToolRuntimeError, resolve_command_argv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        command = [*resolve_command_argv(args.tool), *args.args]
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
