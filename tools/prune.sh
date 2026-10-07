#!/bin/bash
# Monthly housekeeping for ~/.agentdesk. tools/quant_week.sh runs it after the close on the first Friday of each
# month; it can also be run by hand. It prints every action; --dry-run prints them and changes nothing.
#  - gzips paper day logs (paper/YYYY-MM-DD.log) and saved review sessions (sessions/*.snapshot.json,
#    *.events.jsonl) older than 30 days; a gzipped session no longer appears on the review page
#  - rotates launchd .out/.err logs over 10 MB: one gzipped copy (.1.gz), then truncated in place (launchd keeps
#    them open)
#  - deletes SIP-replay cache files older than 30 days (cache-iex-vs-sip; research/iex_vs_sip.py fetches them again)
#  - moves journal.option_quotes and journal.rh_calls rows older than 90 days into archive/quotes-YYYY.db
#    (tools/prune_journal.py; the weekly Quant report reads those archives too)
# macOS bash 3.2 and BSD find/gzip. Usage: tools/prune.sh [--dry-run]
set -u
AD="${AGENTDESK_HOME:-$HOME/.agentdesk}"
DRY=""
if [ "${1:-}" = "--dry-run" ]; then DRY=yes; fi
say() { echo "prune: $*${DRY:+ (dry run)}"; }
old_files() {    # dir name-pattern: regular files directly in dir, last changed more than 30 days ago
  if [ -d "$1" ]; then find "$1" -maxdepth 1 -type f -name "$2" -mtime +30 | sort; fi
}

for f in $(old_files "$AD/paper" '????-??-??.log') $(old_files "$AD/sessions" '*.snapshot.json') \
         $(old_files "$AD/sessions" '*.events.jsonl'); do
  say "gzip $f"
  [ -n "$DRY" ] || gzip -f "$f"
done
for f in "$AD"/paper/launchd.*.log "$AD"/review/launchd.*.log "$AD"/recorder/launchd.*.log "$AD"/reports/launchd.*.log; do
  [ -f "$f" ] || continue
  size="$(wc -c < "$f" | tr -d ' ')"
  [ "$size" -gt 10485760 ] || continue
  say "rotate $f ($((size / 1048576)) MB) into $f.1.gz"
  [ -n "$DRY" ] || { gzip -c "$f" > "$f.1.gz" && : > "$f"; }
done
for f in $(old_files "$AD/cache-iex-vs-sip" '*'); do
  say "delete $f"
  [ -n "$DRY" ] || rm -f "$f"
done

PY="${PRUNE_PY:-$(cd -P "$AD/paper-app/.venv" 2>/dev/null && pwd -P)/bin/python}"
[ -x "$PY" ] || PY="python3"
"$PY" "$(dirname "$0")/prune_journal.py" --journal "$AD/journal.db" --archive-dir "$AD/archive" --days 90 ${DRY:+--dry-run}
