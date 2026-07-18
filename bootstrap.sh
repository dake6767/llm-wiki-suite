#!/usr/bin/env bash
# Standalone deterministic bootstrap (protocol 4).
#
# Usage:
#   bash bootstrap.sh (--host ID | --custom-target DIR) [options] [SKILL ...]
#
# Options:
#   --host ID          Install to a named host; repeatable.
#   --custom-target D  Install to an explicit non-registry skills directory.
#   --repo DIR         Use exactly this checkout.
#   --repo-url URL     Fresh-clone source (otherwise GitHub/Gitee are probed).
#   --copy             Install verified immutable copies instead of links.
#   --replace          Back up and replace every conflicting destination.
#   --update           Fast-forward a clean selected checkout before install.
#   --dry-run          Print a plan and make no changes.
#   --no-doctor        Skip only doctor; required Wiki initialization still runs.
#
# Named hosts: codex, claude, hermes, agents, workbuddy.
set -euo pipefail

PROTOCOL=4
CANONICAL_REPO_URL="https://github.com/dake6767/llm-wiki-suite.git"
CANONICAL_REPO_PROBE_URL="https://github.com/dake6767/llm-wiki-suite"
CHINA_MIRROR_REPO_URL="https://gitee.com/dake6767/llm-wiki-suite.git"
WINDOWS_SETUP_URL="https://github.com/dake6767/llm-wiki-suite/releases/latest/download/My-LLM-Wiki-Setup.exe"
DEFAULT_REPO_HOME="${LLM_WIKI_REPO_HOME:-$HOME/.my-llm-wiki/suite}"

IS_WINDOWS=0
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;; esac

native_path() {
  if [ "$IS_WINDOWS" -eq 1 ] && command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$1"
  else
    printf '%s\n' "$1"
  fi
}

die() { echo "bootstrap: $*" >&2; exit 2; }
fail() { echo "bootstrap: $*" >&2; exit 1; }
need_value() { [ $# -ge 2 ] && [ -n "$2" ] || die "$1 requires a value"; }

if [ "$IS_WINDOWS" -eq 1 ]; then
  echo "bootstrap: Windows no longer supports the Git-Bash/Python/Git install flow." >&2
  echo "bootstrap: download and run the native My-LLM-Wiki-Setup.exe:" >&2
  echo "bootstrap: $WINDOWS_SETUP_URL" >&2
  echo "bootstrap: the Setup UI owns host selection, skills, private runtimes, tools, repair, and uninstall." >&2
  exit 2
fi

REPO_ARG=""
REPO_URL="${LLM_WIKI_REPO_URL:-$CANONICAL_REPO_URL}"
REPO_URL_EXPLICIT=0
[ -n "${LLM_WIKI_REPO_URL:-}" ] && REPO_URL_EXPLICIT=1
UPDATE=0
DRY_RUN=0
RUN_DOCTOR=1
COPY=0
REPLACE=0
HOSTS=()
CUSTOM_TARGETS=()
SLUGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) need_value "$@"; REPO_ARG="$2"; shift ;;
    --repo-url) need_value "$@"; REPO_URL="$2"; REPO_URL_EXPLICIT=1; shift ;;
    --host) need_value "$@"; HOSTS+=("$2"); shift ;;
    --custom-target) need_value "$@"; CUSTOM_TARGETS+=("$(native_path "${2/#\~/$HOME}")"); shift ;;
    --copy) COPY=1 ;;
    --replace) REPLACE=1 ;;
    --update) UPDATE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --no-doctor) RUN_DOCTOR=0 ;;
    -h|--help)
      sed -n '2,/^set -euo pipefail$/p' "${BASH_SOURCE[0]}" |
        sed '$d; s/^# \{0,1\}//'
      exit 0
      ;;
    --) shift; while [ $# -gt 0 ]; do SLUGS+=("$1"); shift; done; break ;;
    -*) die "unknown option: $1" ;;
    *) SLUGS+=("$1") ;;
  esac
  shift
done

[ ${#HOSTS[@]} -gt 0 ] || [ ${#CUSTOM_TARGETS[@]} -gt 0 ] || \
  die "select at least one --host or --custom-target before installation"
if [ ${#HOSTS[@]} -gt 0 ]; then
  for host in "${HOSTS[@]}"; do
    case "$host" in
      codex|claude|hermes|agents|workbuddy) ;;
      *) die "unknown host: $host (choose codex, claude, hermes, agents, or workbuddy)" ;;
    esac
  done
fi

PY_BIN="$(command -v python3 || command -v python || true)"
[ -n "$PY_BIN" ] || die "Python 3.10+ is required"
"$PY_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || \
  die "Python 3.10+ is required"
# Child Python commands must render the same machine-readable and human-readable
# output on Windows terminals regardless of the active legacy code page.
export PYTHONUTF8=1

DEFAULT_REPO_HOME="$(native_path "${DEFAULT_REPO_HOME/#\~/$HOME}")"

is_repo() {
  [ -n "${1:-}" ] &&
    [ -f "$1/registry/bootstrap.json" ] &&
    [ -f "$1/registry/skills.json" ] &&
    [ -f "$1/scripts/install.py" ] &&
    [ -f "$1/scripts/initialize_wiki.py" ] &&
    [ -f "$1/scripts/doctor.py" ]
}

absolute_dir() { (cd "$1" 2>/dev/null && pwd -P); }

git_bounded() {
  timeout="$1"
  shift
  GIT_TERMINAL_PROMPT=0 "$PY_BIN" -c \
    'import subprocess,sys
try:
    result = subprocess.run(sys.argv[2:], timeout=float(sys.argv[1]))
except subprocess.TimeoutExpired:
    print("bootstrap: command timed out: " + " ".join(sys.argv[2:]), file=sys.stderr)
    raise SystemExit(124)
raise SystemExit(result.returncode)' \
    "$timeout" git \
    -c credential.interactive=never \
    -c http.lowSpeedLimit=1024 \
    -c http.lowSpeedTime=20 \
    "$@"
}

build_clone_urls() {
  CLONE_URLS=()
  if [ "$REPO_URL_EXPLICIT" -eq 1 ]; then
    CLONE_URLS+=("$REPO_URL")
  elif [ "$DRY_RUN" -eq 1 ]; then
    CLONE_URLS+=("$CANONICAL_REPO_URL" "$CHINA_MIRROR_REPO_URL")
  elif command -v curl >/dev/null 2>&1 &&
      ! curl -fsSI --connect-timeout 3 --max-time 5 \
        "$CANONICAL_REPO_PROBE_URL" >/dev/null 2>&1; then
    CLONE_URLS+=("$CHINA_MIRROR_REPO_URL" "$CANONICAL_REPO_URL")
  else
    CLONE_URLS+=("$CANONICAL_REPO_URL" "$CHINA_MIRROR_REPO_URL")
  fi
}

REPO_ROOT=""
REPO_SOURCE=""
if [ -n "$REPO_ARG" ]; then
  REPO_ARG="$(native_path "${REPO_ARG/#\~/$HOME}")"
  is_repo "$REPO_ARG" || die "not an llm-wiki-suite checkout: $REPO_ARG"
  REPO_ROOT="$(absolute_dir "$REPO_ARG")"
  REPO_SOURCE="explicit"
else
  SCRIPT_HOME="$(absolute_dir "$(dirname "${BASH_SOURCE[0]}")")"
  if is_repo "$SCRIPT_HOME"; then
    REPO_ROOT="$SCRIPT_HOME"
    REPO_SOURCE="script-directory"
  elif is_repo "$DEFAULT_REPO_HOME"; then
    REPO_ROOT="$(absolute_dir "$DEFAULT_REPO_HOME")"
    REPO_SOURCE="managed-home"
  fi
fi

if [ -z "$REPO_ROOT" ]; then
  build_clone_urls
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "status: planned"
    echo "repo: $DEFAULT_REPO_HOME"
    printf 'clone-source: %s\n' "${CLONE_URLS[@]}"
    if [ ${#HOSTS[@]} -gt 0 ]; then printf 'host: %s\n' "${HOSTS[@]}"; fi
    if [ ${#CUSTOM_TARGETS[@]} -gt 0 ]; then
      printf 'custom-target: %s\n' "${CUSTOM_TARGETS[@]}"
    fi
    exit 0
  fi
  command -v git >/dev/null 2>&1 || die "git is required for a fresh install"
  [ ! -e "$DEFAULT_REPO_HOME" ] && [ ! -L "$DEFAULT_REPO_HOME" ] || \
    die "$DEFAULT_REPO_HOME exists but is not a valid checkout"
  mkdir -p "$(dirname "$DEFAULT_REPO_HOME")"
  CLONE_TMP="${DEFAULT_REPO_HOME}.clone.$$"
  cleanup() { [ ! -e "$CLONE_TMP" ] || rm -rf -- "$CLONE_TMP"; }
  trap cleanup EXIT
  CLONED_FROM=""
  for candidate in "${CLONE_URLS[@]}"; do
    echo "cloning: $candidate"
    if git_bounded 60 clone --depth 1 --single-branch "$candidate" "$CLONE_TMP"; then
      is_repo "$CLONE_TMP" || die "cloned repository has an invalid suite layout"
      mv "$CLONE_TMP" "$DEFAULT_REPO_HOME"
      CLONED_FROM="$candidate"
      break
    fi
    cleanup
  done
  [ -n "$CLONED_FROM" ] || fail "all configured clone sources failed"
  trap - EXIT
  REPO_ROOT="$(absolute_dir "$DEFAULT_REPO_HOME")"
  REPO_SOURCE="fresh-clone"
fi

REPO_ROOT="$(native_path "$REPO_ROOT")"
echo "checkout: $REPO_ROOT ($REPO_SOURCE)"

if [ "$UPDATE" -eq 1 ]; then
  command -v git >/dev/null 2>&1 || die "git is required for --update"
  git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || \
    die "--update requires a git checkout: $REPO_ROOT"
  [ -z "$(git -C "$REPO_ROOT" status --porcelain)" ] || die "refusing to update a dirty checkout"
  BRANCH="$(git -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  [ "$BRANCH" = "main" ] || die "--update requires branch main; current: ${BRANCH:-detached}"
  UPSTREAM="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref '@{upstream}' 2>/dev/null || true)"
  [ "$UPSTREAM" = "origin/main" ] || \
    die "--update requires upstream origin/main; current: ${UPSTREAM:-none}"
  ORIGIN_URL="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
  case "$ORIGIN_URL" in
    https://github.com/dake6767/llm-wiki-suite|https://github.com/dake6767/llm-wiki-suite.git|\
    git@github.com:dake6767/llm-wiki-suite.git|ssh://git@github.com/dake6767/llm-wiki-suite.git|\
    https://gitee.com/dake6767/llm-wiki-suite|https://gitee.com/dake6767/llm-wiki-suite.git) ;;
    *) die "--update requires the canonical GitHub origin or declared Gitee mirror" ;;
  esac
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "update: planned"
  else
    git_bounded 120 -C "$REPO_ROOT" pull --ff-only || fail "update failed"
    [ "$(git -C "$REPO_ROOT" rev-parse HEAD)" = \
      "$(git -C "$REPO_ROOT" rev-parse origin/main)" ] || \
      fail "update did not converge to origin/main (local commits remain)"
  fi
fi

ACTUAL_PROTOCOL="$($PY_BIN -c 'import json,sys; sys.stdout.write(str(json.load(open(sys.argv[1], encoding="utf-8"))["version"]))' "$REPO_ROOT/registry/bootstrap.json")"
[ "$ACTUAL_PROTOCOL" = "$PROTOCOL" ] || \
  die "bootstrap protocol $PROTOCOL does not match checkout protocol $ACTUAL_PROTOCOL; update the checkout and retry"

INSTALL_ARGS=()
if [ ${#HOSTS[@]} -gt 0 ]; then
  for host in "${HOSTS[@]}"; do INSTALL_ARGS+=(--host "$host"); done
fi
if [ ${#CUSTOM_TARGETS[@]} -gt 0 ]; then
  for target in "${CUSTOM_TARGETS[@]}"; do INSTALL_ARGS+=(--custom-target "$target"); done
fi
[ "$COPY" -eq 1 ] && INSTALL_ARGS+=(--copy)
[ "$REPLACE" -eq 1 ] && INSTALL_ARGS+=(--replace)
[ "$DRY_RUN" -eq 1 ] && INSTALL_ARGS+=(--dry-run)

RUN_INSTALL=("$PY_BIN" "$REPO_ROOT/scripts/install.py" "${INSTALL_ARGS[@]}")
if [ ${#SLUGS[@]} -gt 0 ]; then RUN_INSTALL+=("${SLUGS[@]}"); fi
"${RUN_INSTALL[@]}"

if [ "$DRY_RUN" -eq 1 ]; then
  "$PY_BIN" "$REPO_ROOT/scripts/initialize_wiki.py" --dry-run
else
  "$PY_BIN" "$REPO_ROOT/scripts/initialize_wiki.py"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  exit 0
fi

if [ "$RUN_DOCTOR" -eq 1 ]; then
  DOCTOR_ARGS=()
  if [ ${#HOSTS[@]} -gt 0 ]; then
    for host in "${HOSTS[@]}"; do DOCTOR_ARGS+=(--host "$host"); done
  fi
  if [ ${#CUSTOM_TARGETS[@]} -gt 0 ]; then
    for target in "${CUSTOM_TARGETS[@]}"; do DOCTOR_ARGS+=(--custom-target "$target"); done
  fi
  if [ ${#SLUGS[@]} -gt 0 ]; then DOCTOR_ARGS+=(--skills "${SLUGS[@]}"); fi
  set +e
  "$PY_BIN" "$REPO_ROOT/scripts/doctor.py" "${DOCTOR_ARGS[@]}"
  DOCTOR_STATUS=$?
  set -e
  case "$DOCTOR_STATUS" in
    0) echo "status: installed" ;;
    3) echo "status: action-required"; exit 3 ;;
    *) echo "status: failed" >&2; exit "$DOCTOR_STATUS" ;;
  esac
else
  echo "status: installed-unverified"
fi
