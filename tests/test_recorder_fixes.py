"""Standalone recorder (review 2026-10-06): an empty 0DTE chain is a holiday only on a configured holiday, otherwise
the stale-data alert still fires (M4); its normal loops keep to 100 Robinhood calls a minute, the probe doesn't (I7)."""
import asyncio
import sys
from datetime import date, time as dtime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk import recorder
from agentdesk.brokers.robinhood import CallBudget
from agentdesk.clock import at_ct
from agentdesk.config import load_config

MON = date(2026, 9, 28)


def run_with_empty_chain(tmp_path, monkeypatch, holidays=()):
    """Ten minutes of RTH polls that list an empty 0DTE chain and write nothing; returns the alerts raised."""
    monkeypatch.setattr(recorder, "RECORDER_DIR", tmp_path)
    alerts = []
    monkeypatch.setattr(recorder, "notify", alerts.append)
    cfg = load_config()
    cfg["journal_path"] = str(tmp_path / "j.db")
    cfg["calendar"] = {**(cfg.get("calendar") or {}), "holidays": list(holidays)}
    d = recorder.RecorderDaemon(cfg)
    d.ivs = {}
    d.awake = lambda on: None
    clock = [at_ct(MON, dtime(10, 0))]
    monkeypatch.setattr(recorder.time, "time", lambda: clock[0])

    async def empty_chain(now):            # get_option_instruments came back empty (schema change, empty page)
        d.rec = SimpleNamespace(chain_day=str(MON), chain={})
        return 0
    d.tick = empty_chain

    class Done(BaseException):
        pass

    async def sleep(s):
        clock[0] += 10
        if clock[0] > at_ct(MON, dtime(10, 10)):
            raise Done
    monkeypatch.setattr(recorder.asyncio, "sleep", sleep)
    with pytest.raises(Done):
        asyncio.run(d.run())
    return alerts


def test_an_empty_chain_on_a_trading_day_still_alerts(tmp_path, monkeypatch):
    alerts = run_with_empty_chain(tmp_path, monkeypatch)
    assert len(alerts) == 1 and "No quotes" in alerts[0]


def test_an_empty_chain_on_a_configured_holiday_is_quiet(tmp_path, monkeypatch):
    assert run_with_empty_chain(tmp_path, monkeypatch, holidays=[MON]) == []


def test_recorder_loops_keep_to_100_calls_a_minute_and_the_probe_is_uncapped(tmp_path, monkeypatch):
    cfg = load_config()
    s = recorder.settings(cfg)
    b = CallBudget.from_cfg(recorder.recorder_cfg(cfg, s)["robinhood"])
    assert b is not None and b.per_min == 100
    assert CallBudget.from_cfg(recorder.recorder_cfg(cfg, s, capped=False)["robinhood"]) is None

    seen = []

    class Stop(Exception):
        pass

    class FakeMCP:
        def __init__(self, rcfg, meter):
            seen.append(rcfg["robinhood"].get("call_budget"))

        async def start(self):
            raise Stop

    monkeypatch.setattr(recorder, "MeteredRobinhoodMCP", FakeMCP)
    cfg["journal_path"] = str(tmp_path / "j.db")
    with pytest.raises(Stop):
        asyncio.run(recorder.probe(cfg))
    assert seen == [None]                                 # the probe looks for the throttle point: no cap
