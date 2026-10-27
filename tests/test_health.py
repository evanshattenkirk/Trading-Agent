"""The dashboard's Manager panel (Evan, 2026-10-27: "add a manager panel for me to check on that stuff"). Each check
reads local files only (token files, the recorder heartbeat and day log, journal.rh_calls, launchctl) and returns one
row: ok / warn / bad / idle, a line of detail and, when it isn't green, what to run. Spec:
docs/superpowers/specs/2026-10-27-manager-panel-design.md."""
import json
import os
import sqlite3
import sys
from datetime import date, time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk import health
from agentdesk.brokers.robinhood import ENGINE_SIGN_IN_CMD
from agentdesk.clock import at_ct
from agentdesk.config import load_config

CFG = load_config()
DAY = 86400
OPEN = at_ct(date(2026, 10, 27), time(10, 0))        # a Tuesday, market hours
EVENING = at_ct(date(2026, 10, 27), time(18, 0))
MORNING = at_ct(date(2026, 10, 27), time(8, 0))      # before the open
HOLIDAY = at_ct(date(2026, 11, 26), time(10, 0))     # Thanksgiving, in calendar.holidays
SECRET = "eyJhbGciOi-access-token-value"


def token(path, saved_at, life=9.6 * DAY):
    path.write_text(json.dumps({"tokens": {"access_token": SECRET, "refresh_token": "r-" + SECRET,
                                           "expires_in": life, "token_type": "Bearer"}, "saved_at": saved_at}))
    return path


def touched(path, ts):
    path.touch()
    os.utime(path, (ts, ts))
    return path


# ---------------------------------------------------------------- sign-ins
def test_a_fresh_sign_in_is_green_with_its_deadline(tmp_path):
    r = health.sign_in("Engine Robinhood sign-in", token(tmp_path / "t.json", OPEN - DAY), OPEN, ENGINE_SIGN_IN_CMD)
    assert r["state"] == "ok"
    assert "until" in r["detail"] and "8d" in r["detail"]
    assert not r["fix"] and not r["cmd"]


def test_under_three_days_left_is_amber(tmp_path):
    r = health.sign_in("x", token(tmp_path / "t.json", OPEN - 7.6 * DAY), OPEN, ENGINE_SIGN_IN_CMD)
    assert r["state"] == "warn"
    assert r["cmd"] == ENGINE_SIGN_IN_CMD and "outside market hours" in r["fix"]


def test_under_a_day_left_is_red(tmp_path):
    r = health.sign_in("x", token(tmp_path / "t.json", OPEN - 9.2 * DAY), OPEN, ENGINE_SIGN_IN_CMD)
    assert r["state"] == "bad"
    assert r["cmd"] == ENGINE_SIGN_IN_CMD


def test_an_expired_sign_in_is_red_and_says_so(tmp_path):
    r = health.sign_in("x", token(tmp_path / "t.json", OPEN - 10 * DAY), OPEN, ENGINE_SIGN_IN_CMD)
    assert r["state"] == "bad" and "expired" in r["detail"]
    assert r["cmd"] == ENGINE_SIGN_IN_CMD


def test_no_token_file_is_red(tmp_path):
    r = health.sign_in("x", tmp_path / "missing.json", OPEN, ENGINE_SIGN_IN_CMD)
    assert r["state"] == "bad" and "no saved sign-in" in r["detail"]


def test_a_token_without_saved_at_is_dated_from_its_file(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"tokens": {"access_token": SECRET, "expires_in": 9.6 * DAY}}))
    os.utime(p, (OPEN - DAY, OPEN - DAY))
    assert health.sign_in("x", p, OPEN, ENGINE_SIGN_IN_CMD)["state"] == "ok"


def test_the_engines_own_verdict_wins_when_it_is_red(tmp_path):
    """Robinhood refused the token (or a refresh) though the file still looks valid."""
    p = token(tmp_path / "t.json", OPEN - DAY)
    r = health.sign_in("x", p, OPEN, ENGINE_SIGN_IN_CMD, live=(False, "Robinhood sign-in expired: run `x`"))
    assert r["state"] == "bad" and "sign-in expired" in r["detail"]
    assert r["cmd"] == ENGINE_SIGN_IN_CMD


def test_a_sign_in_row_never_carries_the_token(tmp_path):
    for saved in (OPEN - DAY, OPEN - 9.2 * DAY, OPEN - 20 * DAY):
        r = health.sign_in("x", token(tmp_path / "t.json", saved), OPEN, ENGINE_SIGN_IN_CMD)
        assert SECRET not in json.dumps(r)


# ---------------------------------------------------------------- recorder
def test_recorder_writing_in_market_hours_is_green(tmp_path):
    r = health.recorder_writing(touched(tmp_path / "hb", OPEN - 8), tmp_path / "days.jsonl", OPEN, CFG)
    assert r["state"] == "ok" and "8 s ago" in r["detail"]


def test_recorder_silent_in_market_hours_is_red_with_the_log_and_restart(tmp_path):
    r = health.recorder_writing(touched(tmp_path / "hb", OPEN - 600), tmp_path / "days.jsonl", OPEN, CFG)
    assert r["state"] == "bad" and "10 min" in r["detail"]
    assert "recorder.log" in r["fix"] and "com.agentdesk.recorder" in r["cmd"]


def test_recorder_never_written_in_market_hours_is_red(tmp_path):
    assert health.recorder_writing(tmp_path / "hb", tmp_path / "days.jsonl", OPEN, CFG)["state"] == "bad"


def test_recorder_after_hours_and_on_holidays_is_idle(tmp_path):
    hb = touched(tmp_path / "hb", OPEN - DAY)
    for now in (EVENING, MORNING, HOLIDAY):
        assert health.recorder_writing(hb, tmp_path / "days.jsonl", now, CFG)["state"] == "idle"


def test_recorder_row_shows_the_last_finished_day(tmp_path):
    days = tmp_path / "days.jsonl"
    days.write_text(json.dumps({"day": "2026-10-23", "rows": 1000}) + "\nnot json\n"
                    + json.dumps({"day": "2026-10-26", "rows": 94321}) + "\n")
    r = health.recorder_writing(touched(tmp_path / "hb", EVENING - 3 * 3600), days, EVENING, CFG)
    assert "2026-10-26" in r["detail"] and "94,321" in r["detail"]


def calls_db(tmp_path, rows):
    db = tmp_path / "journal.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE rh_calls (ts REAL, tool TEXT, ms REAL, ok INTEGER, kind TEXT, err TEXT, tag TEXT)")
    c.executemany("INSERT INTO rh_calls VALUES (?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    return db


def test_recorder_calls_all_good_is_green(tmp_path):
    db = calls_db(tmp_path, [(OPEN - 10 * i, "get_option_quotes", 300, 1, "ok", None, "recorder") for i in range(30)])
    r = health.recorder_calls(db, OPEN, CFG)
    assert r["state"] == "ok" and "30" in r["detail"]


def test_recorder_rate_limits_are_amber(tmp_path):
    db = calls_db(tmp_path, [(OPEN - 10, "get_option_quotes", 300, 1, "ok", None, "recorder"),
                             (OPEN - 20, "get_option_quotes", 300, 0, "throttled", "429", "recorder")])
    r = health.recorder_calls(db, OPEN, CFG)
    assert r["state"] == "warn" and "1 rate-limited" in r["detail"]


def test_recorder_auth_errors_are_red_with_the_sign_in_command(tmp_path):
    db = calls_db(tmp_path, [(OPEN - 20, "get_option_quotes", 300, 0, "auth", "401", "recorder")])
    r = health.recorder_calls(db, OPEN, CFG)
    assert r["state"] == "bad" and "record-quotes --once" in r["cmd"]


def test_recorder_calls_ignore_other_tags_and_old_rows(tmp_path):
    db = calls_db(tmp_path, [(OPEN - 3600, "get_option_quotes", 300, 0, "auth", "401", "recorder"),
                             (OPEN - 20, "get_equity_quotes", 300, 0, "auth", "401", "probe"),
                             (OPEN - 5, "get_option_quotes", 300, 1, "ok", None, "iv")])
    assert health.recorder_calls(db, OPEN, CFG)["state"] == "ok"


def test_recorder_calls_after_hours_with_none_is_idle(tmp_path):
    db = calls_db(tmp_path, [])
    assert health.recorder_calls(db, EVENING, CFG)["state"] == "idle"
    assert health.recorder_calls(tmp_path / "nope.db", EVENING, CFG)["state"] == "idle"


def test_recorder_calls_silent_in_market_hours_is_red(tmp_path):
    db = calls_db(tmp_path, [(OPEN - 400, "get_option_quotes", 300, 1, "ok", None, "recorder")])
    assert health.recorder_calls(db, OPEN, CFG)["state"] == "bad"


# ---------------------------------------------------------------- engine
STATS = {"last_ok": OPEN - 3, "last_err": None, "window": {"ok": 900, "rate_limited": 0, "timeout": 0, "auth": 0,
                                                            "error": 0},
         "day": {"ok": 9000, "rate_limited": 0, "timeout": 0, "auth": 0, "error": 0, "reconnects": 0}}


def test_engine_calls_flowing_is_green():
    r = health.engine_calls(STATS, OPEN, CFG, running=True)
    assert r["state"] == "ok" and "3 s ago" in r["detail"]


def test_engine_rate_limits_and_timeouts_are_amber():
    s = {**STATS, "window": {**STATS["window"], "rate_limited": 4, "timeout": 1}}
    r = health.engine_calls(s, OPEN, CFG, running=True)
    assert r["state"] == "warn" and "4 rate-limited" in r["detail"] and "1 timed out" in r["detail"]


def test_engine_auth_errors_are_red():
    s = {**STATS, "window": {**STATS["window"], "auth": 2}}
    r = health.engine_calls(s, OPEN, CFG, running=True)
    assert r["state"] == "bad" and r["cmd"] == ENGINE_SIGN_IN_CMD


def test_engine_no_good_call_for_two_minutes_in_market_hours_is_red():
    r = health.engine_calls({**STATS, "last_ok": OPEN - 200}, OPEN, CFG, running=True)
    assert r["state"] == "bad"


def test_engine_calls_when_it_isnt_running_are_idle_with_the_last_values():
    r = health.engine_calls(STATS, EVENING, CFG, running=False)
    assert r["state"] == "idle" and "last good call" in r["detail"]
    assert health.engine_calls(None, EVENING, CFG, running=False)["state"] == "idle"


def test_reconnects_today_show_in_the_detail():
    s = {**STATS, "day": {**STATS["day"], "reconnects": 2}}
    assert "2 reconnects today" in health.engine_calls(s, OPEN, CFG, running=True)["detail"]


def test_spy_prints_fresh_is_green_and_stalled_is_red():
    assert health.feed("alpaca-iex", OPEN - 4, OPEN, CFG, running=True)["state"] == "ok"
    r = health.feed("alpaca-iex", OPEN - 300, OPEN, CFG, running=True)
    assert r["state"] == "bad" and "5 min" in r["detail"]
    assert health.feed("alpaca-iex", OPEN - 300, EVENING, CFG, running=True)["state"] == "idle"
    assert health.feed(None, None, EVENING, CFG, running=False)["state"] == "idle"


def test_engine_running_is_green():
    r = health.engine_row(True, {"mode": "paper", "day": "2026-10-27"}, OPEN, CFG)
    assert r["state"] == "ok" and "paper" in r["detail"]


def test_engine_not_running_in_session_hours_is_red_with_the_kickstart():
    r = health.engine_row(False, {"day": "2026-10-26"}, OPEN, CFG)
    assert r["state"] == "bad" and "com.agentdesk.paper" in r["cmd"]


def test_engine_not_running_outside_session_hours_is_idle():
    for now in (EVENING, at_ct(date(2026, 10, 27), time(8, 0)), HOLIDAY, at_ct(date(2026, 10, 31), time(10, 0))):
        assert health.engine_row(False, None, now, CFG)["state"] == "idle", now


# ---------------------------------------------------------------- halts
def test_no_halts_is_green():
    r = health.halts({"halted": False}, {"books": [{"book": "B", "halted": False}], "account": {"halted": False}},
                     running=True)
    assert r["state"] == "ok"


def test_one_book_halted_is_amber_with_its_reason():
    risk = {"halted": True, "halt_reason": "daily loss limit -$400", "halt_scope": "A"}
    r = health.halts(risk, {"books": [{"book": "C", "halted": True, "halt_reason": "3 errors in a row"}],
                            "account": {"halted": False}}, running=True)
    assert r["state"] == "warn"
    assert "A: daily loss limit -$400" in r["detail"] and "C: 3 errors in a row" in r["detail"]


def test_an_account_wide_halt_is_red():
    risk = {"halted": True, "halt_reason": "SAFETY: Robinhood sign-in expired", "halt_scope": "account"}
    assert health.halts(risk, None, running=True)["state"] == "bad"
    r = health.halts({"halted": False}, {"books": [], "account": {"halted": True, "halt_reason": "KILL switch"}},
                     running=True)
    assert r["state"] == "bad" and "KILL switch" in r["detail"]


def test_halts_from_a_saved_session_are_idle():
    risk = {"halted": True, "halt_reason": "SAFETY: x", "halt_scope": "account"}
    r = health.halts(risk, None, running=False)
    assert r["state"] == "idle" and "last session" in r["detail"]


# ---------------------------------------------------------------- launchd
def launchctl(outputs):
    def run(cmd, **kw):
        job = cmd[-1].rsplit("/", 1)[-1]
        out = outputs.get(job)
        return SimpleNamespace(returncode=0 if out is not None else 113, stdout=out or "", stderr="")
    return run


LOADED = "\tstate = not running\n\tlast exit code = 0\n"


def test_all_jobs_loaded_and_clean_is_green():
    r = health.jobs(launchctl({j: LOADED for j in health.JOBS}), platform="darwin")
    assert r["state"] == "ok"


def test_a_job_not_loaded_is_red_with_the_installer():
    out = {j: LOADED for j in health.JOBS if j != "com.agentdesk.recorder"}
    r = health.jobs(launchctl(out), platform="darwin")
    assert r["state"] == "bad" and "recorder not loaded" in r["detail"] and "install_recorder.sh" in r["cmd"]


def test_a_job_whose_last_run_failed_is_red():
    out = {j: LOADED for j in health.JOBS}
    out["com.agentdesk.quant"] = "\tstate = not running\n\tlast exit code = 1\n"
    r = health.jobs(launchctl(out), platform="darwin")
    assert r["state"] == "bad" and "quant last exit 1" in r["detail"]


def test_a_running_job_with_no_exit_yet_is_fine():
    out = {j: LOADED for j in health.JOBS}
    out["com.agentdesk.review"] = "\tstate = running\n\tpid = 412\n\tlast exit code = (never exited)\n"
    assert health.jobs(launchctl(out), platform="darwin")["state"] == "ok"


def test_jobs_off_macos_are_idle():
    assert health.jobs(launchctl({}), platform="linux")["state"] == "idle"


# ---------------------------------------------------------------- the report
def test_the_report_rolls_up_to_the_worst_row(tmp_path):
    paths = health.Paths(engine_token=token(tmp_path / "e.json", OPEN - DAY),
                         recorder_token=token(tmp_path / "r.json", OPEN - 10 * DAY),
                         heartbeat=touched(tmp_path / "hb", OPEN - 5), days_log=tmp_path / "days.jsonl",
                         journal=calls_db(tmp_path, [(OPEN - 5, "get_option_quotes", 300, 1, "ok", None, "recorder")]))
    snap = {"mode": "paper", "day": "2026-10-27", "risk": {"halted": False}, "books": None,
            "health": {"rh": STATS, "rh_sign_in": [True, "signed in"], "feed": "alpaca-iex", "last_print": OPEN - 2}}
    rep = health.report(CFG, OPEN, snap, running=True, paths=paths, runner=launchctl({}), platform="linux")
    names = [c["name"] for c in rep["checks"]]
    assert names == ["Engine Robinhood sign-in", "Recorder Robinhood sign-in", "Engine Robinhood calls",
                     "Recorder writing quotes", "Recorder Robinhood calls", "SPY prints", "Engine", "Halts",
                     "Mac jobs"]
    assert rep["state"] == "bad"                         # the recorder's token expired
    assert rep["ts"] == OPEN
    assert SECRET not in json.dumps(rep)


def test_the_report_works_with_no_saved_session(tmp_path):
    paths = health.Paths(engine_token=tmp_path / "e.json", recorder_token=tmp_path / "r.json",
                         heartbeat=tmp_path / "hb", days_log=tmp_path / "days.jsonl", journal=tmp_path / "j.db")
    rep = health.report(CFG, EVENING, None, running=False, paths=paths, runner=launchctl({}), platform="linux")
    assert rep["state"] == "bad"                         # no saved sign-ins at all
    assert len(rep["checks"]) == 9


def test_a_command_is_never_repeated_in_the_fix_text(tmp_path):
    """The panel shows `fix` as prose and `cmd` in a box with a Copy button."""
    r = health.sign_in("x", token(tmp_path / "t.json", OPEN - 10 * DAY), OPEN, ENGINE_SIGN_IN_CMD)
    assert r["cmd"] and r["cmd"] not in r["fix"]
