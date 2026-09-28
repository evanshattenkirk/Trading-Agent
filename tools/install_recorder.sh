#!/bin/bash
# Installs (or updates) the standalone 0DTE quote recorder as a per-user LaunchAgent.
#
# It deploys a pinned copy of the code and its own venv to ~/.agentdesk/recorder-app, because
#  - macOS blocks background jobs from reading ~/Desktop (where the working copy may live), and
#  - edits to the working copy shouldn't change the recorder mid-day. Re-run this script to deploy an update.
#
# Needs uv (https://docs.astral.sh/uv/). Usage: tools/install_recorder.sh [--no-load]
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/.agentdesk/recorder-app"
LABEL="com.agentdesk.recorder"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
[ -x "$UV" ] || { echo "uv not found; install it first (Mac setup)"; exit 1; }

mkdir -p "$APP/src" "$HOME/.agentdesk/recorder" "$HOME/Library/LaunchAgents"
rsync -a --delete --exclude __pycache__ --exclude .venv --exclude .git \
  "$REPO/agentdesk" "$REPO/config.yaml" "$REPO/requirements.txt" "$APP/src/"
[ -f "$REPO/.env" ] && cp "$REPO/.env" "$APP/src/.env" && chmod 600 "$APP/src/.env"
(cd "$REPO" && git rev-parse --short HEAD 2>/dev/null || echo "not-a-git-checkout") > "$APP/DEPLOYED"
date -u +%FT%TZ >> "$APP/DEPLOYED"

[ -x "$APP/.venv/bin/python" ] || "$UV" venv --quiet --python 3.12 "$APP/.venv"
"$UV" pip install --quiet --python "$APP/.venv/bin/python" -r "$APP/src/requirements.txt"

sed -e "s#__APP__#$APP#g" -e "s#__HOME__#$HOME#g" "$REPO/tools/launchd/$LABEL.plist" > "$PLIST"
plutil -lint "$PLIST" >/dev/null

if [ "${1:-}" != "--no-load" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  launchctl enable "gui/$(id -u)/$LABEL"
  echo "Loaded $LABEL (deployed $(head -1 "$APP/DEPLOYED")). Status: $APP/.venv/bin/python -m agentdesk --config $APP/src/config.yaml record-quotes --status"
else
  echo "Deployed to $APP; not loaded (--no-load)."
fi
