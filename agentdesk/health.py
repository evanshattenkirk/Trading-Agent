"""The dashboard's Manager panel (Evan, 2026-10-27: "add a manager panel for me to check on that stuff", after a
Robinhood disconnect). Spec: docs/superpowers/specs/2026-10-27-manager-panel-design.md.

Each check returns one row: {name, state, detail, fix}. `state` is "ok", "warn" (something to do soon), "bad" (broken
now) or "idle" (nothing to judge: the market is closed or the engine isn't running). `fix` says what to run when the
row isn't green. Everything here reads local files only (the two token files, the recorder's heartbeat and day log,
journal.rh_calls, the engine's snapshot) plus `launchctl print` on the Mac: no Robinhood, Alpaca or Claude calls, so
the after-hours review server can serve it too. A row never carries a token, the account number or the balance.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time as _time
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

from .brokers.robinhood import ENGINE_SIGN_IN_CMD, RECORDER_SIGN_IN_CMD, FileTokenStorage
from .clock import CT, ct, session_date
from .config import expand

OK, WARN, BAD, IDLE = "ok", "warn", "bad", "idle"
RANK = {IDLE: 0, OK: 1, WARN: 2, BAD: 3}
DAY = 86400.0
WARN_LEFT_S = 3 * DAY           # amber with under 3 days left on a sign-in
BAD_LEFT_S = DAY                # red under a day: the 08:10 connect should have renewed it (REFRESH_AHEAD_S)
CALL_WINDOW_S = 900             # error counts cover the last 15 minutes
SILENT_S = 120                  # no good Robinhood call for this long in market hours is red
PRINT_STALE_S = 60              # the Alpaca feed warns after a minute without SPY prints (feeds/alpaca.py)
RECORDER_STALE_S = 120          # engine.RECORDER_STALE_SEC: the recorder writes every 10 s in market hours
JOBS = ("com.agentdesk.paper", "com.agentdesk.review", "com.agentdesk.quant", "com.agentdesk.recorder")
JOB_LOGS = {"paper": "paper", "review": "review", "quant": "reports", "recorder": "recorder"}
RECORDER_LOG = "~/.agentdesk/recorder/recorder.log"
LATER = "outside market hours"
NOW = "now"
RESTART_RECORDER = "launchctl kickstart -k gui/$(id -u)/com.agentdesk.recorder"
ENGINE_LOG = "tail -50 ~/.agentdesk/paper/$(date +%F).log"


@dataclass
class Paths:
    engine_token: Path
    recorder_token: Path
    heartbeat: Path
    days_log: Path
    journal: Path

    @classmethod
    def from_cfg(cls, cfg) -> "Paths":
        from . import recorder
        rec = recorder.settings(cfg)
        days = (cfg.get("ops") or {}).get("recorder_days_log")
        return cls(engine_token=Path(expand(cfg["robinhood"]["token_dir"])) / "rh_oauth.json",
                   recorder_token=Path(expand(rec["token_dir"])) / "rh_oauth.json",
                   heartbeat=recorder.HEARTBEAT, days_log=Path(expand(days)) if days else recorder.DAYS_LOG,
                   journal=Path(expand(cfg["journal_path"])))


def row(name: str, state: str, detail: str, fix: str = "", cmd: str = "") -> dict:
    """`fix` is what to do in words; `cmd` the exact Terminal command, shown with a Copy button."""
    return {"name": name, "state": state, "detail": detail, "fix": fix, "cmd": cmd}


# ---------------------------------------------------------------- time helpers
def trading_day(now: float, cfg) -> bool:
    cal = cfg.get("calendar") or {}
    return ct(now).weekday() < 5 and str(session_date(now)) not in {str(d) for d in cal.get("holidays") or []}


def session_open(now: float, cfg) -> bool:
    """Market hours as the REC pill counts them: a trading day from 08:32 CT (two minutes for the first quotes) to
    15:00, or 12:00 on an early close."""
    if not trading_day(now, cfg):
        return False
    cal = cfg.get("calendar") or {}
    close = time(12, 0) if str(session_date(now)) in {str(d) for d in cal.get("early_close") or []} else time(15, 0)
    return time(8, 32) <= ct(now).time() < close


def when(ts: float) -> str:
    return datetime.fromtimestamp(ts, CT).strftime("%a %b %d %H:%M CT")


def ago(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 120:
        return f"{sec} s"
    if sec < 2 * 3600:
        return f"{sec // 60} min"
    if sec < 2 * DAY:
        return f"{sec // 3600} h"
    return f"{int(sec // DAY)} days"


def left(sec: float) -> str:
    sec = max(0, int(sec))
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m"


# ---------------------------------------------------------------- Robinhood sign-ins
def sign_in(name: str, token_file: Path, now: float, cmd: str, live=None) -> dict:
    """A saved Robinhood sign-in: its deadline from the token file (saved_at + expires_in, never the token itself),
    and the engine's own verdict (`live`: RobinhoodMCP.sign_in_status()) when it says red, since Robinhood may
    have refused a token whose file still looks valid."""
    if live is not None and not live[0]:
        return row(name, BAD, str(live[1]), f"Sign in again: run this in Terminal {NOW}.", cmd)
    if not Path(token_file).exists():
        return row(name, BAD, "no saved sign-in", f"Sign in: run this in Terminal {NOW}.", cmd)
    exp = FileTokenStorage(Path(token_file)).expires_at()
    if exp is None:
        return row(name, WARN, "signed in, but the token gives no lifetime",
                   f"To be sure, sign in again {LATER}.", cmd)
    rest = exp - now
    if rest <= 0:
        return row(name, BAD, f"expired {when(exp)}", f"Sign in again: run this in Terminal {NOW}.", cmd)
    detail = f"signed in until {when(exp)} ({left(rest)} left)"
    if rest < BAD_LEFT_S:
        return row(name, BAD, detail + "; it should have renewed by now",
                   f"Sign in again today, {LATER}: run this in Terminal.", cmd)
    if rest < WARN_LEFT_S:
        return row(name, WARN, detail, f"Sign in again before then, {LATER}: run this in Terminal.", cmd)
    return row(name, OK, detail)


# ---------------------------------------------------------------- recorder
def _last_day(days_log: Path) -> dict | None:
    last = None
    try:
        for line in Path(days_log).read_text().splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict) and d.get("day") and "rows" in d:
                last = d
    except OSError:
        pass
    return last


def recorder_writing(heartbeat: Path, days_log: Path, now: float, cfg) -> dict:
    """The standalone recorder touches its heartbeat on every quote write (every 10 s in market hours)."""
    name = "Recorder writing quotes"
    try:
        age = now - Path(heartbeat).stat().st_mtime
    except OSError:
        age = None
    last = _last_day(days_log)
    tail = f"; last finished day {last['day']}: {int(last['rows']):,} quote rows" if last else ""
    if age is not None and age < RECORDER_STALE_S:
        return row(name, OK, f"last quotes {ago(age)} ago{tail}")
    seen = "no quotes written yet" if age is None else f"last quotes {ago(age)} ago"
    if not session_open(now, cfg):
        return row(name, IDLE, f"market closed; {seen}{tail}")
    return row(name, BAD, ("no quotes written yet" if age is None else f"no quotes for {ago(age)}") + tail,
               f"Check {RECORDER_LOG}. If it asks for a sign-in, use the recorder sign-in row's command; "
               "otherwise restart the recorder:", RESTART_RECORDER)


def _counts(kinds: dict[str, int]) -> str:
    parts = [f"{kinds.get('ok', 0)} OK"]
    for k, label in (("throttled", "rate-limited"), ("rate_limited", "rate-limited"), ("timeout", "timed out"),
                     ("auth", "refused sign-in"), ("server", "server errors"), ("other", "other errors"),
                     ("error", "other errors")):
        if kinds.get(k):
            parts.append(f"{kinds[k]} {label}")
    return ", ".join(parts)


def recorder_calls(journal: Path, now: float, cfg) -> dict:
    """The recorder's and the IV pass's Robinhood calls (journal.rh_calls, tags recorder and iv), last 15 min."""
    name = "Recorder Robinhood calls"
    rows: list[tuple] = []
    if Path(journal).exists():
        try:
            db = sqlite3.connect(f"file:{journal}?mode=ro", uri=True, timeout=5)
            try:
                rows = db.execute("SELECT ts, ok, kind FROM rh_calls WHERE tag IN ('recorder','iv') AND ts>=? AND ts<=?",
                                  (now - CALL_WINDOW_S, now + 60)).fetchall()
            finally:
                db.close()
        except sqlite3.Error:
            rows = []
    kinds: dict[str, int] = {}
    for _, ok, kind in rows:
        k = "ok" if ok else (kind or "other")
        kinds[k] = kinds.get(k, 0) + 1
    good = [ts for ts, ok, _ in rows if ok]
    if not session_open(now, cfg):
        return row(name, IDLE, f"market closed; {len(rows)} calls in the last 15 min" if rows else "market closed")
    detail = f"{len(rows)} calls in the last 15 min: {_counts(kinds)}" if rows else "no calls in the last 15 min"
    if kinds.get("auth"):
        return row(name, BAD, detail, f"Robinhood refused the recorder's sign-in. Run this in Terminal {NOW}:",
                   RECORDER_SIGN_IN_CMD)
    if not good or now - max(good) > SILENT_S:
        quiet = "no good call in the last 15 min" if not good else f"no good call for {ago(now - max(good))}"
        return row(name, BAD, f"{quiet}; {detail}" if rows else detail,
                   f"Check {RECORDER_LOG}, then restart the recorder:", RESTART_RECORDER)
    if len(kinds) > 1:
        return row(name, WARN, detail, "Usually passes by itself; the engine and recorder share ~240 calls/min.")
    return row(name, OK, detail)


# ---------------------------------------------------------------- engine
def engine_calls(stats: dict | None, now: float, cfg, running: bool) -> dict:
    """The engine's Robinhood calls (RobinhoodMCP.stats(), carried in the snapshot)."""
    name = "Engine Robinhood calls"
    stats = stats or {}
    last_ok = stats.get("last_ok")
    if not running:
        return row(name, IDLE, "engine not running" + (f"; last good call {when(last_ok)}" if last_ok else ""))
    w, d = stats.get("window") or {}, stats.get("day") or {}
    parts = [f"last good call {ago(now - last_ok)} ago" if last_ok else "no good call yet",
             f"{_counts(w)} in the last 15 min"]
    if d.get("reconnects"):
        parts.append(f"{d['reconnects']} reconnects today")
    detail = "; ".join(parts)
    if w.get("auth"):
        return row(name, BAD, detail, f"Robinhood refused the engine's sign-in. Run this in Terminal {NOW}, then "
                                      "restart the engine:", ENGINE_SIGN_IN_CMD)
    if session_open(now, cfg) and (not last_ok or now - last_ok > SILENT_S):
        return row(name, BAD, detail, "Look for Robinhood errors in today's engine log:", ENGINE_LOG)
    if w.get("rate_limited") or w.get("timeout") or w.get("error"):
        return row(name, WARN, detail, "Usually passes by itself; calls are paused and retried. Many in a row halt "
                                       "the books (see Halts).")
    return row(name, OK, detail)


def feed(name_: str | None, last_print: float | None, now: float, cfg, running: bool) -> dict:
    name = "SPY prints"
    if not running:
        return row(name, IDLE, "engine not running")
    src = name_ or "feed"
    seen = f"last print {ago(now - last_print)} ago" if last_print else "no print yet"
    if not session_open(now, cfg):
        return row(name, IDLE, f"{src}: market closed; {seen}")
    if last_print and now - last_print < PRINT_STALE_S:
        return row(name, OK, f"{src}: {seen}")
    return row(name, BAD, f"{src}: " + (f"no SPY prints for {ago(now - last_print)}" if last_print else seen),
               "The Alpaca socket reconnects by itself; if it doesn't, check today's engine log:", ENGINE_LOG)


def engine_row(running: bool, snap: dict | None, now: float, cfg) -> dict:
    name = "Engine"
    snap = snap or {}
    if running:
        return row(name, OK, f"running in {snap.get('mode', '?')} mode")
    last = f"; last session {snap['day']}" if snap.get("day") else ""
    if trading_day(now, cfg) and time(8, 15) <= ct(now).time() < time(15, 0):
        return row(name, BAD, "not running during the session" + last,
                   "Start it with this; why it stopped is in ~/.agentdesk/paper/<date>.log and "
                   "~/.agentdesk/paper/launchd.err.log.", "launchctl kickstart gui/$(id -u)/com.agentdesk.paper")
    return row(name, IDLE, "not running; it starts at 08:10 CT on trading days" + last)


def halts(risk: dict | None, books: dict | None, running: bool) -> dict:
    name = "Halts"
    risk, books = risk or {}, books or {}
    acct, single = [], []
    if risk.get("halted"):
        (single if risk.get("halt_scope") == "A" else acct).append(f"A: {risk.get('halt_reason')}"
                                                                   if risk.get("halt_scope") == "A"
                                                                   else str(risk.get("halt_reason")))
    account = books.get("account") or {}
    if account.get("halted"):
        acct.append(str(account.get("halt_reason")))
    for b in books.get("books") or []:
        if b.get("book") != "A" and b.get("halted"):
            single.append(f"{b['book']}: {b.get('halt_reason')}")
    acct = list(dict.fromkeys(acct))
    parts = [f"all books: {r}" for r in acct] + single
    if not running:
        return row(name, IDLE, "last session: " + ("; ".join(parts) if parts else "no halts"))
    if acct:
        return row(name, BAD, "; ".join(parts), "Read the reason. A halt from a Robinhood sign-in clears with a "
                                                 "restart after signing in; others: run --clear-halt only once "
                                                 "the cause is fixed.")
    if single:
        return row(name, WARN, "; ".join(parts), "A book's own halt lasts the day; the other books keep trading.")
    return row(name, OK, "no halts")


# ---------------------------------------------------------------- launchd
_jobs_cache: tuple[float, dict] | None = None


def jobs(runner=None, platform: str | None = None) -> dict:
    """The four LaunchAgents: loaded, and their last run exited cleanly. Cached 60 s when using launchctl itself."""
    global _jobs_cache
    name = "Mac jobs"
    if (platform or sys.platform) != "darwin":
        return row(name, IDLE, "launchd jobs are checked on the Mac only")
    cached = runner is None
    if cached and _jobs_cache and _time.monotonic() - _jobs_cache[0] < 60:
        return _jobs_cache[1]
    run = runner or subprocess.run
    missing, failed = [], []
    for job in JOBS:
        short = job.rsplit(".", 1)[-1]
        try:
            r = run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{job}"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            missing.append(short)
            continue
        if r.returncode != 0:
            missing.append(short)
            continue
        m = re.search(r"last exit code = (-?\d+)", r.stdout or "")
        if m and int(m.group(1)) != 0:
            failed.append((short, int(m.group(1))))
    if not missing and not failed:
        out = row(name, OK, "paper, review, quant and recorder loaded; last runs exited cleanly")
    else:
        detail = "; ".join([f"{j} not loaded" for j in missing] + [f"{j} last exit {c}" for j, c in failed])
        steps = (["tools/install_recorder.sh"] if "recorder" in missing else []) \
            + (["tools/install_paper.sh"] if any(j != "recorder" for j in missing) else [])
        if steps:
            fix, cmd = ("Reinstall the missing jobs (install_paper.sh only outside 08:10-15:10 CT):",
                        "cd ~/Trading-Agent && " + " && ".join(steps))
        else:
            j = failed[0][0]
            fix, cmd = "See why the last run failed:", f"tail -30 ~/.agentdesk/{JOB_LOGS[j]}/launchd.err.log"
        out = row(name, BAD, detail, fix, cmd)
    if cached:
        _jobs_cache = (_time.monotonic(), out)
    return out


# ---------------------------------------------------------------- the report
def worst(rows: list[dict]) -> str:
    return max((r["state"] for r in rows), key=RANK.__getitem__, default=IDLE)


def report(cfg, now: float, snap: dict | None, running: bool, paths: Paths | None = None, runner=None,
           platform: str | None = None) -> dict:
    """Every check, from the engine's live snapshot (`running`) or the last saved one (the review page)."""
    paths = paths or Paths.from_cfg(cfg)
    snap = snap or {}
    h = snap.get("health") or {}
    live = h.get("rh_sign_in") if running else None
    checks = [
        sign_in("Engine Robinhood sign-in", paths.engine_token, now, ENGINE_SIGN_IN_CMD, live=live),
        sign_in("Recorder Robinhood sign-in", paths.recorder_token, now, RECORDER_SIGN_IN_CMD),
        engine_calls(h.get("rh"), now, cfg, running),
        recorder_writing(paths.heartbeat, paths.days_log, now, cfg),
        recorder_calls(paths.journal, now, cfg),
        feed(h.get("feed"), h.get("last_print"), now, cfg, running),
        engine_row(running, snap, now, cfg),
        halts(snap.get("risk"), snap.get("books"), running),
        jobs(runner, platform),
    ]
    return {"ts": now, "state": worst(checks), "running": running, "checks": checks}
