#!/bin/zsh
# One market day of the engine in PAPER mode. launchd (com.agentdesk.paper) starts it at 08:10 on weekdays in the
# Mac's *system* time zone (set the Mac to Central; the day log gets a WARN line when it isn't) and at login
# (RunAtLoad), so a reboot or a Mac that was off at 08:10 still gets its day.
# Places no orders: forces --mode paper and refuses to start unless config says live_enabled: false.
# Exits quietly on weekends and before 06:00 CT, and doesn't start at or after 15:00 CT, on the NYSE holidays in the
# deployed config.yaml (calendar.holidays, the list the engine uses) or while another engine holds a dashboard
# claim. Started between 06:00 and 07:55 CT (an Eastern system zone fires 08:10 at 07:10 CT) it waits for 08:10 CT.
# Restarts the engine (up to 5 times) if it dies before 15:00 CT, stops it at 15:10 CT, and keeps the Mac from
# idle-sleeping while it runs. The after-hours review page gives the port back at start. Option quotes are left to
# the standalone recorder (com.agentdesk.recorder), which writes the same journal, so the engine's own recorder is
# switched off. Runs under zsh (the plist) and bash (the tests), so it keeps to what both shells share.
set -u
APP="$HOME/.agentdesk/paper-app"
OUT="$HOME/.agentdesk/paper"
DAY="$(TZ=America/Chicago date +%F)"
LOG="$OUT/$DAY.log"
CLAIMS="$HOME/.agentdesk/claims"          # review.claims_dir in config.yaml
log() { mkdir -p "$OUT"; echo "$(date '+%F %T %Z') paper_session: $*" >> "$LOG"; }
quiet() { echo "$(date '+%F %T %Z') paper_session: $*"; exit 0; }     # launchd.out.log only, no day log
now_ct() { TZ=America/Chicago date +%H%M; }
. "$(dirname "$0")/claims.sh"

[ "$(TZ=America/Chicago date +%u)" -le 5 ] || quiet "not a weekday in Central time"
[ "$(now_ct)" -ge 0600 ] || quiet "before 06:00 CT (a login or reboot at night); the 08:10 run starts the day"
if [ "$(now_ct)" -ge 1500 ]; then
  [ -f "$LOG" ] && quiet "after 15:00 CT; today's session is over"
  log "started after 15:00 CT (Mac asleep or off at 08:10?), not running today"; exit 0
fi

# Run from the real path of the deployed release, so a later install can't swap the code under this day's engine.
cd -P "$APP/src" 2>/dev/null || { log "no deployment at $APP/src; run tools/install_paper.sh"; exit 1; }
PY="$(cd -P "$APP/.venv" 2>/dev/null && pwd -P)/bin/python"

# NYSE full-day closures: calendar.holidays in the deployed config.yaml, the list the engine and the Earnings desk use.
case "$("$PY" -c 'import sys, yaml; c = yaml.safe_load(open(sys.argv[1])) or {}; print("holiday" if sys.argv[2] in {str(d) for d in (c.get("calendar") or {}).get("holidays") or []} else "open")' config.yaml "$DAY" 2>/dev/null)" in
  holiday) log "market holiday (calendar.holidays), not running"; exit 0 ;;
  open) ;;
  *) log "WARN: could not read calendar.holidays from $PWD/config.yaml; running as on a trading day" ;;
esac

# launchd's StartCalendarInterval follows the system zone, not the TZ a shell exports.
if [ "$( (unset TZ; date +%z) )" != "$(TZ=America/Chicago date +%z)" ]; then
  zone="$(readlink /etc/localtime 2>/dev/null | sed 's#.*zoneinfo/##')"
  [ -n "$zone" ] || zone="$( (unset TZ; date +%Z) )"
  log "WARN: launchd runs StartCalendarInterval in the system time zone ($zone); set System Settings > General > Date & Time to Central"
fi

caffeinate -i -w $$ &
if [ "$(now_ct)" -lt 0755 ]; then
  log "started at $(now_ct) CT; waiting for 08:10 CT"
  while [ "$(now_ct)" -lt 0810 ]; do sleep 30; done
  cd -P "$APP/src" 2>/dev/null || { log "no deployment at $APP/src; run tools/install_paper.sh"; exit 1; }
  PY="$(cd -P "$APP/.venv" 2>/dev/null && pwd -P)/bin/python"      # an install made during the wait counts
fi

others="$(live_claims "$CLAIMS" | tr '\n' ' ')"
if [ -n "$others" ]; then log "an engine already holds the dashboard port (claim by pid ${others% }), not starting another"; exit 0; fi
# Claim the dashboard port so the after-hours review page (com.agentdesk.review) steps aside; the claim goes when
# this script exits, and one left by a crash or a reboot is ignored (tools/claims.sh records the start time).
write_claim "$CLAIMS" $$; trap 'rm -f "$CLAIMS/$$"' EXIT
for i in {1..20}; do
  lsof -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1 || break
  [ $i -eq 1 ] && log "waiting for the review page to free port 8765"
  sleep 1
done
if lsof -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then log "port 8765 already in use (another engine running?), not starting"; exit 1; fi

mkdir -p "$OUT"; CFG="$OUT/config.yaml"
sed 's/^\(  record_option_quotes:\) true/\1 false/' config.yaml > "$CFG"
grep -q '^live_enabled: false' "$CFG" || { log "config does not say live_enabled: false, refusing"; exit 1; }
log "deployed $(head -1 ../DEPLOYED 2>/dev/null); starting paper engine"

tries=0
while [ "$(now_ct)" -lt 1500 ] && [ $tries -lt 6 ]; do
  tries=$((tries + 1))
  "$PY" -m agentdesk --config "$CFG" run --mode paper --no-browser >> "$LOG" 2>&1 &
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
