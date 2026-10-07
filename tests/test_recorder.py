import asyncio
import os
import sys
import time
from datetime import date, time as dtime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk import recorder
from agentdesk.brokers.robinhood import OptionQuoteRecorder
from agentdesk.clock import at_ct
from agentdesk.journal import Journal

MON = date(2026, 9, 28)
EXP = "2026-09-28"


class FakeRH:
    """Mimics the live response shapes checked on 2026-09-28 (instruments paged by `next`, quotes under results[].quote)."""

    def __init__(self, strikes=range(740, 801), page=50):
        self.calls: list[str] = []
        self.inst = [{"id": f"{r[0]}{k}", "expiration_date": EXP, "strike_price": f"{k:.4f}", "type": r}
                     for r in ("call", "put") for k in strikes]
        self.page = page

    async def call(self, tool, args):
        self.calls.append(tool)
        if tool == "get_option_instruments":
            i = int(args.get("cursor") or 0)
            chunk = self.inst[i:i + self.page] if args["expiration_dates"] == EXP else []
            nxt = str(i + self.page) if i + self.page < len(self.inst) and chunk else None
            return {"instruments": chunk, "next": nxt}
        if tool == "get_option_quotes":
            return {"results": [{"quote": {"instrument_id": oid, "bid_price": "1.10", "ask_price": "1.12"},
                                 "close": {"instrument_id": oid, "price": "1.0"}} for oid in args["instrument_ids"]]}
        if tool == "get_equity_quotes":
            return {"results": [{"quote": {"symbol": "SPY", "last_trade_price": "770.40",
                                           "venue_last_trade_time": "2026-09-28T15:00:00.1Z",
                                           "last_non_reg_trade_price": "768.70",
                                           "venue_last_non_reg_trade_time": "2026-09-28T12:59:00Z"}}]}
        raise AssertionError(tool)


def test_poll_writes_42_rows_and_lists_chain_once_per_day():
    rh, j = FakeRH(), Journal(None)
    rec = OptionQuoteRecorder(rh, j, standalone=True)
    now = at_ct(MON, dtime(9, 0))
    assert asyncio.run(rec.poll_once(770.4, now)) == 42
    assert asyncio.run(rec.poll_once(771.6, now + 10)) == 42
    assert rh.calls.count("get_option_instruments") == 3          # 122 contracts in pages of 50, listed once
    assert rh.calls.count("get_option_quotes") == 2
    rows = j.db.execute("SELECT expiry, strike, right, bid, ask, spot FROM option_quotes WHERE ts=?", (now,)).fetchall()
    assert {r[2] for r in rows} == {"call", "put"}
    assert min(r[1] for r in rows) == 760 and max(r[1] for r in rows) == 780
    assert all(r[0] == EXP and r[3] == 1.10 and r[4] == 1.12 and r[5] == 770.4 for r in rows)


def test_no_expiry_today_writes_nothing_and_does_not_hammer_the_chain():
    rh, j = FakeRH(), Journal(None)
    rec = OptionQuoteRecorder(rh, j, standalone=True)
    hol = at_ct(date(2026, 11, 26), dtime(9, 0))                  # Thanksgiving: no SPY 0DTE
    for k in range(5):
        assert asyncio.run(rec.poll_once(770.0, hol + 10 * k)) == 0
    assert rh.calls.count("get_option_instruments") == 1
    assert "get_option_quotes" not in rh.calls


def test_spot_price_uses_most_recent_print():
    assert asyncio.run(recorder.spot_price(FakeRH(), "SPY")) == 770.40


def test_metered_client_refuses_order_tools(tmp_path):
    cfg = {"robinhood": {"mcp_url": "x", "token_dir": str(tmp_path), "redirect_port": 1, "account_number": None}}
    m = recorder.MeteredRobinhoodMCP(cfg, recorder.CallMeter(tmp_path / "j.db"))
    for tool in ("place_option_order", "review_option_order", "cancel_option_order", "place_equity_order"):
        with pytest.raises(PermissionError):
            asyncio.run(m.call(tool, {}))


def test_classify_errors():
    assert recorder.classify("HTTP 429 Too Many Requests") == "throttled"
    assert recorder.classify("get_option_quotes error: 500 Internal Server Error") == "server"
    assert recorder.classify("401 Unauthorized") == "auth"
    assert recorder.classify("ReadTimeout: timed out") == "timeout"


def test_window_and_next_start():
    s, e = "08:25", "15:05"
    assert recorder.in_window(at_ct(MON, dtime(8, 25)), s, e)
    assert recorder.in_window(at_ct(MON, dtime(15, 4)), s, e)
    assert not recorder.in_window(at_ct(MON, dtime(15, 5)), s, e)
    assert not recorder.in_window(at_ct(date(2026, 9, 27), dtime(10, 0)), s, e)       # Sunday
    fri_eve = at_ct(date(2026, 10, 2), dtime(16, 0))
    assert recorder.next_window_start(fri_eve, s) == at_ct(date(2026, 10, 5), dtime(8, 25))


def test_engine_copy_stands_down_while_standalone_heartbeat_is_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "HEARTBEAT", tmp_path / "heartbeat")
    assert not recorder.standalone_active()
    (tmp_path / "heartbeat").touch()
    assert recorder.standalone_active()
    old = time.time() - 120
    os.utime(tmp_path / "heartbeat", (old, old))
    assert not recorder.standalone_active()


def test_rate_report_and_day_summary(tmp_path):
    db = tmp_path / "j.db"
    Journal(db)
    m = recorder.CallMeter(db)
    t = at_ct(MON, dtime(9, 0))
    for k in range(30):
        m.add(t + k, "get_option_quotes", 120 + k, True, "ok", None)
    m.add(t + 31, "get_option_quotes", 900, False, "throttled", "429")
    m.flush()
    s = recorder.day_summary(db, EXP)
    assert s["calls"] == 31 and s["outcomes"] == {"ok": 30, "throttled": 1}
    rep = recorder.rate_report(db, days=10000)
    assert "get_option_quotes" in rep and "throttled" in rep


def test_iv_pass_tools_are_read_only():
    from agentdesk.recorder import READ_ONLY_TOOLS
    assert "get_earnings_calendar" in READ_ONLY_TOOLS and "get_option_chains" in READ_ONLY_TOOLS
    assert not {"place_option_order", "cancel_option_order", "exercise_option", "review_option_order"} & READ_ONLY_TOOLS


def test_meter_rows_can_carry_the_iv_tag(tmp_path):
    from agentdesk.recorder import CallMeter
    m = CallMeter(tmp_path / "j.db")
    m.add(1.0, "get_option_quotes", 12.0, True, "ok", None, tag="iv")
    m.add(2.0, "get_option_quotes", 12.0, True, "ok", None)
    m.flush()
    assert [r[0] for r in m.db.execute("SELECT tag FROM rh_calls ORDER BY ts")] == ["iv", "recorder"]


def test_quote_and_iv_loops_share_one_robinhood_session(tmp_path, monkeypatch):        # book E final review 2
    from agentdesk.config import load_config
    started = []

    class FakeMCP:
        def __init__(self, cfg, meter, interactive=False):
            self.meter = meter

        async def start(self):
            started.append(self)
            await asyncio.sleep(0.01)          # the OAuth handshake yields to the other loop

        async def close(self):
            pass
    monkeypatch.setattr(recorder, "RECORDER_DIR", tmp_path)
    monkeypatch.setattr(recorder, "MeteredRobinhoodMCP", FakeMCP)
    cfg = load_config()
    cfg["journal_path"] = str(tmp_path / "j.db")
    d = recorder.RecorderDaemon(cfg)

    async def go():
        await asyncio.gather(d.connect(), d.connect(), recorder.IVClient(d).d.connect())
    asyncio.run(go())
    assert len(started) == 1 and d.rh is started[0]


def test_empty_polls_alert_like_failed_ones(tmp_path, monkeypatch):           # 2026-10-01 sweep
    """Polls that return 0 rows without raising (a schema change, no bids) must still raise the 5-minute alert."""
    from agentdesk.config import load_config
    monkeypatch.setattr(recorder, "RECORDER_DIR", tmp_path)
    alerts = []
    monkeypatch.setattr(recorder, "notify", alerts.append)
    cfg = load_config()
    cfg["journal_path"] = str(tmp_path / "j.db")
    d = recorder.RecorderDaemon(cfg)
    d.ivs = {}
    d.awake = lambda on: None
    clock = [at_ct(MON, dtime(10, 0))]
    monkeypatch.setattr(recorder.time, "time", lambda: clock[0])

    async def empty(now):
        return 0
    d.tick = empty

    class Done(BaseException):          # not caught by the poll loop
        pass

    async def sleep(s):
        clock[0] += 10
        if clock[0] > at_ct(MON, dtime(10, 7)):
            raise Done
    monkeypatch.setattr(recorder.asyncio, "sleep", sleep)
    with pytest.raises(Done):
        asyncio.run(d.run())
    assert len(alerts) == 1 and "No quotes" in alerts[0]
