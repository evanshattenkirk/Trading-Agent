"""Premarket schedule: desks arrive and catch up at their own desks (08:15 CT), then a quick
huddle before the open (08:25 CT) that uses the briefs they prepared."""
import asyncio
import copy
import sys
from datetime import date, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.clock import at_ct
from agentdesk.config import load_config
from agentdesk.crew import PREMARKET_DESKS, Crew
from agentdesk.risk import RiskManager

CFG = load_config()
D = date(2026, 9, 28)          # a Monday
PREMARKET = PREMARKET_DESKS


class Bus:
    def __init__(self):
        self.events = []

    def emit(self, kind, ts, **kw):
        self.events.append((kind, ts, kw))


class Journal:
    def __init__(self):
        self.briefs = []

    def record_brief(self, day, ts, desk, brief):
        self.briefs.append((ts, desk))


class Feed:
    is_sim = True
    mood = "neutral"

    def __init__(self, now):
        self.t = now

    def now(self):
        return self.t


class FakeEngine:
    def __init__(self, cfg, now):
        self.cfg, self.feed, self.bus, self.journal = cfg, Feed(now), Bus(), Journal()
        self.risk = RiskManager(cfg)
        self.price, self.l2 = 765.0, None
        self.bars = {"1m": []}
        self.agent = []

    def set_agent(self, now, activity, text=""):
        self.agent.append((now, activity, text))


def make(cfg=None, now=None):
    cfg = copy.deepcopy(cfg or CFG)
    e = FakeEngine(cfg, now or at_ct(D, time(7, 0)))
    return e, Crew(e, cfg)


def run(coro):
    return asyncio.run(coro)


def briefs_at(e, ts):
    return [d for t, d in e.journal.briefs if t == ts]


def test_schedule_defaults_are_0815_arrive_and_0825_huddle():
    assert CFG["crew"]["schedule"]["arrive"] == "08:15"
    assert CFG["crew"]["schedule"]["premarket"] == "08:25"


def test_nothing_happens_before_arrival():
    e, c = make()
    run(c.on_clock(at_ct(D, time(8, 14))))
    assert not e.journal.briefs and not e.bus.events


def test_desks_catch_up_at_0815_then_huddle_at_0825():
    e, c = make()
    t_arrive, t_huddle = at_ct(D, time(8, 15)), at_ct(D, time(8, 25))

    async def day():
        await c.on_clock(t_arrive)
        arrive_events, prepared = list(e.bus.events), set(c.prep)
        await c.on_clock(at_ct(D, time(8, 20)))      # nothing new between arrival and the huddle
        mid_events = list(e.bus.events)
        await c.on_clock(t_huddle)
        return arrive_events, prepared, mid_events

    arrive_events, prepared, mid_events = run(day())
    # catch-up: nobody walks to the huddle yet, each premarket desk has prepared a brief
    assert not any(kw.get("phase") == "walk" for _, _, kw in arrive_events)
    assert prepared == set(PREMARKET)
    assert mid_events == arrive_events
    # 08:25 huddle: desks walk over and the briefs recorded are the ones prepared at 08:15
    walks = [kw["desk"] for _, ts, kw in e.bus.events if ts == t_huddle and kw.get("phase") == "walk"]
    assert walks == PREMARKET
    assert sorted(briefs_at(e, t_huddle)) == sorted(PREMARKET)
    for k in PREMARKET:
        assert c.briefs[k]["slot"] == "premarket"
        assert c.briefs[k]["prepared_ts"] == t_arrive
    assert not c.prep                                  # prepared briefs are consumed once


def test_huddle_finishes_before_the_open():
    e, c = make()

    async def day():
        await c.on_clock(at_ct(D, time(8, 15)))
        await c.on_clock(at_ct(D, time(8, 25)))

    run(day())
    done = [ts for _, ts, kw in e.bus.events if kw.get("phase") == "done"]
    assert done and max(done) < at_ct(D, time(8, 30))


def test_late_desk_attends_with_offline_brief_and_never_delays_the_huddle():
    e, c = make()
    c.offline = False                                   # pretend we have an API key
    slow = asyncio.Event()

    async def fake_llm(key, now, extra=""):
        if key == "fed":
            await slow.wait()                           # Fed Watch is still researching at 08:25
        return {"headline": f"{key} live read", "bias": "neutral", "confidence": 0.7, "size_multiplier": 1.0}

    async def fake_roundtable(slot, desks, now):
        return {"lines": [], "votes": {}, "proposals": []}

    c._llm = fake_llm
    c._roundtable = fake_roundtable

    async def day():
        await c.on_clock(at_ct(D, time(8, 15)))
        await asyncio.sleep(0)                          # let the fast desks finish
        await asyncio.sleep(0)
        await c._consult("premarket", PREMARKET, at_ct(D, time(8, 25)))

    run(day())
    assert c.briefs["macro"]["headline"] == "macro live read"
    fed = c.briefs["fed"]
    assert fed["headline"] != "fed live read"
    assert any("still researching" in n.lower() for n in fed.get("notes", []))
    assert 0.5 <= fed["size_multiplier"] <= 1.25


def test_engine_started_after_the_huddle_time_skips_catch_up_and_briefs_fully():
    e, c = make()
    t = at_ct(D, time(9, 10))
    run(c.on_clock(t))
    assert not c.prep
    assert sorted(briefs_at(e, t)) == sorted(PREMARKET)


def test_without_arrive_key_the_old_single_huddle_still_works():
    cfg = copy.deepcopy(CFG)
    cfg["crew"]["schedule"].pop("arrive")
    e, c = make(cfg)
    t = at_ct(D, time(8, 25))
    run(c.on_clock(t))
    assert sorted(briefs_at(e, t)) == sorted(PREMARKET)


def test_postclose_sign_off_names_the_arrival_time():
    e, c = make()
    assert "8:15" in c._wrap_line("postclose")
