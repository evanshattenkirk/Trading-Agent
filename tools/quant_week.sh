#!/bin/bash
# The Friday Quant job (launchd com.agentdesk.quant, Fridays 16:00 in the Mac's system time zone, installed by
# tools/install_paper.sh). From the deployed paper release (~/.agentdesk/paper-app):
#  1. book A's SIP replay of the last 5 sessions (research/iex_vs_sip.py, Alpaca keys from the deployed .env;
#     SIP history is free once it is 15 minutes old) into ~/.agentdesk/reports/sip/<day>,
#  2. the weekly Quant report (python -m reporting.weekly_quant: journal, SIP replays, Post-mortem files) into
#     ~/.agentdesk/reports/quant-<Friday>.md and .json,
#  3. on the first Friday of a month, tools/prune.sh.
# No Robinhood calls, no orders. A failed replay doesn't stop the report. Log: ~/.agentdesk/reports/quant-<day>.log
set -u
AD="$HOME/.agentdesk"
APP="$AD/paper-app"
OUT="$AD/reports"
DAY="$(TZ=America/Chicago date +%F)"
LOG="$OUT/quant-$DAY.log"
mkdir -p "$OUT/sip"
log() { echo "$(date '+%F %T %Z') quant_week: $*" >> "$LOG"; }
ct() { TZ=America/Chicago date "+$1"; }

cd -P "$APP/src" 2>/dev/null || { log "no deployment at $APP/src; run tools/install_paper.sh"; exit 1; }
PY="$(cd -P "$APP/.venv" 2>/dev/null && pwd -P)/bin/python"
# launchd fires at 16:00 system time: on a Mac left on Eastern that is 15:00 CT, before the engine stops at 15:10.
if [ "$(ct %u)" = 5 ] && [ "$(ct %H%M)" -lt 1515 ]; then
  log "waiting for 15:15 CT (the system time zone isn't Central?)"
  while [ "$(ct %H%M)" -lt 1515 ]; do sleep 60; done
fi

if [ -f .env ]; then
  log "SIP replay of the last 5 sessions"
  "$PY" research/iex_vs_sip.py --days 5 --no-diag --env "$PWD/.env" --out "$OUT/sip/$DAY" >> "$LOG" 2>&1 \
    || log "SIP replay failed (code $?); the report runs without this week's replay"
else
  log "no .env in the deployment (Alpaca keys), so no SIP replay this week"
fi
log "weekly Quant report"
"$PY" -m reporting.weekly_quant --journal "$AD/journal.db" --out "$OUT" --sip-dir "$OUT/sip" \
  --postmortem-dir "$AD/postmortems" >> "$LOG" 2>&1 || log "weekly report failed (code $?)"

# Monthly housekeeping, only after the close (a run caught up on Monday morning waits for next month).
if [ "$(ct %d)" -le 7 ] && { [ "$(ct %u)" -ge 6 ] || [ "$(ct %H%M)" -ge 1515 ]; }; then
  log "first week of the month: tools/prune.sh"
  bash tools/prune.sh >> "$LOG" 2>&1 || log "prune failed (code $?)"
fi
log "done"
