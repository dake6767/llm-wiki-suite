#!/usr/bin/env bash
#
# Install / sync maintained skills from this collection into agent skill dirs.
#
# registry/skills.json is the source of truth: only skills with lifecycle
# "active" are installed. Default mode is symlink, so every agent shares the one
# canonical copy in this repo — edit once, effective everywhere, no drift.
# Runtime artifacts (data/, reports/) are .gitignored, so agents may write them
# into the linked dir without polluting the repo.
#
# Usage:
#   scripts/install.sh [options] [slug ...]
#
# Options:
#   --copy          Copy files instead of symlinking (isolated per-agent copy).
#   --force         Replace an existing real dir/symlink at the target
#                   (the old one is moved to <dest>.bak-YYYYmmddHHMMSS).
#   --target DIR    Install target dir (repeatable). Overrides the defaults.
#   --dry-run       Print the plan; change nothing.
#   -h, --help      Show this help.
#
# Defaults (when no --target given):
#   ~/.claude/skills  ~/.hermes/skills
#
# Examples:
#   scripts/install.sh                      # symlink all active skills to defaults
#   scripts/install.sh --dry-run            # preview
#   scripts/install.sh my-llm-wiki          # just one skill
#   scripts/install.sh --target ~/.codex/skills --target ~/.agents/skills
#   scripts/install.sh --copy --force       # isolated copies, replacing existing
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
REGISTRY="$ROOT/registry/skills.json"

MODE="link"
FORCE=0
DRY=0
TARGETS=()
WANT_SLUGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --copy) MODE="copy" ;;
    --force) FORCE=1 ;;
    --dry-run) DRY=1 ;;
    --target) shift; TARGETS+=("${1/#\~/$HOME}") ;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) WANT_SLUGS+=("$1") ;;
  esac
  shift
done

if [ ${#TARGETS[@]} -eq 0 ]; then
  TARGETS=("$HOME/.claude/skills" "$HOME/.hermes/skills")
fi

[ -f "$REGISTRY" ] || { echo "registry not found: $REGISTRY" >&2; exit 1; }

# Backups go OUTSIDE any skills dir (a .bak left inside it gets loaded as a skill).
BACKUP_ROOT="$HOME/.skill-install-backups"

# Emit "slug<TAB>abs_src" for each active skill (optionally filtered to WANT_SLUGS).
ENTRIES=()
while IFS= read -r line; do ENTRIES+=("$line"); done < <(
  ROOT="$ROOT" python3 - ${WANT_SLUGS[@]+"${WANT_SLUGS[@]}"} <<'PY'
import json, os, sys
root = os.environ["ROOT"]
want = set(sys.argv[1:])
data = json.load(open(os.path.join(root, "registry", "skills.json"), encoding="utf-8"))
for s in data.get("skills", []):
    if s.get("lifecycle", "active") != "active":
        continue
    if want and s["slug"] not in want:
        continue
    src = os.path.join(root, s["collection_path"])
    print(f"{s['slug']}\t{src}")
PY
)

[ ${#ENTRIES[@]} -gt 0 ] || { echo "no matching active skills in registry." >&2; exit 1; }

ts() { date +%Y%m%d%H%M%S; }
act() { if [ "$DRY" -eq 1 ]; then echo "  [dry-run] $*"; else eval "$@"; fi; }

echo "mode=$MODE  force=$FORCE  dry-run=$DRY"
echo "skills: $(printf '%s ' "${ENTRIES[@]%%$'\t'*}")"
echo

n_ok=0; n_done=0; n_skip=0
for target in "${TARGETS[@]}"; do
  echo "▸ target: $target"
  tag="$(basename "$(dirname "$target")")"; tag="${tag#.}"   # ~/.hermes/skills -> hermes
  act "mkdir -p '$target'"
  for entry in "${ENTRIES[@]}"; do
    slug="${entry%%$'\t'*}"; src="${entry#*$'\t'}"
    dest="$target/$slug"
    if [ ! -e "$src" ]; then echo "  ✗ $slug: source missing ($src)"; n_skip=$((n_skip+1)); continue; fi

    if [ "$MODE" = "link" ]; then
      if [ -L "$dest" ] && [ "$(readlink "$dest")" = "$src" ]; then
        echo "  = $slug (up to date)"; n_ok=$((n_ok+1)); continue
      fi
      if [ -L "$dest" ]; then
        echo "  ~ $slug: repointing symlink"
        act "rm '$dest'"; act "ln -s '$src' '$dest'"; n_done=$((n_done+1)); continue
      fi
      if [ -e "$dest" ]; then
        if [ "$FORCE" -eq 1 ]; then
          echo "  ! $slug: backing up real path → $BACKUP_ROOT/$tag, linking"
          act "mkdir -p '$BACKUP_ROOT/$tag'"; act "mv '$dest' '$BACKUP_ROOT/$tag/$slug.bak-$(ts)'"; act "ln -s '$src' '$dest'"; n_done=$((n_done+1))
        else
          echo "  ⏭ $slug: real dir exists (use --force to replace) — skipped"; n_skip=$((n_skip+1))
        fi
        continue
      fi
      echo "  + $slug: linking"
      act "ln -s '$src' '$dest'"; n_done=$((n_done+1))
    else  # copy
      if [ -e "$dest" ] || [ -L "$dest" ]; then
        if [ "$FORCE" -eq 1 ]; then
          echo "  ! $slug: backing up existing → $BACKUP_ROOT/$tag, copying"
          act "mkdir -p '$BACKUP_ROOT/$tag'"; act "mv '$dest' '$BACKUP_ROOT/$tag/$slug.bak-$(ts)'"
        elif [ -L "$dest" ]; then
          echo "  ⏭ $slug: target is a symlink (use --force to replace) — skipped"; n_skip=$((n_skip+1)); continue
        fi
      fi
      echo "  + $slug: copying"
      act "rsync -a --exclude '.git' --exclude '__pycache__' --exclude '.DS_Store' --exclude 'reports/' --exclude 'data/' '$src/' '$dest/'"
      n_done=$((n_done+1))
    fi
  done
  echo
done

echo "done. installed/updated=$n_done  already-ok=$n_ok  skipped=$n_skip"
if [ "$DRY" -eq 1 ]; then echo "(dry-run: nothing changed)"; fi
exit 0
