#!/bin/bash
# Installs (or updates) the daily paper session as a per-user LaunchAgent (com.agentdesk.paper), and the after-hours
# review page (com.agentdesk.review: the last saved session, read-only, on the same port while the engine is off).
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
REVIEW="com.agentdesk.review"
REVIEW_PLIST="$HOME/Library/LaunchAgents/$REVIEW.plist"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
[ -x "$UV" ] || { echo "uv not found; install it first (Mac setup)"; exit 1; }
grep -q '^live_enabled: false' "$REPO/config.yaml" || { echo "config.yaml must say live_enabled: false"; exit 1; }

mkdir -p "$APP/src" "$HOME/.agentdesk/paper" "$HOME/.agentdesk/review" "$HOME/Library/LaunchAgents"
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
sed -e "s#__APP__#$APP#g" -e "s#__HOME__#$HOME#g" "$REPO/tools/launchd/$REVIEW.plist" > "$REVIEW_PLIST"
plutil -lint "$PLIST" >/dev/null
plutil -lint "$REVIEW_PLIST" >/dev/null

# launchctl bootstrap has failed before while still exiting 0 ("Bootstrap failed: 5: Input/output error"), so check
# that the job is really loaded and retry.
load_job() {
  local label="$1" plist="$2" dom="gui/$(id -u)"
  for try in 1 2 3; do
    launchctl bootout "$dom/$label" 2>/dev/null || true
    sleep 1
    launchctl bootstrap "$dom" "$plist" 2>/dev/null || true
    launchctl enable "$dom/$label" 2>/dev/null || true
    if launchctl print "$dom/$label" >/dev/null 2>&1; then return 0; fi
    echo "$label did not load (attempt $try of 3); retrying"
    sleep 2
  done
  echo "FAILED: $label is not loaded. Try: launchctl bootstrap $dom $plist"
  return 1
}

if [ "${1:-}" != "--no-load" ]; then
  load_job "$LABEL" "$PLIST"   # paper
  echo "Loaded $LABEL (deployed $(head -1 "$APP/DEPLOYED")). Runs weekdays 08:10 CT; logs in ~/.agentdesk/paper/"
  load_job "$REVIEW" "$REVIEW_PLIST"   # REVIEW
  echo "Loaded $REVIEW: http://127.0.0.1:8765 shows the last session read-only while the engine is off; logs in ~/.agentdesk/review/"
else
  echo "Deployed to $APP; not loaded (--no-load)."
fi
