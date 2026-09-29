#!/bin/bash
# Installs (or updates) the daily paper session as a per-user LaunchAgent (com.agentdesk.paper).
#
# Like the recorder, it deploys a pinned copy of the code and its own venv (~/.agentdesk/paper-app), because
# macOS blocks background jobs from reading ~/Desktop and edits to the working copy shouldn't change a running
# session. Re-run this after merging engine changes to deploy them. Refuses to deploy if the tests fail.
#
# Needs uv. Usage: tools/install_paper.sh [--no-load]
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/.agentdesk/paper-app"
LABEL="com.agentdesk.paper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
[ -x "$UV" ] || { echo "uv not found; install it first (Mac setup)"; exit 1; }
grep -q '^live_enabled: false' "$REPO/config.yaml" || { echo "config.yaml must say live_enabled: false"; exit 1; }

mkdir -p "$APP/src" "$HOME/.agentdesk/paper" "$HOME/Library/LaunchAgents"
rsync -a --delete --exclude __pycache__ --exclude .venv --exclude .git --exclude /research/data/ \
  "$REPO/agentdesk" "$REPO/reporting" "$REPO/research" "$REPO/tests" "$REPO/tools" \
  "$REPO/config.yaml" "$REPO/requirements.txt" "$APP/src/"
[ -f "$REPO/.env" ] && cp "$REPO/.env" "$APP/src/.env" && chmod 600 "$APP/src/.env"

[ -x "$APP/.venv/bin/python" ] || "$UV" venv --quiet --python 3.12 "$APP/.venv"
"$UV" pip install --quiet --python "$APP/.venv/bin/python" -r "$APP/src/requirements.txt"
(cd "$APP/src" && "$APP/.venv/bin/python" -m pytest -q tests) || { echo "tests failed; not loading"; exit 1; }

(cd "$REPO" && git rev-parse --short HEAD 2>/dev/null || echo "not-a-git-checkout") > "$APP/DEPLOYED"
date -u +%FT%TZ >> "$APP/DEPLOYED"

sed -e "s#__APP__#$APP#g" -e "s#__HOME__#$HOME#g" "$REPO/tools/launchd/$LABEL.plist" > "$PLIST"
plutil -lint "$PLIST" >/dev/null

if [ "${1:-}" != "--no-load" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  launchctl enable "gui/$(id -u)/$LABEL"
  echo "Loaded $LABEL (deployed $(head -1 "$APP/DEPLOYED")). Runs weekdays 08:10 CT; logs in ~/.agentdesk/paper/"
else
  echo "Deployed to $APP; not loaded (--no-load)."
fi
