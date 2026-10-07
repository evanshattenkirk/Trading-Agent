# Shared by tools/install_paper.sh and tools/install_recorder.sh (sourced). Runs under macOS /bin/bash 3.2 with the
# BSD userland, so: no GNU-only flags, no bash 4 features.
#
# An app dir ($APP, e.g. ~/.agentdesk/paper-app) holds tested releases and points at one of them:
#   releases/<UTC time>-<pid>/src, .venv, DEPLOYED    one deployment each, copied, installed and tested in place
#   current -> releases/<...>                         switched by one rename, only after the tests passed there
#   src -> current/src, .venv -> current/.venv, DEPLOYED -> current/DEPLOYED   the paths the plists and docs use
# A failed build never touches the live release. The current release and the two newest others are kept, so a
# process still running from the previous one (paper_session.sh resolves the real path at start) keeps its files.

. "$(dirname "${BASH_SOURCE[0]}")/claims.sh"

PORT=8765
DEPLOY_PATHS="agentdesk reporting research tests tools config.yaml requirements.txt"
OPTIONAL_PATHS="requirements.lock pyproject.toml docs README.md CLAUDE.md HANDOFF.md"   # docs: a test may read them
STATE_FILES=".env overrides.yaml"      # machine state that lives in the deployed tree, carried to each new release
REL=""
REL_LIVE=""

die() { echo "$*"; exit 1; }

find_uv() {
  UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
  [ -x "$UV" ] || die "uv not found; install it first (Mac setup)"
}

check_tree() {   # repo allow_dirty(yes/no) -> sets DESC (git describe --always --dirty); refuses a dirty tree
  local repo="$1" allow="$2" dirty
  if ! git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then DESC="not-a-git-checkout"; return 0; fi
  dirty="$( (git -C "$repo" status --porcelain --untracked-files=no
            git -C "$repo" status --porcelain --untracked-files=all -- agentdesk reporting tests tools | grep '^??') \
            2>/dev/null)" || true
  DESC="$(git -C "$repo" describe --always --dirty 2>/dev/null)" || DESC="unknown"
  [ -n "$dirty" ] || return 0
  if [ "$allow" != yes ]; then
    echo "The checkout has changes that are not committed:"
    echo "$dirty"
    die "Not deployed (DEPLOYED would name a commit that isn't what runs). Commit them, or run again with --dirty."
  fi
  case "$DESC" in *-dirty) ;; *) DESC="$DESC-dirty" ;; esac
}

engine_running() {   # prints what is running and returns 0 while an engine (or its paper session) is up
  local pids pid cmd job
  pids="$(live_claims "$HOME/.agentdesk/claims" | tr '\n' ' ')"
  if [ -n "${pids% }" ]; then echo "pid ${pids% } holds a dashboard claim in ~/.agentdesk/claims"; return 0; fi
  for pid in $(lsof -t -iTCP:$PORT -sTCP:LISTEN 2>/dev/null); do
    cmd="$(ps -o command= -p "$pid" 2>/dev/null)" || cmd=""
    case "$cmd" in *agentdesk*" review"*) continue ;; esac        # the after-hours review page, not an engine
    echo "port $PORT is in use by pid $pid (${cmd:-unknown command})"; return 0
  done
  job="$(launchctl print "gui/$(id -u)/com.agentdesk.paper" 2>/dev/null)" || job=""   # no grep -q: pipefail + SIGPIPE
  case "$job" in *"state = running"*) echo "launchd job com.agentdesk.paper is running"; return 0 ;; esac
  return 1
}

new_release() {   # app repo -> sets REL; copies the deploy paths from the checkout into REL/src
  local app="$1" repo="$2" p
  REL="$app/releases/$(date -u +%Y%m%dT%H%M%SZ)-$$"
  mkdir -p "$REL/src"
  trap 'abandon_release' EXIT
  set --
  for p in $DEPLOY_PATHS $OPTIONAL_PATHS; do
    if [ -e "$repo/$p" ]; then set -- ${1+"$@"} "$repo/$p"; fi      # ${1+...}: bash 3.2 with set -u
  done
  rsync -a --exclude __pycache__ --exclude .venv --exclude .git --exclude /research/data/ "$@" "$REL/src/"
}

abandon_release() {   # EXIT trap: a release that never went live is removed
  if [ -n "$REL" ] && [ "$REL_LIVE" != yes ]; then
    rm -rf "$REL"
    echo "Not deployed; the live release is unchanged."
  fi
}

build_venv() {   # its own venv per release; requirements.lock (pinned) when the checkout has one
  "$UV" venv --quiet --python 3.12 "$REL/.venv"
  if [ -f "$REL/src/requirements.lock" ]; then
    "$UV" pip sync --quiet --python "$REL/.venv/bin/python" "$REL/src/requirements.lock"
  else
    "$UV" pip install --quiet --python "$REL/.venv/bin/python" -r "$REL/src/requirements.txt"
  fi
}

run_tests() {
  echo "Testing the new release in $REL/src (about 3 minutes)..."
  (cd "$REL/src" && "$REL/.venv/bin/python" -m pytest -q -p no:cacheprovider tests) || die "Tests failed."
}

write_deployed() {
  printf '%s\n%s\n' "$DESC" "$(date -u +%FT%TZ)" > "$REL/DEPLOYED"
}

go_live() {   # app repo: carries the machine state over, switches current, then keeps 3 releases
  local app="$1" repo="$2" f x old
  for f in $STATE_FILES; do
    if [ -f "$app/src/$f" ]; then cp -p "$app/src/$f" "$REL/src/$f"; fi
  done
  if [ -f "$repo/.env" ]; then cp "$repo/.env" "$REL/src/.env"; fi
  if [ -f "$REL/src/.env" ]; then chmod 600 "$REL/src/.env"; fi
  rm -f "$app/current.new"
  ln -s "releases/${REL##*/}" "$app/current.new"
  "$REL/.venv/bin/python" -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$app/current.new" "$app/current"
  REL_LIVE=yes
  # The first install with releases moves the old src/.venv/DEPLOYED into a release of their own.
  old="$app/releases/00000000T000000Z-before-releases"
  for x in src .venv DEPLOYED; do
    if [ -e "$app/$x" ] && [ ! -L "$app/$x" ]; then mkdir -p "$old"; mv "$app/$x" "$old/$x"; fi
    if [ ! -e "$app/$x" ] && [ ! -L "$app/$x" ]; then ln -s "current/$x" "$app/$x"; fi
  done
  prune_releases "$app"
}

prune_releases() {   # app: keeps the current release and the two newest others
  local app="$1" cur r n=0
  cur="$(cd -P "$app/current" && pwd -P)" || return 0
  for r in $(ls -1 "$app/releases" | sort -r); do
    [ -d "$app/releases/$r" ] || continue
    [ "$(cd -P "$app/releases/$r" && pwd -P)" = "$cur" ] && continue
    n=$((n + 1))
    [ "$n" -le 2 ] && continue
    rm -rf "$app/releases/$r"
  done
  return 0
}

# launchctl bootstrap has failed before while still exiting 0 ("Bootstrap failed: 5: Input/output error"), so check
# that the job is really loaded and retry.
load_job() {
  local label="$1" plist="$2" dom="gui/$(id -u)" try
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

write_plist() {   # template output
  sed -e "s#__APP__#$APP#g" -e "s#__HOME__#$HOME#g" "$1" > "$2"
  plutil -lint "$2" >/dev/null
}
