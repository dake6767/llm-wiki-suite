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
#   scripts/install.sh my-llm-wiki          # facade + bundled video/X leaf skills
#   scripts/install.sh my-llm-wiki-video    # leaf + required my-llm-wiki core
#   scripts/install.sh --target ~/.codex/skills --target ~/.agents/skills
#   scripts/install.sh --copy --force       # isolated copies, replacing existing
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
REGISTRY="$ROOT/registry/skills.json"

# Windows（Git Bash / MSYS2）兼容开关。三个差异都在这个标志下处理：
# - 原生 Windows Python 打不开 /c/Users 这类 MSYS 路径，传参前须 cygpath -m 转成 C:/Users;
# - `ln -s` 默认静默降级为复制，须改用目录联接（mklink /J，无需管理员/开发者模式）;
# - 一般只有 `python` 没有 `python3`，也没有 rsync。
IS_WINDOWS=0
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;; esac

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

PY_BIN="$(command -v python3 || command -v python || true)"
[ -n "$PY_BIN" ] || { echo "python3/python not found on PATH" >&2; exit 1; }

PY_GRAPH="$ROOT/scripts/skill_graph.py"
PY_DOCTOR="$ROOT/scripts/doctor.py"
PY_REGISTRY="$REGISTRY"
if [ "$IS_WINDOWS" -eq 1 ]; then
  PY_GRAPH="$(cygpath -m "$PY_GRAPH")"
  PY_DOCTOR="$(cygpath -m "$PY_DOCTOR")"
  PY_REGISTRY="$(cygpath -m "$PY_REGISTRY")"
fi

# Emit "slug<TAB>role<TAB>abs_src" from the shared graph resolver. Roles keep
# runtime-only `requires` separate from user-facing `requested` / `bundled`
# skills, which later determines the relevant toolchain profiles.
ENTRIES=()
GRAPH_OUTPUT="$("$PY_BIN" "$PY_GRAPH" \
  --registry "$PY_REGISTRY" --format install-tsv \
  ${WANT_SLUGS[@]+"${WANT_SLUGS[@]}"})"
while IFS= read -r line; do
  # Native Windows Python writes CRLF to a pipe. Bash strips only the LF, so
  # without this the trailing CR becomes part of every source path.
  line="${line%$'\r'}"
  [ -n "$line" ] && ENTRIES+=("$line")
done <<< "$GRAPH_OUTPUT"

[ ${#ENTRIES[@]} -gt 0 ] || { echo "no matching active skills in registry." >&2; exit 1; }

SKILL_SUMMARY=()
for entry in "${ENTRIES[@]}"; do
  slug="${entry%%$'\t'*}"; rest="${entry#*$'\t'}"
  role="${rest%%$'\t'*}"
  SKILL_SUMMARY+=("$slug[$role]")
done

ts() { date +%Y%m%d%H%M%S; }
act() { if [ "$DRY" -eq 1 ]; then echo "  [dry-run] $*"; else eval "$@"; fi; }

# Windows 上 `ln -s` 静默降级为复制，改用 PowerShell 创建目录联接
# （junction）：无需管理员/开发者模式，也不依赖 Git Bash 对 `cmd //c` 的
# 参数改写行为。路径通过环境变量传入，避免空格和引号被二次解析。
make_link() {
  if [ "$IS_WINDOWS" -eq 1 ]; then
    local ps_bin src_win dest_win
    ps_bin="$(command -v powershell.exe || command -v pwsh.exe || true)"
    [ -n "$ps_bin" ] || {
      echo "PowerShell is required to create Windows junctions" >&2
      return 1
    }
    src_win="$(cygpath -w "$1")"
    dest_win="$(cygpath -w "$2")"
    LLM_WIKI_JUNCTION_SRC="$src_win" LLM_WIKI_JUNCTION_DEST="$dest_win" \
      "$ps_bin" -NoLogo -NoProfile -NonInteractive -Command '
        $ErrorActionPreference = "Stop"
        $src = [Environment]::GetEnvironmentVariable("LLM_WIKI_JUNCTION_SRC")
        $dest = [Environment]::GetEnvironmentVariable("LLM_WIKI_JUNCTION_DEST")
        New-Item -ItemType Junction -Path $dest -Target $src | Out-Null
      '
  else
    ln -s "$1" "$2"
  fi
}

# Python 3.12 exposes Path.is_junction(); older versions expose the Windows
# reparse-point bit through stat(). This helper keeps install idempotent on both.
is_linklike() {
  if [ "$IS_WINDOWS" -eq 1 ]; then
    "$PY_BIN" -c '
import pathlib, sys
p = pathlib.Path(sys.argv[1])
is_junction = getattr(p, "is_junction", None)
linked = p.is_symlink() or bool(is_junction and is_junction())
if not linked:
    try:
        linked = bool(getattr(p.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)
    except OSError:
        linked = False
raise SystemExit(0 if linked else 1)
' "$(cygpath -m "$1")"
  else
    [ -L "$1" ]
  fi
}

# dest 是否已是指向 src 的链接或 junction。Windows 的 MSYS `readlink` 对
# junction 的行为并不一致，所以让原生 Python 跟随 reparse point 后比较。
links_to() {
  if [ "$IS_WINDOWS" -eq 1 ]; then
    is_linklike "$1" || return 1
    "$PY_BIN" -c '
import pathlib, sys
try:
    same = pathlib.Path(sys.argv[1]).resolve(strict=True) == pathlib.Path(sys.argv[2]).resolve(strict=True)
except OSError:
    same = False
raise SystemExit(0 if same else 1)
' "$(cygpath -m "$1")" "$(cygpath -m "$2")"
  else
    [ -L "$1" ] || return 1
    [ "$(readlink "$1")" = "$2" ]
  fi
}

remove_link() {
  if [ "$IS_WINDOWS" -eq 1 ]; then
    local ps_bin dest_win
    ps_bin="$(command -v powershell.exe || command -v pwsh.exe || true)"
    [ -n "$ps_bin" ] || return 1
    dest_win="$(cygpath -w "$1")"
    LLM_WIKI_JUNCTION_DEST="$dest_win" \
      "$ps_bin" -NoLogo -NoProfile -NonInteractive -Command '
        $ErrorActionPreference = "Stop"
        $dest = [Environment]::GetEnvironmentVariable("LLM_WIKI_JUNCTION_DEST")
        Remove-Item -LiteralPath $dest -Force
      '
  else
    rm "$1"
  fi
}

do_copy() {
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude '.git' --exclude '__pycache__' --exclude '.DS_Store' \
      --exclude 'reports/' --exclude 'data/' "$1/" "$2/"
  else
    # Git Bash 没有 rsync：cp 后清掉运行期/系统产物（data/、reports/ 只在顶层）。
    mkdir -p "$2"
    cp -R "$1/." "$2/"
    rm -rf "$2/.git" "$2/reports" "$2/data"
    find "$2" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find "$2" -name '.DS_Store' -delete 2>/dev/null || true
  fi
}

echo "mode=$MODE  force=$FORCE  dry-run=$DRY"
echo "skills: ${SKILL_SUMMARY[*]}"
echo

n_ok=0; n_done=0; n_skip=0
for target in "${TARGETS[@]}"; do
  echo "▸ target: $target"
  tag="$(basename "$(dirname "$target")")"; tag="${tag#.}"   # ~/.hermes/skills -> hermes
  act "mkdir -p '$target'"
  for entry in "${ENTRIES[@]}"; do
    slug="${entry%%$'\t'*}"; rest="${entry#*$'\t'}"
    role="${rest%%$'\t'*}"; src="${rest#*$'\t'}"
    dest="$target/$slug"
    if [ ! -e "$src" ]; then echo "  ✗ $slug: source missing ($src)"; n_skip=$((n_skip+1)); continue; fi

    if [ "$MODE" = "link" ]; then
      if links_to "$dest" "$src"; then
        echo "  = $slug (up to date)"; n_ok=$((n_ok+1)); continue
      fi
      if is_linklike "$dest"; then
        echo "  ~ $slug: repointing link"
        act "remove_link '$dest'"; act "make_link '$src' '$dest'"; n_done=$((n_done+1)); continue
      fi
      if [ -e "$dest" ]; then
        if [ "$FORCE" -eq 1 ]; then
          echo "  ! $slug: backing up real path → $BACKUP_ROOT/$tag, linking"
          act "mkdir -p '$BACKUP_ROOT/$tag'"; act "mv '$dest' '$BACKUP_ROOT/$tag/$slug.bak-$(ts)'"; act "make_link '$src' '$dest'"; n_done=$((n_done+1))
        else
          echo "  ⏭ $slug: real dir exists (use --force to replace) — skipped"; n_skip=$((n_skip+1))
        fi
        continue
      fi
      echo "  + $slug: linking"
      act "make_link '$src' '$dest'"; n_done=$((n_done+1))
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
      act "do_copy '$src' '$dest'"
      n_done=$((n_done+1))
    fi
  done
  echo
done

echo "done. installed/updated=$n_done  already-ok=$n_ok  skipped=$n_skip"
if [ "$DRY" -eq 1 ]; then echo "(dry-run: nothing changed)"; fi
if [ ${#WANT_SLUGS[@]} -gt 0 ]; then
  echo "toolchain check: $PY_BIN $PY_DOCTOR --skills ${WANT_SLUGS[*]}"
else
  echo "toolchain check: $PY_BIN $PY_DOCTOR"
fi
exit 0
