#!/bin/bash
# Installs (or updates) the daily paper session as a per-user LaunchAgent (com.agentdesk.paper), the after-hours
# review page (com.agentdesk.review: the last saved session, read-only, on the same port while the engine is off)
# and the Friday Quant job (com.agentdesk.quant, tools/quant_week.sh: SIP replay, weekly report, monthly prune;
# --no-quant leaves it out and removes an installed one).
#
# Like the recorder, it deploys a pinned copy of the code and its own venv (~/.agentdesk/paper-app), because
# macOS blocks background jobs from reading ~/Desktop and edits to the working copy shouldn't change a running
# session. Re-run this after merging engine changes to deploy them. Each install builds a new release next to the
# live one, runs the test suite there and switches to it only if the tests pass (tools/deploy_lib.sh), so a
# failing build is never deployed and the live one stays as it was.
#
# It refuses while an engine is running (a live claim in ~/.agentdesk/claims, port 8765 held by anything but the
# review page, or the paper job running), because reloading the job would stop that day's session. --force
# deploys anyway and leaves the running engine alone: it keeps its code until its next start. It refuses a checkout
# with uncommitted changes unless --dirty, so DEPLOYED (git describe --always --dirty) names what runs.
#
# Needs uv. Usage: tools/install_paper.sh [--no-load] [--force] [--dirty] [--no-quant]
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
. "$REPO/tools/deploy_lib.sh"
APP="$HOME/.agentdesk/paper-app"
LABEL="com.agentdesk.paper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REVIEW="com.agentdesk.review"
REVIEW_PLIST="$HOME/Library/LaunchAgents/$REVIEW.plist"
QUANT="com.agentdesk.quant"
QUANT_PLIST="$HOME/Library/LaunchAgents/$QUANT.plist"
NO_LOAD=no FORCE=no DIRTY=no NO_QUANT=no
while [ $# -gt 0 ]; do
  case "$1" in
    --no-load) NO_LOAD=yes ;;
    --force) FORCE=yes ;;
    --dirty) DIRTY=yes ;;
    --no-quant) NO_QUANT=yes ;;
    *) die "Unknown option $1. Usage: tools/install_paper.sh [--no-load] [--force] [--dirty] [--no-quant]" ;;
  esac
  shift
done

find_uv
grep -q '^live_enabled: false' "$REPO/config.yaml" || die "config.yaml must say live_enabled: false"
check_tree "$REPO" "$DIRTY"
if [ "$FORCE" != yes ] && why="$(engine_running)"; then
  die "An engine is running: $why. Not deployed. Install after 15:10 CT, or run again with --force (the running engine then keeps its code until its next start)."
fi

mkdir -p "$HOME/.agentdesk/paper" "$HOME/.agentdesk/review" "$HOME/.agentdesk/reports" "$HOME/Library/LaunchAgents"
new_release "$APP" "$REPO"
build_venv
run_tests
write_deployed
RUNNING=""
if why="$(engine_running)"; then
  [ "$FORCE" = yes ] || die "An engine started while the tests ran: $why. Run again after 15:10 CT, or with --force."
  RUNNING="$why"
fi
go_live "$APP" "$REPO"
echo "Deployed $DESC to $APP (release ${REL##*/}); src, .venv and DEPLOYED there point at it."

write_plist "$APP/src/tools/launchd/$LABEL.plist" "$PLIST"
write_plist "$APP/src/tools/launchd/$REVIEW.plist" "$REVIEW_PLIST"
[ "$NO_QUANT" = yes ] || write_plist "$APP/src/tools/launchd/$QUANT.plist" "$QUANT_PLIST"
if [ "$NO_LOAD" = yes ]; then
  echo "Not loaded (--no-load)."
  exit 0
fi
if [ -n "$RUNNING" ]; then
  echo "NOT RELOADED: $LABEL, because an engine is running ($RUNNING)."
  echo "  That engine keeps the code it started with until its next start; its next start runs this release."
  echo "  Run tools/install_paper.sh again after 15:10 CT so launchd also picks up the new job settings."
else
  load_job "$LABEL" "$PLIST"   # paper
  echo "Loaded $LABEL (deployed $DESC). Runs weekdays at 08:10 in the Mac's system time zone (keep the Mac on"
  echo "  Central) and at login; on a weekday between 06:00 and 15:00 CT this load starts today's session now."
  echo "  Logs in ~/.agentdesk/paper/"
fi
load_job "$REVIEW" "$REVIEW_PLIST"   # REVIEW
echo "Loaded $REVIEW: http://127.0.0.1:8765 shows the last session read-only while the engine is off; logs in ~/.agentdesk/review/"
if [ "$NO_QUANT" = yes ]; then
  if [ -f "$QUANT_PLIST" ]; then
    launchctl bootout "gui/$(id -u)/$QUANT" 2>/dev/null || true
    rm -f "$QUANT_PLIST"
    echo "Removed $QUANT (--no-quant)."
  fi
else
  load_job "$QUANT" "$QUANT_PLIST"   # QUANT
  echo "Loaded $QUANT: Fridays 16:00 system time (keep the Mac on Central), SIP replay + weekly Quant report into"
  echo "  ~/.agentdesk/reports/quant-<Friday>.md, tools/prune.sh on the first Friday of a month."
fi
