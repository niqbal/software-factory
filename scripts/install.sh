#!/usr/bin/env bash
# Install the software factory from this checkout, check the machine, and optionally add a
# project and open the console. Safe to run again: every step is skipped when already done.
#
#   scripts/install.sh                         # minimal: venv, asf on PATH, machine check
#   scripts/install.sh --project ~/work/myapp  # also add a project (detects everything)
#   scripts/install.sh --ui                    # also open the console on its Set up page
#   scripts/install.sh --with-e2e              # also Playwright + Chromium for browser checks
#
# Options for --project are passed to `asf setup add`, for example:
#   scripts/install.sh --project ~/work/myapp -- --backend pool --github-issues --github-prs
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${ASF_VENV:-$ROOT/.venv}"
BIN_DIR="${ASF_BIN_DIR:-$HOME/.local/bin}"
WITH_E2E=0 LINK=1 UI=0 PROJECT="" EXTRA=()

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --with-e2e) WITH_E2E=1 ;;
    --no-link) LINK=0 ;;
    --ui) UI=1 ;;
    --project) PROJECT="${2:?--project needs a path}"; shift ;;
    --) shift; EXTRA=("$@"); break ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
die()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*" >&2; exit 1; }

say "Prerequisites"
command -v git >/dev/null || die "git not found (macOS: xcode-select --install)"
ok "$(git --version)"
PY=""
for cand in "${ASF_PYTHON:-}" python3.13 python3.12 python3.11 python3; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$(command -v "$cand")"; break
  fi
done
[ -n "$PY" ] || die "Python 3.11 or newer not found (macOS: brew install python@3.12; or set ASF_PYTHON)"
ok "$("$PY" --version) at $PY"

say "Python environment ($VENV)"
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV"
  ok "created"
else
  ok "exists"
fi
EXTRAS="dev"
[ "$WITH_E2E" = 1 ] && EXTRAS="dev,e2e"
LOG="$(mktemp -t asf-install.XXXXXX)"
quiet() { "$@" >"$LOG" 2>&1 || { cat "$LOG" >&2; die "failed: $*"; }; }
quiet "$VENV/bin/python" -m pip install --upgrade pip
quiet "$VENV/bin/python" -m pip install -e "$ROOT[$EXTRAS]"
ok "asf installed ($EXTRAS)"
if [ "$WITH_E2E" = 1 ]; then
  quiet "$VENV/bin/python" -m playwright install chromium
  ok "Chromium for Playwright installed"
fi
rm -f "$LOG"

ASF="$VENV/bin/asf"
if [ "$LINK" = 1 ]; then
  mkdir -p "$BIN_DIR"
  ln -sf "$ASF" "$BIN_DIR/asf"
  ok "linked $BIN_DIR/asf"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on PATH; add this to your shell profile: export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac
fi

say "This machine"
MACHINE_OK=1
"$ASF" setup check || MACHINE_OK=0

if [ -n "$PROJECT" ]; then
  say "Project $PROJECT"
  if [ "$MACHINE_OK" = 0 ]; then
    warn "the machine check failed; adding the project anyway, but jobs will not run until it passes"
  fi
  "$ASF" setup add "$PROJECT" ${EXTRA[@]+"${EXTRA[@]}"} || warn "project checks found problems (see above)"
fi

say "Done"
cat <<EOF
  Console:        asf ui --open          (or: asf ui --setup to add projects in the browser)
  Terminal setup: asf setup              (wizard: machine check, add a project, first work item)
  Docs:           docs/ADD-A-PROJECT.md
EOF

if [ "$UI" = 1 ]; then
  exec "$ASF" ui --setup
fi
