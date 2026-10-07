"""The dashboard shows whether today's 0DTE quotes are landing (review 2026-10-06, I8).

The standalone recorder touches ~/.agentdesk/recorder/heartbeat each time it writes quotes (market hours only). The
engine reads its age every 15 s, puts the state in the snapshot and sends a 'recorder' event when it changes, so a
recorder that stopped (an expired Robinhood token, a crashed job) shows on the dashboard instead of only in its log."""
import os
import sys
from datetime import date, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from agentdesk import engine as engine_mod
from agentdesk.clock import at_ct

from test_safety import NOW, engine


@pytest.fixture
def beat(tmp_path, monkeypatch):
    hb = tmp_path / "heartbeat"
    monkeypatch.setattr(engine_mod, "RECORDER_HEARTBEAT", hb)

    def at(ts):
        hb.touch()
        os.utime(hb, (ts, ts))
    return at


def test_fresh_heartbeat_in_market_hours_is_ok(beat):
    e = engine()
    beat(NOW - 20)
    assert e.recorder_status(NOW) == {"state": "ok", "age": 20}


def test_stale_heartbeat_in_market_hours_is_down(beat):
    e = engine()
    beat(NOW - 600)
    assert e.recorder_status(NOW) == {"state": "down", "age": 600}


def test_no_heartbeat_file_in_market_hours_is_down(beat):
    assert engine().recorder_status(NOW) == {"state": "down", "age": None}


def test_the_first_two_minutes_after_the_open_are_not_down(beat):
    e = engine()
    beat(NOW - 86400)                                         # yesterday's last write
    assert e.recorder_status(at_ct(date(2026, 9, 28), time(8, 31)))["state"] == "idle"
    assert e.recorder_status(at_ct(date(2026, 9, 28), time(8, 33)))["state"] == "down"


def test_outside_market_hours_holidays_and_after_an_early_close_are_idle(beat):
    e = engine()
    beat(NOW - 86400)                                        # last written yesterday
    assert e.recorder_status(at_ct(date(2026, 9, 28), time(8, 15)))["state"] == "idle"
    assert e.recorder_status(at_ct(date(2026, 9, 28), time(15, 5)))["state"] == "idle"
    e.cfg.setdefault("calendar", {})["holidays"] = ["2026-09-28"]
    assert e.recorder_status(NOW)["state"] == "idle"
    e.cfg["calendar"] = {"holidays": [], "early_close": ["2026-09-28"]}
    assert e.recorder_status(at_ct(date(2026, 9, 28), time(12, 30)))["state"] == "idle"
    assert e.recorder_status(at_ct(date(2026, 9, 28), time(11, 0)))["state"] == "down"


def test_the_engines_own_recorder_counts_when_the_standalone_one_is_quiet(beat):
    e = engine()
    beat(NOW - 600)
    e._recorder_task = object()                              # record_option_quotes on and no standalone recorder
    assert e.recorder_status(NOW)["state"] == "engine"


def test_the_simulator_has_no_recorder_state(beat):
    e = engine()
    e.feed.is_sim = True
    assert e.recorder_status(NOW) is None


def test_a_change_is_sent_once_and_kept_in_the_snapshot(beat):
    e = engine()
    sent = []
    e.bus.emit = lambda kind, ts, **kw: sent.append((kind, kw))
    beat(NOW - 5)
    e._check_recorder(NOW)
    e._check_recorder(NOW + 15)                               # same state: nothing new
    assert [k for k, _ in sent if k == "recorder"] == ["recorder"]
    assert e.snapshot()["recorder"]["state"] == "ok"
    e._check_recorder(NOW + 60)                               # a minute on: the same state again, with its age
    assert [kw for k, kw in sent if k == "recorder"][-1] == {"state": "ok", "age": 65}
    assert not any(k == "log" for k, _ in sent)
    e._check_recorder(NOW + 400)                              # heartbeat now 405 s old
    rec = [kw for k, kw in sent if k == "recorder"]
    assert rec[-1] == {"state": "down", "age": 405}
    assert any(k == "log" and kw.get("level") == "warn" and "recorder" in kw.get("msg", "") for k, kw in sent)


def test_the_check_runs_every_15_seconds(beat, monkeypatch):
    e = engine()
    calls = []
    monkeypatch.setattr(e, "recorder_status", lambda now: calls.append(now) or {"state": "ok", "age": 1})
    for s in range(31):
        e._check_recorder(NOW + s)
    assert calls == [NOW, NOW + 15, NOW + 30]
