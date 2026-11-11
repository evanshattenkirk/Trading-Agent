# Phone alerts from the Manager panel: design

Evan, 2026-11-09, on making the paper engine easier to run unattended: "improve it all". He picked **iMessage to
himself** as the channel (decision card, 2026-11-09; confirmed 2026-11-10: "imessage to myself is good"). Today a
problem only shows if he opens the dashboard: the Manager panel (`/api/health`, `agentdesk/health.py`, PR #60) goes
amber or red, and the recorder alone posts a Mac banner nobody sees when away from the Mac. Robinhood sign-ins last
about 9.6 days and re-auth is by hand, so an expiring token is the most common surprise.

## What Evan gets

- An **iMessage to his own number or Apple ID**, sent by the Mac's Messages app, and the same text as a **Mac
  banner**, when a Manager panel row turns amber or red. Each message names the row, its one-line detail, what to do
  and the exact Terminal command, e.g.

  > AgentDesk 🟠 Engine Robinhood sign-in: signed in until Thu Nov 13 09:12 CT (2d 20h left).
  > Sign in again before then, outside market hours: run this in Terminal.
  > cd ~/Trading-Agent && .venv/bin/python -m agentdesk rh-inspect

- The **token heads-up** is the sign-in rows' existing amber state (under 3 days left), so it arrives about 3 days
  before a sign-in expires, and red under 1 day.
- A **"fixed" message** (✅) once a row that alerted is green or idle again.
- A **morning all-clear** at 08:20 CT on trading days ("AgentDesk 08:20: all green, engine running in paper"), or
  the list of what isn't green. If it doesn't arrive, the alerting itself is broken. `alerts.morning: false` turns
  it off.
- Nothing else: no repeats of the same amber, no message for a row that only changes its wording (a countdown).

## Rules

| Change | When it is sent |
|---|---|
| A row goes red (`bad`) | at once, any hour |
| It stays red | again every 60 min (`alerts.repeat_bad_min`), only 07:00-21:00 CT, at most 2 repeats a day |
| A row goes amber (`warn`) | once it has been amber on 2 checks in a row (10 min), 07:00-21:00 CT; one that turns amber overnight is sent at 07:00 if still amber |
| A row goes red -> amber | sent (it changed), same hours as amber |
| A row that alerted goes green or idle | ✅ once it has been green on 2 checks in a row, same hours as amber (held overnight, dropped if it turns bad again) |
| Idle rows (market closed, engine off) | never alert |

All changes found in one check go out as one message, worst first. Rows are keyed by their `name`. The two-check
rules (added after the final review, 2026-11-11) keep a flickering row quiet: "Engine Robinhood calls" turns amber on
a single rate limit, and without them one rate limit every 20 minutes would send ~36 texts a day.

**Nothing answers on port 8765** (neither the engine nor the review page): one silent check judges nothing (a slow
answer or the engine's start-up handoff of the port). From the second silent check in a row (10 min) the watcher runs
`health.report` itself on local files (engine not running, no snapshot), so sign-ins, recorder and Mac jobs are still
judged, and adds a red **Dashboard** row ("neither the engine nor the review page answers on :8765").

A message never carries a token, account number or balance. The panel's rows already never hold them; the watcher
sends only `name`, `detail`, `fix` and `cmd`.

## How it works

1. **`agentdesk/alerts.py`** (new, pure apart from `send`):
   - `diff(prev_state, report, now, cfg) -> (messages, new_state)`: the rules above. State per row: last state
     sent, when, and anything held for the morning. No I/O, so the rules are tested directly.
   - `fetch(url)`: GET `/api/health` with a 10 s timeout (stdlib `urllib`), `None` on any failure.
   - `send(text, to)`: `osascript` with the handle and text passed as arguments (`on run argv`), so nothing is
     quoted into the script: Messages, iMessage service, `send text to participant handle`. Then `display
     notification` for the banner. Returns whether the iMessage went; a failure is logged and the message is kept
     and retried on the next check (at most 3 times, then dropped with a log line), and the banner still shows.
2. **`python -m agentdesk alerts`** (new CLI command): one check, then exit. `--test` sends "AgentDesk alerts are
   on" and exits. State lives in `~/.agentdesk/alerts/state.json` (written atomically), the log in
   `~/.agentdesk/alerts/alerts.log`.
3. **LaunchAgent `com.agentdesk.watch`** (`tools/launchd/com.agentdesk.watch.plist`): runs the command every 300 s
   (`StartInterval`) and at load, from the paper app's release (`~/.agentdesk/paper-app`). `tools/install_paper.sh`
   installs and verifies it with the other three jobs; it doesn't touch the engine, so it never needs the engine
   stopped by itself. A sleeping Mac runs it on wake.
4. **Recipient**: `ALERT_IMESSAGE_TO` in `~/Trading-Agent/.env`, which Evan types himself (his number in +1...
   form, or his Apple ID email); the installer already copies `.env` into each release. Without it the watcher
   still shows Mac banners and logs one warning a day. It is never printed in full in logs (last 4 characters only).
5. **Config** (`config.yaml`): `alerts: {enabled: true, day_start: "07:00", day_end: "21:00", repeat_bad_min: 60,
   morning: true, morning_at: "08:20", state_dir: ~/.agentdesk/alerts}`. The interval is the plist's 300 s only.
6. **`health.JOBS`** gains `com.agentdesk.watch`, so the panel's Mac jobs row goes red if the watcher isn't loaded.

### macOS permission

The first iMessage sent from the launchd job makes macOS ask once whether `osascript` may control Messages. Each
install resets the state's `hello`, so the job's first run after the install (at load) texts "AgentDesk alerts are
on"; that is when the prompt shows, while Evan is at the Mac, and the installer prints: "If macOS asks whether
osascript may control Messages, click OK. Then check your phone for 'AgentDesk alerts are on'."
`python -m agentdesk alerts --test` sends the same text by hand. Messages must be signed in to iMessage on the Mac.
Texting your own number shows in a conversation with yourself; if the phone doesn't buzz for that, Evan can use his
Apple ID email as the handle instead.

## Out of scope

- Posting alerts into the Claude project (Evan chose iMessage only, 2026-11-10).
- Any action on an alert: the watcher only reports, like the panel. Signing in and restarts stay Terminal steps.
- New checks: the watcher alerts on whatever rows the panel has (including the crew row from PR #67).

## Testing (test-first)

- `tests/test_alerts.py`: `diff` for each rule (first red at 03:00 goes, a repeat at 03:00 doesn't; amber at 22:00
  is held and sent at 07:00 only if still amber; red -> amber; a ✅ after an alert and none after a row that never
  alerted; wording-only change sends nothing; idle never alerts; several changes in one message, worst first); the
  unreachable-port fallback with its two-check grace; the morning all-clear on a trading day and not on a weekend
  or `calendar.holidays`; `send` builds the `osascript` argument list without the handle or text inside the script,
  keeps a failed message for retry and drops it after 3 tries; no message text contains a token file's contents.
- `tests/test_health.py`: the Mac jobs row includes `watch`.
- Installer: the plist template is filled and loaded like the review job (checked by the existing installer
  tests where they cover plists).
