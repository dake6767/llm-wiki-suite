#!/usr/bin/env bash
# Protocol 5 acquisition wrapper. Installation choices belong to the agent:
#   bash bootstrap.sh inspect --json
#   bash bootstrap.sh plan --inspection inspection.json --selection selection.json --out plan.json
#   bash bootstrap.sh apply --plan plan.json --json-events
set -euo pipefail

PROTOCOL=5
CANONICAL_REPO_URL="https://github.com/dake6767/llm-wiki-suite.git"
CHINA_REPO_URL="https://gitee.com/dake6767/llm-wiki-suite.git"
WINDOWS_SETUP_URL="https://github.com/dake6767/llm-wiki-suite/releases/latest/download/My-LLM-Wiki-Setup.exe"
DEFAULT_REPO_HOME="${LLM_WIKI_REPO_HOME:-$HOME/.my-llm-wiki/suite}"

die() { echo "bootstrap: $*" >&2; exit 2; }
fail() { echo "bootstrap: $*" >&2; exit 1; }
need_value() { [ $# -ge 2 ] && [ -n "$2" ] || die "$1 requires a value"; }

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    echo "bootstrap: Windows uses the Protocol 5 headless native core:" >&2
    echo "bootstrap: $WINDOWS_SETUP_URL" >&2
    echo "bootstrap: invoke My-LLM-Wiki-Setup.exe inspect/plan/apply from the agent." >&2
    exit 2
    ;;
esac

REPO_ARG=""
REPO_URL="${LLM_WIKI_REPO_URL:-}"
UPDATE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) need_value "$@"; REPO_ARG="$2"; shift 2 ;;
    --repo-url) need_value "$@"; REPO_URL="$2"; shift 2 ;;
    --update) UPDATE=1; shift ;;
    -h|--help)
      sed -n '2,/^set -euo pipefail$/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'
      exit 0
      ;;
    *) break ;;
  esac
done

[ $# -gt 0 ] || die "choose one command: inspect, plan, apply, status, repair, uninstall"
case "$1" in inspect|plan|apply|status|repair|uninstall) ;; *) die "unsupported Protocol 5 command: $1" ;; esac

PY_BIN="$(command -v python3 || command -v python || true)"
[ -n "$PY_BIN" ] || die "Python 3.10+ is required on macOS/Linux"
"$PY_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || \
  die "Python 3.10+ is required"
export PYTHONUTF8=1

is_repo() {
  [ -f "$1/registry/bootstrap.json" ] &&
    [ -f "$1/registry/agent-install-v5.schema.json" ] &&
    [ -f "$1/scripts/agent_install.py" ]
}

absolute_dir() { (cd "$1" 2>/dev/null && pwd -P); }
REPO_ROOT=""
if [ -n "$REPO_ARG" ]; then
  REPO_ARG="${REPO_ARG/#\~/$HOME}"
  is_repo "$REPO_ARG" || die "not a Protocol 5 suite checkout: $REPO_ARG"
  REPO_ROOT="$(absolute_dir "$REPO_ARG")"
else
  SCRIPT_HOME="$(absolute_dir "$(dirname "${BASH_SOURCE[0]}")")"
  if is_repo "$SCRIPT_HOME"; then
    REPO_ROOT="$SCRIPT_HOME"
  elif is_repo "$DEFAULT_REPO_HOME"; then
    REPO_ROOT="$(absolute_dir "$DEFAULT_REPO_HOME")"
  fi
fi

git_bounded() {
  local timeout_seconds="$1"
  shift
  GIT_TERMINAL_PROMPT=0 "$PY_BIN" -c \
    'import subprocess,sys
try:
    result=subprocess.run(sys.argv[2:], timeout=float(sys.argv[1]), stdin=subprocess.DEVNULL)
except subprocess.TimeoutExpired:
    raise SystemExit(124)
raise SystemExit(result.returncode)' \
    "$timeout_seconds" git -c credential.interactive=never \
    -c http.lowSpeedLimit=1024 -c http.lowSpeedTime=20 "$@"
}

if [ -z "$REPO_ROOT" ]; then
  command -v git >/dev/null 2>&1 || die "git is required to acquire the Protocol 5 core"
  [ ! -e "$DEFAULT_REPO_HOME" ] || die "$DEFAULT_REPO_HOME exists but is not a valid checkout"
  mkdir -p "$(dirname "$DEFAULT_REPO_HOME")"
  CLONE_TMP="${DEFAULT_REPO_HOME}.clone.$$"
  cleanup() { [ ! -e "$CLONE_TMP" ] || rm -rf -- "$CLONE_TMP"; }
  trap cleanup EXIT
  if [ -n "$REPO_URL" ]; then
    SOURCES=("$REPO_URL")
  else
    SOURCES=("$CANONICAL_REPO_URL" "$CHINA_REPO_URL")
  fi
  cloned=0
  for source in "${SOURCES[@]}"; do
    if git_bounded 90 clone --depth 1 --single-branch "$source" "$CLONE_TMP"; then
      is_repo "$CLONE_TMP" || die "downloaded suite has an invalid Protocol 5 layout"
      mv "$CLONE_TMP" "$DEFAULT_REPO_HOME"
      cloned=1
      break
    fi
    cleanup
  done
  [ "$cloned" -eq 1 ] || fail "all configured suite sources failed"
  trap - EXIT
  REPO_ROOT="$(absolute_dir "$DEFAULT_REPO_HOME")"
fi

if [ "$UPDATE" -eq 1 ]; then
  command -v git >/dev/null 2>&1 || die "git is required for --update"
  [ -z "$(git -C "$REPO_ROOT" status --porcelain)" ] || die "refusing to update a dirty checkout"
  [ "$(git -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD || true)" = main ] || \
    die "--update requires branch main"
  git_bounded 120 -C "$REPO_ROOT" pull --ff-only || fail "update failed"
fi

ACTUAL_PROTOCOL="$($PY_BIN -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "$REPO_ROOT/registry/bootstrap.json")"
ACTUAL_PROTOCOL="${ACTUAL_PROTOCOL//$'\r'/}"
[ "$ACTUAL_PROTOCOL" = "$PROTOCOL" ] || die "checkout protocol is $ACTUAL_PROTOCOL, expected $PROTOCOL"

exec "$PY_BIN" "$REPO_ROOT/scripts/agent_install.py" "$@"
