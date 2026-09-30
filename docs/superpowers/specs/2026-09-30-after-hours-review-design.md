# After-hours dashboard (review mode): design and plan

Evan, 2026-09-30: "build an after hours view for if its open after hours". Today http://127.0.0.1:8765 goes
dark at 15:10 CT because the paper engine serves it and the engine stops then.

## What Evan sees

Outside the engine's hours (after the close, evenings, weekends, holidays) the same URL shows the most recent
paper session, read-only:

- The page looks like the live dashboard as it stood when the engine stopped: SPY chart with all four
  timeframes, entry/exit marks, levels, the book tabs, open positions (book E and F2 hold overnight), risk and
  day P&L, crew briefs and votes, proposals, the Trades table and the Activity feed.
- The mode pill reads `REVIEW`. A banner says which session it is and when it was saved, and that the live view
  comes back at 08:10 CT on the next trading day.
- No Pause, Flatten or Kill buttons; proposal Approve/Reject are disabled. The review server has no control
  endpoints at all, never talks to Robinhood, Alpaca or Claude, and makes no web calls.
- A day picker lists earlier saved sessions.
- At 08:10 CT the paper engine takes the port back and the live dashboard returns as today.

## How it works

1. **The engine saves its session (paper, shadow and live runs; not sim).** Two files per day in
   `~/.agentdesk/sessions/`:
   - `YYYY-MM-DD.snapshot.json`: `engine.snapshot()` (what the dashboard loads on connect), written every 60 s
     and once more at shutdown after positions are sold. Written to a temp file and renamed, so a crash never
     leaves half a file.
   - `YYYY-MM-DD.events.jsonl`: the dashboard's feed events (orders, fills, skips, crew lines, logs, proposals,
     book events; no ticks or bars), appended as they happen. It gives the Activity tab its history.
   This is written from the `run` command (`__main__.py` / `lifecycle.py`); `engine.py` and the books are not
   touched, so no trading behaviour changes.
2. **`python -m agentdesk review`** serves `web/` on the same host and port with a read-only API: `/api/state`
   (the saved snapshot, so the book F tab works unchanged), `/api/review` (snapshot + events + the list of
   saved days). Every non-GET request is refused, and the same host/origin checks as the engine apply.
   `app.js` gets a `review` source: it applies the saved events in bulk to build the Activity feed, then loads
   the snapshot, and hides the controls.
3. **Handing the port back and forth.** A new LaunchAgent `com.agentdesk.review` (KeepAlive) runs the review
   server all the time, but it only listens while nobody has claimed the dashboard. A claim is a file in
   `~/.agentdesk/claims/` named after a process id:
   - `paper_session.sh` claims at start (its own pid, removed on exit), then waits up to 20 s for port 8765 to
     free before its existing "port in use" check.
   - `python -m agentdesk run` (any mode, including a manual sim run) also claims before it binds and waits up
     to 15 s for the port; the claim is removed when it exits.
   - The review server checks every 2 s: while a claim names a live process it stops listening; claims of dead
     processes are deleted, so a crash or reboot never leaves the review server locked out.
4. **Install.** `tools/install_paper.sh` also installs `com.agentdesk.review` from the same pinned deploy and
   venv. Both jobs are verified loaded after `launchctl bootstrap` (retried up to 3 times, then the script
   fails loudly), because the recorder's bootstrap has flaked before.

## Limits

- If the engine restarts mid-day, the snapshot is the latest process's view. Today that already means trades
  closed before the restart drop off the live dashboard's table; the review page shows the same thing. The
  Activity feed keeps everything.
- Sessions are kept (a few MB a day at most); no pruning for now.
- The review page doesn't replay the day bar by bar. The saved events would allow that later.

## Plan (TDD, one commit per step)

1. `agentdesk/archive.py`: `SessionArchive` (snapshot writer with atomic rename, events file, feed-event filter)
   and `list_days`/`load_day`. Tests: atomic write, filter, load of latest and named day, empty dir.
2. Hook it into `cmd_run`/`lifecycle.serve`: periodic snapshot task, bus recorder, final snapshot after the
   shutdown flatten; off in sim. Tests with a fake engine through `lifecycle.serve`.
3. `agentdesk/claims.py`: claim/release, `claimed()` with stale-pid cleanup, `wait_port_free`. Tests.
4. `agentdesk/review.py`: `create_review_app` (GET-only, host guard, `/api/state`, `/api/review`, `/config.js`
   with `source: 'review'`) and the supervisor that starts/stops uvicorn on claims. Tests with FastAPI
   TestClient and a fake clock/claims.
5. `run` claims the port; `review` CLI command. Tests.
6. `app.js` review source (bulk-apply events, load snapshot, REVIEW pill, banner, hide controls, day picker);
   rebuild the demo.
7. `paper_session.sh` claim + wait; `com.agentdesk.review.plist`; `install_paper.sh` installs it and verifies
   both jobs loaded with retries. `bash -n`/`zsh -n` and `plutil`-free checks in CI-able tests.
8. CLAUDE.md, README and HANDOFF notes; full test run.
