# Port claims for shell scripts. Sourced by tools/paper_session.sh (zsh) and by the installers (bash 3.2), so keep
# it to what both shells and the macOS (BSD) userland share.
#
# Same format and rule as agentdesk/claims.py: a file named after the pid in the claims dir (~/.agentdesk/claims),
# holding that process's start time as `ps -o lstart=` prints it in UTC and the C locale. A claim counts only while
# the pid is alive and started at that time, so a pid reused after a reboot or a kill -9 can't keep it alive.
# An empty claim (the format before 2026-10-07) counts while the pid is alive.

_claim_start() {   # pid -> its start time, whitespace collapsed; empty when there is no such process
  LC_ALL=C TZ=UTC ps -o lstart= -p "$1" 2>/dev/null | tr -s ' \t\n' ' ' | sed -e 's/^ //' -e 's/ $//'
}

write_claim() {    # dir pid
  mkdir -p "$1" && _claim_start "$2" > "$1/$2"
}

claim_alive() {    # claim file -> status 0 while the process that wrote it is still running
  local f="$1" pid want have
  pid="${f##*/}"
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  have="$(_claim_start "$pid")"
  [ -n "$have" ] || return 1
  want="$(tr -s ' \t\n' ' ' < "$f" 2>/dev/null | sed -e 's/^ //' -e 's/ $//')"
  [ -z "$want" ] && return 0
  [ "$want" = "$have" ]
}

live_claims() {    # dir -> the pids holding a live claim, one per line
  local p
  for p in $(ls "$1" 2>/dev/null); do
    claim_alive "$1/$p" && echo "$p"
  done
  return 0
}
