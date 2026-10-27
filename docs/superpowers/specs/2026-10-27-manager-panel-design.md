# Manager panel: design

Evan, 2026-10-27: "robinhood was disconnected, just reconnected it" ... "add a manager panel for me to check on that
stuff". Today the only signs of a Robinhood sign-in problem are the Ops desk's pre-flight line (once, at 08:25), a
log line and the REC pill. Nothing shows the token deadlines, the recorder's own sign-in, or the Mac jobs in one
place, and nothing at all shows them while the engine is off.

## What Evan sees

- A **Manager** button in the dashboard header, next to the REC pill, with a dot: green when every check passes,
  amber when something needs doing soon, red when something is broken now. The dot shows on the live dashboard and
  on the after-hours review page, so a disconnect is visible without opening anything.
- Clicking it (or opening `http://127.0.0.1:8765/#manager`) opens a panel over the side column with one row per
  check: a coloured dot, the name, one line of detail, and, when the row isn't green, **what to do** (the exact
  Terminal command, with a Copy button, and when to run it, e.g. "outside market hours").
- The panel refreshes every 30 s from `GET /api/health`.
- It only reports. It has no buttons that sign in, restart or change anything: those need Terminal on the Mac.

## The checks

| Check | Green | Amber | Red | What to do |
|---|---|---|---|---|
| Engine Robinhood sign-in (`~/.agentdesk/rh_oauth.json`) | valid, shows "until Oct 30 09:12 CT (2d 20h)" | under 3 days left | expired, under 1 day left, a refused refresh, or the engine reports it signed out | `rh-inspect` command (outside market hours) |
| Recorder Robinhood sign-in (`~/.agentdesk/recorder/rh_oauth.json`) | same rules | | | `record-quotes --once` command |
| Engine Robinhood calls | last good call under 60 s ago in market hours; error counts for the last 15 min | rate limits or timeouts in the last 15 min | no good call for 2 min in market hours, or auth errors | read the line; sign in if auth |
| Recorder writing | heartbeat under 2 min old (same rule as the REC pill), today's and the last session's quote rows | | down in market hours | `tools/install_recorder.sh` / recorder log path |
| SPY data feed (Alpaca) | last print under 60 s old in market hours | | stalled | engine log path |
| Engine | running, mode | | not running on a trading day after 08:15 CT | kickstart command |
| Halts | none | one book halted (its reason) | account-wide halt (its reason) | `--clear-halt` only where it applies |
| Mac jobs (paper, review, quant, recorder) | loaded, last exit 0 | | not loaded or last exit non-zero | installer command |

Outside market hours, rows that only matter during the session (calls, feed, recorder writing) show grey "idle"
with the last value, never red. Weekends and `calendar.holidays` count as outside.

## How it works

1. **`agentdesk/health.py`** (new, pure): one function per check, each taking paths, `now` and, when the engine is
   running, its snapshot; each returns `{name, state: ok|warn|bad|idle, detail, fix}`. `report(cfg, now, engine=None)`
   runs them all. It reads local files only: the two token files (expiry from `saved_at + expires_in`, as
   `FileTokenStorage.expires_at` already does; never the token itself), the recorder heartbeat and `days.jsonl`,
   `journal.rh_calls` (recorder and IV calls), the claims folder, the saved session snapshot, and on macOS
   `launchctl print` for the four jobs (cached 60 s; "n/a" elsewhere). No Robinhood, Alpaca or Claude calls.
2. **Engine call stats.** `RobinhoodMCP.call` keeps in-memory counters (last good call, last error with its kind,
   rate limits, timeouts and reconnects, per 15-minute window and per day). The engine already halts and logs on
   these; this only counts them. The snapshot gains `health` (those stats, the sign-in state and the last print
   time), so the review page can show the engine's last known values with "as of".
3. **`GET /api/health`** on both the engine server and the review server. Like `/api/state`, it needs no token; the
   response holds times, counts and commands, never a token, account number or balance.
4. **Web:** `web/manager.js` (button, panel, polling), pure helpers in `web/panel.js` (state colour, "2d 20h"
   countdown, worst-of rollup), styles in `styles.css`. Rebuild the demo with `tools/build_demo.py`.

The Ops desk's pre-flight check is unchanged; it keeps going red under a day left.

## Testing (test-first)

- `tests/test_health.py`: each check with temp token files, heartbeat, `days.jsonl`, `rh_calls` rows and a fake
  `launchctl`; market hours vs evening vs holiday; a token file without `saved_at` (dated from its mtime).
- Engine call stats: rate-limited, timeout, reconnect and auth paths bump the right counters.
- Both apps: `/api/health` answers GET without a token, refuses other methods on the review app, and the body never
  contains the token file's contents or the account number.
- `tests/web/panel.test.js`: the new helpers under node.

## Not in scope

macOS notifications, the claude.ai Robinhood connector (it signs in separately and the engine can't see it),
buttons that act, and any change to trading behaviour or the Ops desk.
