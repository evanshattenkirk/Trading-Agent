#!/bin/bash
# Installs (or updates) the standalone 0DTE quote recorder as a per-user LaunchAgent.
#
# It deploys a pinned copy of the code and its own venv to ~/.agentdesk/recorder-app, because
#  - macOS blocks background jobs from reading ~/Desktop (where the working copy may live), and
#  - edits to the working copy shouldn't change the recorder mid-day. Re-run this script to deploy an update.
# Each install builds a new release next to the live one, runs the test suite there and switches to it only if the
# tests pass (tools/deploy_lib.sh); loading then restarts the recorder, so install outside 08:25-15:05 CT. It
# refuses a checkout with uncommitted changes unless --dirty.
#
# Needs uv (https://docs.astral.sh/uv/). Usage: tools/install_recorder.sh [--no-load] [--dirty]
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
. "$REPO/tools/deploy_lib.sh"
APP="$HOME/.agentdesk/recorder-app"
LABEL="com.agentdesk.recorder"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
NO_LOAD=no DIRTY=no
while [ $# -gt 0 ]; do
  case "$1" in
    --no-load) NO_LOAD=yes ;;
    --dirty) DIRTY=yes ;;
    *) die "Unknown option $1. Usage: tools/install_recorder.sh [--no-load] [--dirty]" ;;
  esac
  shift
done

find_uv
check_tree "$REPO" "$DIRTY"
mkdir -p "$HOME/.agentdesk/recorder" "$HOME/Library/LaunchAgents"
new_release "$APP" "$REPO"
build_venv
run_tests
write_deployed
go_live "$APP" "$REPO"
echo "Deployed $DESC to $APP (release ${REL##*/})."

write_plist "$APP/src/tools/launchd/$LABEL.plist" "$PLIST"
if [ "$NO_LOAD" = yes ]; then
  echo "Not loaded (--no-load)."
  exit 0
fi
load_job "$LABEL" "$PLIST"
echo "Loaded $LABEL (deployed $DESC). Status: $APP/.venv/bin/python -m agentdesk --config $APP/src/config.yaml record-quotes --status"
