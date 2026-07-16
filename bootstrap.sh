#!/usr/bin/env bash
#
# Standalone bootstrap for llm-wiki-suite skills.
#
# Download this one file, inspect it, then run it from anywhere. It reuses an
# existing checkout when possible; otherwise it clones the suite into the
# stable canonical home. The repo-local installer remains the single owner of
# dependency resolution and link/copy behavior.
#
# Usage:
#   bash bootstrap.sh [options] [skill-slug ...]
#
# Options:
#   --repo DIR       Reuse this checkout (highest priority).
#   --repo-url URL   Clone from URL when no checkout is found.
#   --target DIR     User-selected agent skills directory (repeatable, required).
#   --copy           Copy skills instead of linking to the checkout.
#   --force          Back up and replace conflicting target paths.
#   --update         Run `git pull --ff-only` on a clean existing checkout.
#   --dry-run        Show the install plan without changing skill targets.
#   --no-doctor      Skip the post-install suite/toolchain check.
#   -h, --help       Show this help.
#
# Examples:
#   bash bootstrap.sh --target ~/.workbuddy/skills
#   bash bootstrap.sh --target ~/.workbuddy/skills my-llm-wiki-x
#   bash bootstrap.sh --target ~/.codex/skills my-llm-wiki-video
#   bash bootstrap.sh --repo-url https://gitee.com/dake6767/llm-wiki-suite.git --target ~/.workbuddy/skills
#   bash bootstrap.sh --repo ~/projects/llm-wiki-suite --target ~/.workbuddy/skills --update
set -euo pipefail

# These bootstrap constants intentionally mirror registry/bootstrap.json.
# GitHub remains canonical; Gitee is the mainland-China Pull mirror. Explicit
# environment/CLI overrides keep private forks and non-standard homes possible
# before a repository exists to read configuration from.
CANONICAL_REPO_URL="https://github.com/dake6767/llm-wiki-suite.git"
CANONICAL_REPO_PROBE_URL="https://github.com/dake6767/llm-wiki-suite"
CHINA_MIRROR_REPO_URL="https://gitee.com/dake6767/llm-wiki-suite.git"
REPO_URL_EXPLICIT=0
if [ -n "${LLM_WIKI_REPO_URL:-}" ]; then
  REPO_URL_EXPLICIT=1
fi
DEFAULT_REPO_URL="${LLM_WIKI_REPO_URL:-$CANONICAL_REPO_URL}"
DEFAULT_REPO_HOME="${LLM_WIKI_REPO_HOME:-$HOME/.my-llm-wiki/suite}"
DEFAULT_REPO_HOME="${DEFAULT_REPO_HOME/#\~/$HOME}"

REPO_ARG=""
REPO_URL="$DEFAULT_REPO_URL"
UPDATE=0
DRY_RUN=0
RUN_DOCTOR=1
COPY=0
FORCE=0
TARGETS=()
SLUGS=()

die() {
  echo "bootstrap: $*" >&2
  exit 1
}

need_value() {
  [ $# -ge 2 ] && [ -n "$2" ] || die "$1 requires a value"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)
      need_value "$@"; REPO_ARG="$2"; shift
      ;;
    --repo-url)
      need_value "$@"; REPO_URL="$2"; REPO_URL_EXPLICIT=1; shift
      ;;
    --target)
      need_value "$@"; TARGETS+=("${2/#\~/$HOME}"); shift
      ;;
    --copy) COPY=1 ;;
    --force) FORCE=1 ;;
    --update) UPDATE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --no-doctor) RUN_DOCTOR=0 ;;
    -h|--help)
      sed -n '2,/^set -euo pipefail$/p' "${BASH_SOURCE[0]}" |
        sed '$d; s/^# \{0,1\}//'
      exit 0
      ;;
    --)
      shift
      while [ $# -gt 0 ]; do SLUGS+=("$1"); shift; done
      break
      ;;
    -*) die "unknown option: $1" ;;
    *) SLUGS+=("$1") ;;
  esac
  shift
done

[ ${#TARGETS[@]} -gt 0 ] || die "no agent target selected; ask the user which agent(s) to install into, then re-run with --target <agent-skills-dir> (for example: --target ~/.workbuddy/skills)"

PY_BIN="$(command -v python3 || command -v python || true)"
[ -n "$PY_BIN" ] || die "python3/python is required"

build_clone_urls() {
  CLONE_URLS=()
  if [ "$REPO_URL_EXPLICIT" -eq 1 ]; then
    CLONE_URLS+=("$REPO_URL")
    return
  fi

  # A short read-only probe avoids waiting on a blocked GitHub clone before
  # trying the domestic mirror. curl is optional; without it the clone itself
  # still falls back from GitHub to Gitee.
  if command -v curl >/dev/null 2>&1 &&
      ! curl -fsSI --connect-timeout 3 --max-time 5 \
        "$CANONICAL_REPO_PROBE_URL" >/dev/null 2>&1; then
    CLONE_URLS+=("$CHINA_MIRROR_REPO_URL" "$CANONICAL_REPO_URL")
  else
    CLONE_URLS+=("$CANONICAL_REPO_URL" "$CHINA_MIRROR_REPO_URL")
  fi
}

is_repo() {
  [ -n "${1:-}" ] &&
    [ -f "$1/registry/bootstrap.json" ] &&
    [ -f "$1/registry/skills.json" ] &&
    [ -f "$1/scripts/install.sh" ] &&
    [ -f "$1/scripts/doctor.py" ]
}

absolute_dir() {
  (cd "$1" 2>/dev/null && pwd -P)
}

find_linked_checkout() {
  local target slug link raw candidate
  for target in \
    "$HOME/.codex/skills" \
    "$HOME/.claude/skills" \
    "$HOME/.hermes/skills" \
    "$HOME/.agents/skills" \
    "$HOME/.workbuddy/skills"; do
    for slug in my-llm-wiki my-llm-wiki-maintainer cn-mirrors \
      my-llm-wiki-x my-llm-wiki-video; do
      link="$target/$slug"
      [ -L "$link" ] || continue
      raw="$(readlink "$link" 2>/dev/null || true)"
      [ -n "$raw" ] || continue
      case "$raw" in
        /*|[A-Za-z]:/*) ;;
        *) raw="$(dirname "$link")/$raw" ;;
      esac
      candidate="$(absolute_dir "$(dirname "$(dirname "$raw")")" || true)"
      if is_repo "$candidate"; then
        printf '%s\n' "$candidate"
        return 0
      fi
    done
  done
  return 1
}

REPO_ROOT=""
REPO_SOURCE=""

if [ -n "$REPO_ARG" ]; then
  REPO_ARG="${REPO_ARG/#\~/$HOME}"
  is_repo "$REPO_ARG" || die "not an llm-wiki-suite checkout: $REPO_ARG"
  REPO_ROOT="$(absolute_dir "$REPO_ARG")"
  REPO_SOURCE="--repo"
else
  SCRIPT_HOME="$(absolute_dir "$(dirname "${BASH_SOURCE[0]}")")"
  if is_repo "$SCRIPT_HOME"; then
    REPO_ROOT="$SCRIPT_HOME"
    REPO_SOURCE="script directory"
  elif is_repo "$PWD"; then
    REPO_ROOT="$(absolute_dir "$PWD")"
    REPO_SOURCE="current directory"
  else
    REPO_ROOT="$(find_linked_checkout || true)"
    if [ -n "$REPO_ROOT" ]; then
      REPO_SOURCE="installed skill link"
    else
      for candidate in \
        "$HOME/projects/llm-wiki-suite" \
        "$HOME/Projects/llm-wiki-suite" \
        "$HOME/src/llm-wiki-suite" \
        "$HOME/dev/llm-wiki-suite"; do
        if is_repo "$candidate"; then
          REPO_ROOT="$(absolute_dir "$candidate")"
          REPO_SOURCE="development checkout"
          break
        fi
      done
    fi
  fi
fi

if [ -z "$REPO_ROOT" ] && is_repo "$DEFAULT_REPO_HOME"; then
  REPO_ROOT="$(absolute_dir "$DEFAULT_REPO_HOME")"
  REPO_SOURCE="canonical home"
fi

if [ -z "$REPO_ROOT" ]; then
  build_clone_urls
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] would try fresh-clone source(s), in order:"
    printf '          %s\n' "${CLONE_URLS[@]}"
    echo "[dry-run] destination: $DEFAULT_REPO_HOME"
    if [ ${#TARGETS[@]} -eq 0 ]; then
      echo "[dry-run] target dirs would come from registry/bootstrap.json"
    else
      printf '[dry-run] target: %s\n' "${TARGETS[@]}"
    fi
    if [ ${#SLUGS[@]} -gt 0 ]; then
      echo "[dry-run] requested skills: ${SLUGS[*]}"
    else
      echo "[dry-run] requested skills: all active skills"
    fi
    exit 0
  fi

  command -v git >/dev/null 2>&1 || die "git is required for a fresh install"
  { [ ! -e "$DEFAULT_REPO_HOME" ] && [ ! -L "$DEFAULT_REPO_HOME" ]; } || die \
    "$DEFAULT_REPO_HOME exists but is not a valid checkout; move it or use --repo"
  echo "No existing checkout found."
  mkdir -p "$(dirname "$DEFAULT_REPO_HOME")"
  CLONE_TMP="${DEFAULT_REPO_HOME}.clone.$$"
  { [ ! -e "$CLONE_TMP" ] && [ ! -L "$CLONE_TMP" ]; } || die \
    "$CLONE_TMP already exists; remove it and retry"
  cleanup_clone_tmp() {
    if [ -n "${CLONE_TMP:-}" ] && { [ -e "$CLONE_TMP" ] || [ -L "$CLONE_TMP" ]; }; then
      rm -rf -- "$CLONE_TMP"
    fi
  }
  trap cleanup_clone_tmp EXIT
  CLONED_FROM=""
  for candidate_url in "${CLONE_URLS[@]}"; do
    echo "Cloning $candidate_url"
    echo "     to $DEFAULT_REPO_HOME"
    if git clone "$candidate_url" "$CLONE_TMP"; then
      mv "$CLONE_TMP" "$DEFAULT_REPO_HOME"
      CLONE_TMP=""
      CLONED_FROM="$candidate_url"
      break
    fi
    echo "Clone failed; trying the next configured source." >&2
    cleanup_clone_tmp
  done
  trap - EXIT
  [ -n "$CLONED_FROM" ] || die "all configured clone sources failed"
  is_repo "$DEFAULT_REPO_HOME" || die "clone completed but the suite layout is invalid"
  REPO_ROOT="$(absolute_dir "$DEFAULT_REPO_HOME")"
  REPO_SOURCE="fresh clone ($CLONED_FROM)"
else
  echo "Reusing checkout ($REPO_SOURCE): $REPO_ROOT"
fi

if [ "$UPDATE" -eq 1 ]; then
  command -v git >/dev/null 2>&1 || die "git is required for --update"
  git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 ||
    die "--update requires a git checkout: $REPO_ROOT"
  [ -z "$(git -C "$REPO_ROOT" status --porcelain)" ] ||
    die "refusing to update a dirty checkout: $REPO_ROOT"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] would update checkout with git pull --ff-only"
  else
    echo "Updating checkout with git pull --ff-only..."
    git -C "$REPO_ROOT" pull --ff-only
  fi
fi

INSTALL_ARGS=()
[ "$COPY" -eq 1 ] && INSTALL_ARGS+=(--copy)
[ "$FORCE" -eq 1 ] && INSTALL_ARGS+=(--force)
[ "$DRY_RUN" -eq 1 ] && INSTALL_ARGS+=(--dry-run)
for target in "${TARGETS[@]}"; do
  INSTALL_ARGS+=(--target "$target")
done

echo
echo "Syncing skills into ${#TARGETS[@]} agent target(s)..."
if [ ${#SLUGS[@]} -gt 0 ]; then
  "$REPO_ROOT/scripts/install.sh" "${INSTALL_ARGS[@]}" "${SLUGS[@]}"
else
  "$REPO_ROOT/scripts/install.sh" "${INSTALL_ARGS[@]}"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "bootstrap: dry-run complete; doctor was not run"
elif [ "$RUN_DOCTOR" -eq 1 ]; then
  echo
  echo "Running suite and selected-toolchain check..."
  DOCTOR_ARGS=()
  for target in "${TARGETS[@]}"; do
    DOCTOR_ARGS+=(--target "$target")
  done
  if [ ${#SLUGS[@]} -gt 0 ]; then
    "$PY_BIN" "$REPO_ROOT/scripts/doctor.py" \
      "${DOCTOR_ARGS[@]}" --skills "${SLUGS[@]}"
  else
    "$PY_BIN" "$REPO_ROOT/scripts/doctor.py" "${DOCTOR_ARGS[@]}"
  fi
else
  echo "bootstrap: doctor skipped (--no-doctor)"
fi

echo
echo "Skills are synchronized from: $REPO_ROOT"
if [ "$DRY_RUN" -eq 0 ] && [ "$RUN_DOCTOR" -eq 1 ]; then
  echo "External capture tools were detected only; none were installed."
else
  echo "No external capture tools were installed."
fi
