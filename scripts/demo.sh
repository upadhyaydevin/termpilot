#!/bin/sh
# TermPilot demo on synthetic history. Touches nothing outside a temp dir:
# your real ~/.zshrc, history and TermPilot data are never read or written.
#
#   scripts/demo.sh          print ranked suggestions for a few inputs
#   scripts/demo.sh --live   also open a sandboxed zsh with the widget loaded
#
# Uses `termpilot` from PATH, or set TERMPILOT_BIN=/path/to/termpilot.
set -eu

BIN=${TERMPILOT_BIN:-termpilot}
if ! command -v "$BIN" >/dev/null 2>&1; then
  echo "demo: '$BIN' not found; install TermPilot or set TERMPILOT_BIN" >&2
  exit 1
fi
BIN=$(command -v "$BIN")

SANDBOX=$(mktemp -d "${TMPDIR:-/tmp}/termpilot-demo.XXXXXX")
trap 'rm -rf "$SANDBOX"' EXIT
export HOME="$SANDBOX/home" TERMPILOT_DATA_DIR="$SANDBOX/data"
export TERMPILOT_CONFIG="$SANDBOX/config.toml" TERMPILOT_HISTORY="$SANDBOX/zsh_history"
mkdir -p "$HOME"

# A small synthetic history in zsh EXTENDED_HISTORY format.
now=$(date +%s)
i=0
while IFS= read -r cmd; do
  i=$((i + 1))
  printf ': %s:0;%s\n' "$((now - 86400 * 30 + i * 3600))" "$cmd"
done > "$TERMPILOT_HISTORY" <<'EOF'
git status
git checkout main
git checkout -b feature/search-ranking
git status
git commit -m 'Add ranking weights'
git push origin feature/search-ranking
docker compose up -d
docker compose logs -f api
python -m pytest -q
python -m pytest tests/test_ranking.py -k fuzzy
git status
kubectl get pods -n staging
kubectl logs -f deploy/api -n staging
make lint
make test
git checkout main
EOF

run() {
  printf '\n$ termpilot %s\n' "$*"
  "$BIN" "$@" || true
}

run index
run suggest "git ch"
run suggest --explain "git ch"
run suggest "kgp"
run suggest "dock"
run suggest --format inline "python -m"
echo
run stats

if [ "${1:-}" = "--live" ]; then
  ZDOTDIR="$SANDBOX/zdot"
  mkdir -p "$ZDOTDIR"
  SCRIPT=$("$BIN" paths | sed -n 's/^zsh_script=//p')
  cat > "$ZDOTDIR/.zshrc" <<EOF
HISTFILE='$TERMPILOT_HISTORY'
HISTSIZE=1000 SAVEHIST=1000
PS1='termpilot-demo %# '
TERMPILOT_BIN='$BIN'
source '$SCRIPT'
print "Sandboxed zsh with TermPilot. Try typing: git ch   docker c   make"
print "Right arrow / Ctrl-E accepts the dimmed suggestion; Enter runs only what is on the line."
print "Type 'exit' to leave (the sandbox is deleted afterwards)."
EOF
  ZDOTDIR="$ZDOTDIR" zsh -d -i
fi
