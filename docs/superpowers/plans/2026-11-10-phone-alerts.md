# Phone alerts (iMessage) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Mac LaunchAgent that checks the Manager panel every 5 minutes and texts Evan (iMessage + Mac banner) when a row turns amber or red, repeats red hourly in the day, says when it's fixed, and sends a morning all-clear.

**Architecture:** One new module `agentdesk/alerts.py`: a pure `diff()` that turns (last state sent, current `/api/health` report, time) into message texts and the new state; a `check()` that fetches the report (or builds one from local files when port 8765 is silent), runs `diff`, sends, retries and saves state; and `send()` around `osascript`. A new CLI command `alerts` runs one check; launchd runs it every 300 s from the paper app's release.

**Tech Stack:** Python 3.12 stdlib (`urllib.request`, `subprocess`, `json`), existing `agentdesk.health`, `osascript` on macOS, launchd, bash installer.

**Spec:** `docs/superpowers/specs/2026-11-10-phone-alerts-design.md`

## Global Constraints

- A message never carries a token, account number or balance: only each row's `name`, `detail`, `fix`, `cmd`.
- The recipient comes only from `ALERT_IMESSAGE_TO` in the environment (the Mac's `.env`). It never appears in code, tests (use `+15550000000`), docs, commits or logs; logs show only its last 4 characters (`…0000`).
- Hours are Central time (`agentdesk.clock.ct`). Day window 07:00 ≤ t < 21:00 CT.
- Red repeats every 60 min, day window only. First red: any hour.
- Unreachable port: Dashboard row only after 2 failed checks in a row.
- Morning all-clear at/after 08:20 CT, once per trading day (`health.trading_day`), only inside the day window.
- No new dependencies. Paper/engine code untouched (`engine.py`, books).
- Config keys under `alerts:`: `enabled: true`, `day_start: "07:00"`, `day_end: "21:00"`, `repeat_bad_min: 60`, `morning: true`, `morning_at: "08:20"`, `state_dir: ~/.agentdesk/alerts`. (The spec's `interval_sec` is dropped: the plist's `StartInterval` 300 is the one source.)
- Failed iMessage: kept in `pending`, retried on later checks, dropped after 3 tries total with a log line. The Mac banner is shown on the first try only.

## Review Focus

1. A row that disappears from the report (the Dashboard row once 8765 answers again, or a check renamed) must count as green, so it gets its ✅ and its state is pruned. Test in Task 1.
2. A corrupt or missing `state.json` must not crash the job or replay old alerts; it starts fresh (and sends the hello). Test in Task 3.
3. `/api/health` answering with non-JSON or JSON without `checks` must count as unreachable, not crash. Test in Task 3.
4. A row that goes red overnight, then green before 07:00, must still send its ✅ at 07:00 (red was sent, so Evan needs the all-clear). Test in Task 1.
5. Text with quotes, newlines or emoji must reach `osascript` intact as an argument, never inside the script source. Test in Task 2.

---

### Task 1: `diff()` — the alert rules

**Files:**
- Create: `agentdesk/alerts.py`
- Modify: `config.yaml` (add the `alerts:` section after `review:`)
- Test: `tests/test_alerts.py`

**Interfaces:**
- Consumes: `agentdesk.health` constants `OK, WARN, BAD, IDLE`, `health.row(name, state, detail, fix="", cmd="")`, `health.trading_day(now, cfg)`, `agentdesk.clock.ct(ts)`.
- Produces:
  - `settings(cfg) -> dict` — the `alerts:` section merged over the defaults in Global Constraints (cfg without `alerts` works).
  - `daytime(now: float, cfg) -> bool`
  - `diff(prev: dict, report: dict, now: float, cfg) -> tuple[list[str], dict]` — `prev`/returned state shape: `{"rows": {name: {"sent": "warn"|"bad", "at": float}}, "morning": "YYYY-MM-DD"|None, ...}`; keys other than `rows`/`morning` pass through unchanged. Returns 0–2 texts: at most one transition message and at most one morning message.

Rules (per row, keyed by `name`; a row missing from the report counts as `ok`; `sent` absent means nothing outstanding):
- current `bad`: send if `sent != "bad"`; else send a repeat if `now - at >= repeat_bad_min*60` and daytime. Sets `sent="bad", at=now`.
- current `warn`: if `sent != "warn"` and daytime, send; sets `sent="warn", at=now`. (Overnight: nothing changes, so it goes at 07:00 if still amber.)
- current `ok`/`idle`: if `sent` is set and daytime, send ✅ and drop the row's entry. Otherwise keep the entry (held).
- A changed `detail` with the same state never sends.

Message format (one text, sections in order red, amber, fixed; blank line between rows):
```
AgentDesk
🔴 {name}: {detail}
{fix}
{cmd}
```
Repeats prefix the name with `still red: `. Amber uses 🟠. Fixed: `✅ {name}: fixed ({detail})`. Omit empty `fix`/`cmd` lines.

Morning (if `morning` on, trading day, daytime, ct time ≥ `morning_at`, `prev["morning"] != today`): all rows ok/idle → `AgentDesk {HH:MM}: all green; {Engine row detail}`; otherwise `AgentDesk {HH:MM}: not all green: ` + `", ".join(f"{name} ({state})")` for non-green rows. Sets `morning` to today's `session_date` string.

- [ ] **Step 1: Write the failing tests** in `tests/test_alerts.py`, using times built with `at_ct(date(2026, 11, 10), time(...))` (a Tuesday) and `REP(rows)` = `{"checks": rows, "state": health.worst(rows)}`, `cfg` = `load_config()` (conftest sets `AGENTDESK_NO_DOTENV`):
  - `test_first_red_goes_at_any_hour` — red at 03:00 from empty state → 1 text containing "🔴 Engine Robinhood sign-in" and the row's `cmd`; state `sent == "bad"`.
  - `test_red_repeats_hourly_only_in_the_day` — sent red at 10:00; same red at 10:59 → no text; at 11:00 → text containing "still red"; sent red at 02:00, red at 03:30 → no text.
  - `test_amber_waits_for_morning` — amber at 22:00 → no text, state unchanged; same amber at 07:00 → one 🟠 text; amber at 22:00 then ok at 06:00 → no text ever (entry never set).
  - `test_red_then_green_overnight_sends_fixed_at_seven` — red at 02:00 (sent), ok at 05:00 → none; ok at 07:00 → text containing "✅" and "fixed"; entry removed.
  - `test_red_to_amber_is_sent` — sent red, now amber at 10:00 → 🟠 text.
  - `test_wording_change_sends_nothing` — sent amber "2d 20h left", now amber "2d 19h left" → no text.
  - `test_idle_rows_never_alert` — idle rows from empty state at 10:00 → no text.
  - `test_missing_row_counts_as_green` — sent red for "Dashboard", report lacks it at 10:00 → ✅ text; "Dashboard" not in state.
  - `test_one_message_worst_first` — one red and one amber newly at 10:00 → exactly 1 transition text and `text.index("🔴") < text.index("🟠")`.
  - `test_morning_all_clear_once_on_trading_days` — all ok at 08:25 Tue → text starting "AgentDesk 08:25: all green"; second call with returned state → none; Sat 2026-11-14 08:25 → none; Thanksgiving 2026-11-26 08:25 → none; 08:10 → none; `alerts.morning: false` → none.
  - `test_morning_lists_what_is_not_green` — one warn row at 08:25 → morning text contains "not all green" and "(warn)".
  - `test_no_secret_reaches_a_message` — a row whose detail is normal but whose dict also carries an extra key `"token": "eyJ-secret"` → no text contains "eyJ-secret".

- [ ] **Step 2: Run** `python -m pytest -q tests/test_alerts.py` — expected: FAIL (`ModuleNotFoundError: agentdesk.alerts`).

- [ ] **Step 3: Implement** `settings`, `daytime`, `diff` in `agentdesk/alerts.py` (module docstring cites the spec and Evan's 2026-11-09/10 choices) and add the `alerts:` block to `config.yaml` with the Global Constraints values and a one-line comment.

- [ ] **Step 4: Run** `python -m pytest -q tests/test_alerts.py` — expected: all pass.

- [ ] **Step 5: Commit** `git add agentdesk/alerts.py config.yaml tests/test_alerts.py && git commit -m "alerts: the rules for when a Manager panel row texts Evan"`

---

### Task 2: `send()` — iMessage and banner through osascript

**Files:**
- Modify: `agentdesk/alerts.py`
- Test: `tests/test_alerts.py`

**Interfaces:**
- Produces:
  - `IMESSAGE_SCRIPT: str` (AppleScript, `on run argv` … `send (item 2 of argv) to participant (item 1 of argv) of (1st account whose service type = iMessage)` inside `tell application "Messages"`).
  - `BANNER_SCRIPT: str` (`on run argv` … `display notification (item 1 of argv) with title "AgentDesk"`).
  - `send(text: str, to: str | None, banner: bool = True, runner=subprocess.run) -> bool` — runs the banner (if `banner`) then, if `to`, `["/usr/bin/osascript", "-e", IMESSAGE_SCRIPT, to, text]` with `timeout=30, capture_output=True, text=True`; returns True only when the iMessage ran with return code 0. Any exception from `runner` → False. No `to` → returns False after the banner.
  - `mask(to: str | None) -> str` — `"…" + to[-4:]`, or `"(none)"`.

- [ ] **Step 1: Write the failing tests** with a fake runner that records `args` lists and returns `SimpleNamespace(returncode=rc, stdout="", stderr="")`:
  - `test_send_passes_handle_and_text_as_arguments` — text `'He said "hi"\nline 2 🔴'`, to `"+15550000000"`; the iMessage call's argv is `["/usr/bin/osascript", "-e", alerts.IMESSAGE_SCRIPT, "+15550000000", text]`; neither the handle nor the text appears in `IMESSAGE_SCRIPT` or `BANNER_SCRIPT`; returns True.
  - `test_send_failure_returns_false` — iMessage call rc 1 → False; runner raising `subprocess.TimeoutExpired` → False.
  - `test_no_recipient_shows_banner_only` — `to=None` → exactly one call (the banner) and False.
  - `test_mask_shows_last_four` — `mask("+15550000000") == "…0000"`, `mask(None) == "(none)"`.

- [ ] **Step 2: Run** `python -m pytest -q tests/test_alerts.py -k send or mask` — expected: FAIL (`AttributeError`).

- [ ] **Step 3: Implement** the two scripts, `send` and `mask`.

- [ ] **Step 4: Run** the same command — expected: pass.

- [ ] **Step 5: Commit** `git commit -am "alerts: send iMessages and banners with the text as an osascript argument"`

---

### Task 3: `check()` — fetch, fallback, retries, state file, hello

**Files:**
- Modify: `agentdesk/alerts.py`
- Test: `tests/test_alerts.py`

**Interfaces:**
- Consumes: `diff`, `send`, `settings`, `health.report(cfg, now, None, running=False)`, `health.row`, `health.BAD`.
- Produces:
  - `fetch(url: str, timeout: float = 10) -> dict | None` — GET with `urllib.request`; returns the parsed dict only if it is a dict with a list under `checks`; anything else (connection error, timeout, bad JSON, wrong shape) → None.
  - `DASHBOARD_ROW` builder: `health.row("Dashboard", health.BAD, "neither the engine nor the review page answers on :8765", "Restart the review page; if the engine should be running, check today's engine log:", "launchctl kickstart -k gui/$(id -u)/com.agentdesk.review")`.
  - `HELLO = "AgentDesk alerts are on. You'll get a text here when the Manager panel turns amber or red."`
  - `check(cfg, now: float, *, fetcher=fetch, local=None, sender=send, to: str | None = None, state_path: Path | None = None, log=logging.getLogger("agentdesk.alerts")) -> list[str]` — returns the texts it tried to send this run (for tests and the log). Steps:
    1. Load state from `state_path` (default `settings(cfg)["state_dir"]/state.json`, expanded); missing or unreadable → `{}` and log once.
    2. If state has no `"hello"` key: queue `HELLO`, set `hello = now`.
    3. `rep = fetcher(f"http://127.0.0.1:{cfg['server']['port']}/api/health")`; on None: `unreachable += 1`, `rep = (local or health.report)(cfg, now, None, running=False)`, and append the Dashboard row to `rep["checks"]` when `unreachable >= 2`. On success: `unreachable = 0`.
    4. `texts, state = diff(state, rep, now, cfg)`.
    5. Retry each `pending` item first (`sender(text, to, banner=False)`); on failure `tries += 1`, drop and log at 3. Then each new text with `sender(text, to)`; on failure append `{"text", "tries": 1}` to `pending`.
    6. No `to`: log one warning per `session_date` ("ALERT_IMESSAGE_TO is not set in .env; Mac banners only"), tracked in state `no_to`; do not queue to `pending` (nothing to retry).
    7. Write state atomically (`tmp` + `os.replace`), creating the directory.
    If `settings(cfg)["enabled"]` is false: log and return `[]` without fetching.

- [ ] **Step 1: Write the failing tests** (state in `tmp_path`, fake fetcher/sender/local):
  - `test_first_run_sends_hello_once` — empty state, all-ok report → texts == [HELLO]; second run → [].
  - `test_corrupt_state_starts_fresh` — `state.json` contains `"{not json"` → no exception; HELLO sent.
  - `test_silent_port_waits_two_checks_then_alerts` — fetcher returns None; local returns an all-idle report; run 1 → no "Dashboard" text; run 2 → a text containing "🔴 Dashboard"; then fetcher returns an ok report at 10:20 → a "✅ Dashboard" text.
  - `test_bad_health_json_counts_as_silent` — call `fetch` against a stub `http.server` on a free port answering `b"<html>"` and then `b'{"x":1}'` → both None.
  - `test_failed_imessage_is_retried_then_dropped` — sender always False with a `to` set → after the first run `pending` has 1 item with `tries == 1`; after 3 more runs it is gone and the log recorded a drop; retries call the sender with `banner=False`.
  - `test_no_recipient_warns_once_a_day_and_queues_nothing` — `to=None`, two runs same day → one warning in `caplog`, `pending` empty.
  - `test_disabled_does_nothing` — `alerts.enabled: false` → fetcher not called, returns [].
  - `test_log_never_shows_the_full_number` — run with `to="+15550000000"` and a failing sender; `"+15550000000" not in caplog.text`.

- [ ] **Step 2: Run** `python -m pytest -q tests/test_alerts.py` — expected: the new tests FAIL.

- [ ] **Step 3: Implement** `fetch`, the Dashboard row, `HELLO`, `check`.

- [ ] **Step 4: Run** `python -m pytest -q tests/test_alerts.py` — expected: all pass.

- [ ] **Step 5: Commit** `git commit -am "alerts: one check: fetch or fall back, retry failed texts, keep state"`

---

### Task 4: CLI command `alerts`

**Files:**
- Modify: `agentdesk/__main__.py` (new `cmd_alerts`, parser next to `review`)
- Test: `tests/test_alerts.py`

**Interfaces:**
- Consumes: `alerts.check`, `alerts.send`, `alerts.HELLO`, `alerts.mask`, `alerts.settings`.
- Produces: `python -m agentdesk alerts [--test]`. Logging goes to `state_dir/alerts.log` (append, plus stderr). Recipient: `os.environ.get("ALERT_IMESSAGE_TO") or None` after `load_config`. `--test` sends `HELLO` directly and exits 0 if the iMessage went, 1 otherwise, printing `sent to …0000` or why not. Without `--test`: one `check(cfg, time.time(), to=...)`; exits 0 even when sends fail (launchd must not count it as a failing job; failures are in the log and `pending`).

- [ ] **Step 1: Write the failing test** `test_cli_alerts_runs_one_check` — monkeypatch `agentdesk.alerts.check` to record its `to` kwarg, set env `ALERT_IMESSAGE_TO=+15550000000`, `alerts.state_dir` to `tmp_path` via a temp config copy, run `agentdesk.__main__.main()` with `sys.argv = ["agentdesk", "--config", str(cfg_path), "alerts"]` → recorded `to == "+15550000000"`; `test_cli_alerts_test_exit_code` — patched `send` returning False → `SystemExit` code 1.

- [ ] **Step 2: Run** `python -m pytest -q tests/test_alerts.py -k cli` — FAIL (`invalid choice: 'alerts'`).

- [ ] **Step 3: Implement** `cmd_alerts` and the subparser (`help="text Evan when the Manager panel turns amber/red (launchd runs this every 5 min)"`).

- [ ] **Step 4: Run** the same command — pass.

- [ ] **Step 5: Commit** `git commit -am "alerts: python -m agentdesk alerts [--test]"`

---

### Task 5: LaunchAgent, installer, Manager panel job row, docs

**Files:**
- Create: `tools/launchd/com.agentdesk.watch.plist`
- Modify: `tools/install_paper.sh`, `agentdesk/health.py` (`JOBS`, the green detail text), `CLAUDE.md`, `README.md` (Mac jobs list), the spec's install paragraph and `interval_sec` line
- Test: `tests/test_review_launchd.py` (or new `tests/test_watch_launchd.py`), `tests/test_health.py`

**Interfaces:**
- Plist: `Label` `com.agentdesk.watch`; `ProgramArguments` `["__APP__/.venv/bin/python", "-m", "agentdesk", "--config", "__APP__/src/config.yaml", "alerts"]`; `WorkingDirectory` `__APP__/src`; `StartInterval` 300; `RunAtLoad` true; no KeepAlive; `ProcessType` Background; logs `__HOME__/.agentdesk/alerts/launchd.{out,err}.log`.
- Installer: `WATCH="com.agentdesk.watch"`, `WATCH_PLIST`; `mkdir -p "$HOME/.agentdesk/alerts"`; `write_plist` it with the others; before loading, delete the `hello` key so each install re-sends the hello (`"$APP/.venv/bin/python" -c` one-liner that loads `state.json` if present, pops `hello`, writes it back); `load_job "$WATCH" "$WATCH_PLIST"   # WATCH` (not skipped when an engine runs: it never touches the engine); then print: "Loaded com.agentdesk.watch: checks the Manager panel every 5 min and texts ALERT_IMESSAGE_TO from .env. If macOS asks whether osascript may control Messages, click OK. Then check your phone for 'AgentDesk alerts are on'." and, when `.env` has no `ALERT_IMESSAGE_TO=` line, "WARNING: no ALERT_IMESSAGE_TO in .env; Mac banners only."
- `health.JOBS` gains `"com.agentdesk.watch"`, `JOB_LOGS["watch"] = "alerts"`; the green detail becomes `"paper, review, quant, recorder and watch loaded; last runs exited cleanly"`.

- [ ] **Step 1: Write the failing tests**:
  - `test_watch_plist_runs_alerts_every_five_minutes` — parses the plist: Label, ProgramArguments as above, `StartInterval == 300`, `RunAtLoad is True`, `"KeepAlive" not in pl`, `StandardErrorPath.startswith("__HOME__/.agentdesk/alerts/")`.
  - `test_install_paper_loads_the_watch_job_and_resends_hello` — `install_paper.sh` passes `bash -n`, contains `com.agentdesk.watch`, `re.search(r"load_job .*WATCH", s)`, `.agentdesk/alerts`, `hello`, and `ALERT_IMESSAGE_TO`; the WATCH load line is outside the `if [ -n "$RUNNING" ]` block (its index is after that block's `fi`).
  - In `tests/test_health.py`: `test_watch_job_is_checked` — `"com.agentdesk.watch" in health.JOBS`; a launchctl fake missing only `watch` → state `bad`, detail contains "watch not loaded", cmd contains `install_paper.sh`.
  - Existing `tests/test_install.py` box tests must still pass with the new plist (they fake `launchctl`/`plutil`); extend the fake-tools list there only if a test fails on the new commands.

- [ ] **Step 2: Run** `python -m pytest -q tests/test_review_launchd.py tests/test_health.py tests/test_install.py` — expected: the new tests FAIL.

- [ ] **Step 3: Implement** the plist, installer lines, `health.py` changes; update CLAUDE.md (Mac jobs bullet + CLI list + a one-line "Phone alerts" bullet under the Manager panel), README's job list, and the spec's install paragraph (first run after each install sends the hello; no `interval_sec`).

- [ ] **Step 4: Run** the same command — pass. Then the full suite once: `python -m pytest -q tests research/tests` — expected: all pass (note the count in the PR).

- [ ] **Step 5: Commit** `git add -A tools agentdesk/health.py tests CLAUDE.md README.md docs && git commit -m "alerts: com.agentdesk.watch LaunchAgent, installed with the paper jobs"`
