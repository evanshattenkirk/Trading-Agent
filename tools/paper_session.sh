#!/bin/zsh
# One market day of the engine in PAPER mode. launchd (com.agentdesk.paper) starts it at 08:10 CT on weekdays.
# Places no orders: forces --mode paper and refuses to start unless config says live_enabled: false.
# Skips NYSE holidays, restarts the engine (up to 5 times) if it dies before 15:00 CT, stops it at 15:10 CT,
# and keeps the Mac from idle-sleeping while it runs. Option quotes are left to the standalone recorder
# (com.agentdesk.recorder), which writes the same journal, so the engine's own recorder is switched off.
set -u
APP="$HOME/.agentdesk/paper-app"
OUT="$HOME/.agentdesk/paper"
DAY="$(TZ=America/Chicago date +%F)"
LOG="$OUT/$DAY.log"
mkdir -p "$OUT"
log() { echo "$(date '+%F %T %Z') paper_session: $*" >> "$LOG"; }
now_ct() { TZ=America/Chicago date +%H%M; }

# NYSE full-day closures (weekends never trigger the job).
HOLIDAYS=(2026-11-26 2026-12-25 2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05
          2027-09-06 2027-11-25 2027-12-24)
if (( ${HOLIDAYS[(Ie)$DAY]} )); then log "market holiday, not running"; exit 0; fi
if [ "$(now_ct)" -ge 1500 ]; then log "started after 15:00 CT (Mac asleep at 08:10?), not running today"; exit 0; fi
if lsof -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then log "port 8765 already in use (another engine running?), not starting"; exit 1; fi

caffeinate -i -w $$ &
cd "$APP/src" || { log "no deployment at $APP/src; run tools/install_paper.sh"; exit 1; }
CFG="$OUT/config.yaml"
sed 's/^\(  record_option_quotes:\) true/\1 false/' config.yaml > "$CFG"
grep -q '^live_enabled: false' "$CFG" || { log "config does not say live_enabled: false, refusing"; exit 1; }
log "deployed $(head -1 "$APP/DEPLOYED"); starting paper engine"

tries=0
while [ "$(now_ct)" -lt 1500 ] && [ $tries -lt 6 ]; do
  tries=$((tries + 1))
  "$APP/.venv/bin/python" -m agentdesk --config "$CFG" run --mode paper --no-browser >> "$LOG" 2>&1 &
  ENG=$!
  while kill -0 $ENG 2>/dev/null && [ "$(now_ct)" -lt 1510 ]; do sleep 20; done
  if kill -0 $ENG 2>/dev/null; then
    log "15:10 CT, stopping engine"
    # The engine doesn't always exit on SIGINT/SIGTERM (MCP teardown), so escalate.
    kill -INT $ENG
    for i in {1..30}; do kill -0 $ENG 2>/dev/null || break; sleep 1; done
    kill $ENG 2>/dev/null; sleep 5; kill -9 $ENG 2>/dev/null
    exit 0
  fi
  wait $ENG; log "engine exited early (code $?), attempt $tries"
  sleep 30
done
log "done for the day"
