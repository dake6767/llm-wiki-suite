#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_BIN="$(command -v python3 || command -v python || true)"
[ -n "$PY_BIN" ] || { echo "install: Python 3.10+ is required" >&2; exit 2; }
exec "$PY_BIN" "$ROOT/scripts/install.py" "$@"
