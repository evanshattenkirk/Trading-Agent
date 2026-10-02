"""Fixes from the 2026-10-02 paper session check: the risk panel's per-book loss limits, one blackout per event
window, crew notes without web-search citation tags, no zero-length minute-bar calls, and a dashboard socket that
resyncs after the bus drops events for it."""
import asyncio
import copy
import json
import sys
from datetime import date, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.books.book import Book
from agentdesk.bus import Bus
from agentdesk.clock import at_ct
from agentdesk.config import load_config
from agentdesk.risk import RiskManager

from test_crew_desks import make
from test_f_data import RH, data

CFG = load_config()
D = date(2026, 10, 2)
NFP = at_ct(D, time(7, 30))


class Strat:
    name = "TEST"


def test_book_snapshot_carries_its_day_loss_limit():
    assert Book("F1_stocks_in_play", {"daily_loss": 75}, Strat()).to_dict()["daily_loss"] == 75
    assert Book("C_orb_bull_put", {"max_trades_day": 2}, Strat()).to_dict()["daily_loss"] is None


def test_a_reworded_event_at_the_same_time_adds_no_second_blackout():
    r = RiskManager(copy.deepcopy(CFG))
    r.add_blackout(NFP, "Employment Situation / Nonfarm Payrolls (Sep)", added_ts=NFP - 3600)
    r.add_blackout(NFP, "Sep NFP (already out): +29K vs 90K exp", added_ts=NFP - 3000)
    assert [b.name for b in r.st.blackouts] == ["Employment Situation / Nonfarm Payrolls (Sep)"]


def test_an_event_whose_blackout_already_ended_adds_none():
    r = RiskManager(copy.deepcopy(CFG))
    r.add_blackout(NFP, "Sep NFP (already out)", added_ts=at_ct(D, time(11, 30)))
    assert r.st.blackouts == []


def test_crew_notes_and_headline_drop_citation_tags():
    e, c = make()
    b = c._finish({"headline": "<cite index='7-3'>10Y 5.26%</cite> near highs",
                   "notes": ["NFP +29K. <cite index='12-2,12-5'>Wages +3.1% y/y.</cite>"]}, "macro")
    assert b["headline"] == "10Y 5.26% near highs"
    assert b["notes"] == ["NFP +29K. Wages +3.1% y/y."]


def test_minute_bars_with_no_whole_minute_yet_makes_no_call(tmp_path):
    rh = RH()
    got = asyncio.run(data(tmp_path, rh).minute_bars(["NVDA"], D, 575, 575))
    assert got == {} and rh.calls == []


def test_a_queue_that_overflowed_is_reported_once():
    bus = Bus()
    q = bus.subscribe()
    for i in range(q.maxsize + 3):
        bus.emit("tick", float(i), price=1.0)
    assert bus.take_overflow(q) is True
    assert bus.take_overflow(q) is False


def test_the_socket_resends_a_snapshot_after_its_queue_overflowed():
    from fastapi.testclient import TestClient
    from agentdesk.server import create_app

    class Eng:
        n = 0

        def snapshot(self):
            Eng.n += 1
            return {"type": "snapshot", "n": Eng.n}

    bus = Bus()
    c = TestClient(create_app(Eng(), bus, token="t"), base_url="http://127.0.0.1:8765")
    with c.websocket_connect("ws://127.0.0.1:8765/ws", headers={"Origin": "http://127.0.0.1:8765"}) as ws:
        assert json.loads(ws.receive_text())["n"] == 1
        q = next(iter(bus.subs))

        def flood():             # on the server's loop, as the engine emits; faster than the socket drains
            for i in range(q.maxsize + 3):
                bus.emit("tick", float(i), price=1.0)
        ws.portal.call(flood)
        events = 0
        while events < q.maxsize:            # every queued event, then the resync
            m = json.loads(ws.receive_text())
            if m.get("type") == "snapshot":
                break
            events += len(m["events"])
        assert m == {"type": "snapshot", "n": 2}
