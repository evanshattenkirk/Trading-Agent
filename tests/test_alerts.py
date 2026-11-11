"""Phone alerts (Evan, 2026-11-09/10: "imessage to myself is good"). A LaunchAgent checks the Manager panel every
5 min and texts Evan when a row turns amber or red, repeats red hourly in the day, says when it's fixed, and sends a
morning all-clear. Spec: docs/superpowers/specs/2026-11-10-phone-alerts-design.md.

No test here may run the real osascript: every send goes through a fake runner or sender."""
import copy
from datetime import date, time

from agentdesk import alerts, health
from agentdesk.clock import at_ct
from agentdesk.config import load_config

CFG = load_config()
TUE = date(2026, 11, 10)


def at(hh, mm=0, d=TUE):
    return at_ct(d, time(hh, mm))


def rep(*rows):
    rows = list(rows)
    return {"checks": rows, "state": health.worst(rows)}


def sign_in(state, detail="signed in until Thu Nov 13 09:12 CT (2d 20h left)"):
    return health.row("Engine Robinhood sign-in", state, detail, "Sign in again before then: run this in Terminal.",
                      "cd ~/Trading-Agent && .venv/bin/python -m agentdesk rh-inspect")


def engine(state=health.OK, detail="running in paper mode"):
    return health.row("Engine", state, detail)


def cfg_with(**alerts_over):
    c = copy.deepcopy(CFG)
    c["alerts"] = {**(c.get("alerts") or {}), **alerts_over}
    return c


NO_MORNING = cfg_with(morning=False)


def run(prev, report, now, cfg=NO_MORNING):
    return alerts.diff(prev, report, now, cfg)


# ---------------------------------------------------------------- diff: transitions
def test_first_red_goes_at_any_hour():
    texts, st = run({}, rep(sign_in(health.BAD)), at(3))
    assert len(texts) == 1
    assert "🔴 Engine Robinhood sign-in" in texts[0] and "agentdesk rh-inspect" in texts[0]
    assert st["rows"]["Engine Robinhood sign-in"]["sent"] == "bad"


def test_red_repeats_hourly_only_in_the_day():
    _, st = run({}, rep(sign_in(health.BAD)), at(10))
    assert run(st, rep(sign_in(health.BAD)), at(10, 59))[0] == []
    texts, _ = run(st, rep(sign_in(health.BAD)), at(11))
    assert len(texts) == 1 and "still red" in texts[0]
    _, night = run({}, rep(sign_in(health.BAD)), at(2))
    assert run(night, rep(sign_in(health.BAD)), at(3, 30))[0] == []


def test_amber_waits_for_morning():
    texts, st = run({}, rep(sign_in(health.WARN)), at(22, d=date(2026, 11, 9)))
    assert texts == [] and "Engine Robinhood sign-in" not in st.get("rows", {})
    texts, st = run(st, rep(sign_in(health.WARN)), at(7))
    assert len(texts) == 1 and "🟠 Engine Robinhood sign-in" in texts[0]
    _, st = run({}, rep(sign_in(health.WARN)), at(22, d=date(2026, 11, 9)))
    texts, st = run(st, rep(sign_in(health.OK)), at(6))
    assert texts == []
    assert run(st, rep(sign_in(health.OK)), at(7))[0] == []


def test_red_then_green_overnight_sends_fixed_at_seven():
    _, st = run({}, rep(sign_in(health.BAD)), at(2))
    texts, st = run(st, rep(sign_in(health.OK)), at(5))
    assert texts == []
    texts, st = run(st, rep(sign_in(health.OK, "signed in until Fri Nov 20")), at(7))
    assert len(texts) == 1 and "✅" in texts[0] and "fixed" in texts[0]
    assert "Engine Robinhood sign-in" not in st["rows"]


def test_red_to_amber_is_sent():
    _, st = run({}, rep(sign_in(health.BAD)), at(9))
    texts, st = run(st, rep(sign_in(health.WARN)), at(10))
    assert len(texts) == 1 and "🟠" in texts[0]
    assert st["rows"]["Engine Robinhood sign-in"]["sent"] == "warn"


def test_wording_change_sends_nothing():
    _, st = run({}, rep(sign_in(health.WARN, "2d 20h left")), at(9))
    assert run(st, rep(sign_in(health.WARN, "2d 19h left")), at(10))[0] == []


def test_idle_rows_never_alert():
    texts, st = run({}, rep(health.row("SPY prints", health.IDLE, "market closed"), engine(health.IDLE)), at(10))
    assert texts == [] and st.get("rows", {}) == {}


def test_missing_row_counts_as_green():
    dash = health.row("Dashboard", health.BAD, "neither the engine nor the review page answers on :8765")
    _, st = run({}, rep(dash, engine()), at(9))
    texts, st = run(st, rep(engine()), at(10))
    assert len(texts) == 1 and "✅ Dashboard" in texts[0]
    assert "Dashboard" not in st["rows"]


def test_one_message_worst_first():
    texts, _ = run({}, rep(sign_in(health.WARN), engine(health.BAD, "not running during the session")), at(10))
    assert len(texts) == 1
    assert texts[0].index("🔴") < texts[0].index("🟠")


def test_no_secret_reaches_a_message():
    r = sign_in(health.BAD)
    r["token"] = "eyJ-secret"
    texts, _ = run({}, rep(r), at(10))
    assert texts and all("eyJ-secret" not in t for t in texts)


# ---------------------------------------------------------------- diff: the morning all-clear
def test_morning_all_clear_once_on_trading_days():
    texts, st = alerts.diff({}, rep(engine(), sign_in(health.OK)), at(8, 25), CFG)
    assert len(texts) == 1 and texts[0].startswith("AgentDesk 08:25: all green")
    assert "running in paper mode" in texts[0]
    assert alerts.diff(st, rep(engine()), at(8, 40), CFG)[0] == []
    assert alerts.diff({}, rep(engine()), at(8, 25, d=date(2026, 11, 14)), CFG)[0] == []    # Saturday
    assert alerts.diff({}, rep(engine()), at(8, 25, d=date(2026, 11, 26)), CFG)[0] == []    # Thanksgiving
    assert alerts.diff({}, rep(engine()), at(8, 10), CFG)[0] == []
    assert alerts.diff({}, rep(engine()), at(8, 25), NO_MORNING)[0] == []


def test_morning_lists_what_is_not_green():
    _, st = alerts.diff({}, rep(engine(), sign_in(health.WARN)), at(8), CFG)     # the amber goes out at 08:00
    texts, _ = alerts.diff(st, rep(engine(), sign_in(health.WARN)), at(8, 25), CFG)
    assert len(texts) == 1
    assert "not all green" in texts[0] and "Engine Robinhood sign-in (warn)" in texts[0]


def test_settings_have_defaults_without_an_alerts_section():
    c = copy.deepcopy(CFG)
    c.pop("alerts", None)
    s = alerts.settings(c)
    assert s["day_start"] == "07:00" and s["day_end"] == "21:00" and s["repeat_bad_min"] == 60
    assert s["morning"] is True and s["morning_at"] == "08:20" and s["enabled"] is True
