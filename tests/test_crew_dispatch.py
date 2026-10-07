"""How the crew runs next to the engine (review plan 2026-10-06): offline paper huddles go through the same queue as
online ones, so a slow Robinhood read never holds the engine's one-second loop (M23); one "halt" huddle per halt
(I11); Quant's vote reset after a winner is logged (L4); background tasks keep a reference (L3)."""
import asyncio
import copy
import logging
import sys
from datetime import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.clock import at_ct
from agentdesk.journal import Journal

from test_crew_desks import D, make
from test_crew_review import WEEK, online


def run(coro):
    return asyncio.run(coro)


class HangingVix:
    """Robinhood's VIX read stuck (a dropped session, a rate-limit pause) until released."""
    def __init__(self):
        self.release = asyncio.Event()

    async def current(self):
        await self.release.wait()
        return 16.0

    async def prior_close(self, day):
        await self.release.wait()
        return 15.5


def offline_paper(monkeypatch, tmp_path, now):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    e, c = make(now=now, sim=False, tmp=tmp_path)
    c.cfg["calendar_path"] = str(tmp_path / "econ_calendar.json")
    assert c.offline and not e.feed.is_sim
    return e, c


# ----------------------------------------------------------------------------- M23
def test_offline_paper_huddle_never_holds_the_engine_clock(monkeypatch, tmp_path):
    t = at_ct(D, time(11, 30))
    e, c = offline_paper(monkeypatch, tmp_path, t)
    c.ran |= {"calendar", "arrive", "premarket"}

    async def go():
        c.vix = HangingVix()
        await asyncio.wait_for(c.on_clock(t), 1.0)          # the midday huddle is queued, not run on the clock
        assert c._worker is not None and not c._worker.done()
        c.vix.release.set()
        await asyncio.wait_for(c._worker, 5.0)
    run(go())
    assert c.briefs["vol"]["slot"] == "midday" and c.briefs["macro"]["slot"] == "midday"


def test_offline_paper_catch_up_researches_in_tasks_and_the_huddle_does_not_wait(monkeypatch, tmp_path):
    e, c = offline_paper(monkeypatch, tmp_path, at_ct(D, time(8, 15)))

    async def go():
        c.vix = HangingVix()
        await asyncio.wait_for(c.on_clock(at_ct(D, time(8, 15))), 1.0)
        assert "vol" in c.prep_tasks                       # still reading VIX at its desk
        e.feed.t = at_ct(D, time(8, 25))
        await asyncio.wait_for(c.on_clock(e.feed.t), 1.0)
        await asyncio.wait_for(c._worker, 5.0)
    run(go())
    assert any("still researching" in n.lower() for n in c.briefs["vol"]["notes"])
    assert c.briefs["macro"]["slot"] == "premarket"


def test_the_simulator_still_runs_huddles_inline():
    e, c = make(now=at_ct(D, time(11, 30)))
    c.ran |= {"calendar", "arrive", "premarket"}
    run(c.on_clock(at_ct(D, time(11, 30))))
    assert c._worker is None and c.briefs["macro"]["slot"] == "midday"


# ----------------------------------------------------------------------------- I11: one halt huddle per halt
def test_a_kill_switch_flatten_queues_one_halt_huddle(monkeypatch):
    e, c = make()
    slots = []

    async def dispatch(slot, desks, now):
        slots.append(slot)
    c._dispatch = dispatch
    now = at_ct(D, time(10, 0))
    e.risk.halt("KILL switch", flatten=True)
    for _ in range(4):                                     # four positions sold by the flatten
        run(c.on_trade_closed(None, 12.0, now))
    assert slots == ["halt"]
    e.risk.halt("safety: no fresh quote for 11 s", flatten=True)
    run(c.on_trade_closed(None, 12.0, now + 60))
    assert slots == ["halt", "halt"]                       # a different halt gets its own huddle


# ----------------------------------------------------------------------------- L4: Quant's reset is logged
def test_quants_vote_reset_after_a_winner_is_logged_as_a_directive():
    e, c = make()
    e.journal = Journal(None)
    now = at_ct(D, time(10, 0))
    c.briefs["quant"] = {"headline": "2 straight losers", "day": str(D), "ts": now, "size_multiplier": 0.75}
    c._apply(now)
    run(c.on_trade_closed(None, 40.0, now + 300))
    rows = [r for r in e.journal.crew_log(str(D)) if r["kind"] == "directive"]
    assert rows and rows[-1]["detail"]["votes"]["quant"] == 1.0
    assert rows[-1]["detail"]["revised"] == {"quant": 1.0} and rows[-1]["detail"]["book_mults"]["A"] == 1.0


# ----------------------------------------------------------------------------- L3: task references
def test_the_weekly_calendar_task_is_kept_until_it_finishes(monkeypatch, tmp_path):
    e, c = online(monkeypatch, WEEK, now=at_ct(D, time(8, 15)), tmp=tmp_path)
    c.cfg["calendar_path"] = str(tmp_path / "econ_calendar.json")
    c.ran |= {"arrive"}

    async def go():
        await c.on_clock(at_ct(D, time(8, 15)))
        assert len(c._bg) == 1
        await asyncio.gather(*c._bg)
        await asyncio.sleep(0)
    run(go())
    assert not c._bg and (tmp_path / "econ_calendar.json").exists()


def test_l2_read_failures_are_logged_once_a_minute(caplog):
    from agentdesk.l2 import L2Monitor
    m = L2Monitor({"l2": {"poll_ms": 1}})

    class RH:
        calls = 0

        async def price_book(self, symbol):
            RH.calls += 1
            raise RuntimeError("502 Bad Gateway")

    async def go():
        t = asyncio.create_task(m.run_live(RH(), lambda st: None))
        await asyncio.sleep(0.05)
        t.cancel()
    with caplog.at_level(logging.WARNING, logger="agentdesk.l2"):
        run(go())
    warns = [r for r in caplog.records if r.name == "agentdesk.l2"]
    assert RH.calls > 3 and len(warns) == 1 and "502" in warns[0].getMessage()


def test_engine_keeps_its_l2_and_quote_recorder_tasks(monkeypatch):
    from test_safety import engine
    import agentdesk.brokers.robinhood as rb

    class Rec:
        def __init__(self, rh, journal):
            pass

        async def run(self, price, now):
            await asyncio.sleep(3600)
    monkeypatch.setattr(rb, "OptionQuoteRecorder", Rec)
    from agentdesk.l2 import L2Monitor
    e = engine()
    e.inline = False
    e.l2 = L2Monitor(e.cfg)
    e.l2_rh = SimpleNamespace(price_book=lambda s: asyncio.sleep(3600))
    seen = {}

    async def go():
        await e.run()
        seen["l2"], seen["rec"] = e.l2._task, e._recorder_task
        assert not seen["l2"].done() and not seen["rec"].done()
    run(go())
    assert isinstance(seen["l2"], asyncio.Task) and isinstance(seen["rec"], asyncio.Task)
