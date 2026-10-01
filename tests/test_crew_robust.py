"""A malformed LLM brief must not break the crew or book A's entries (2026-10-01 sweep): a null or text confidence,
a null vote, events or proposals that aren't objects. Yesterday's briefs and directive don't carry into a new day."""
import asyncio
import json
import sys
from datetime import time, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.clock import at_ct

from test_crew_desks import D, make
from test_crew_review import online

BAD = {"headline": None, "bias": "very bullish", "confidence": "high", "size_multiplier": None,
       "cooldown_minutes": "ten", "notes": "one note", "events": ["CPI 07:30", {"name": "FOMC", "time_ct": "25:99"},
                                                                    {"name": "PPI", "time_ct": "07:30", "impact": "high"}],
       "proposals": ["tighten stops", {"scope": "day", "title": "ok", "params": {"exits.stop_loss_pct": 0.15}}]}


def test_finish_coerces_a_malformed_brief():
    e, c = make()
    b = c._finish(json.loads(json.dumps(BAD)), "macro")
    assert b["headline"] == "" and b["bias"] == "neutral" and b["confidence"] == 0.0
    assert b["size_multiplier"] == 1.0 and b["cooldown_minutes"] == 0 and b["notes"] == ["one note"]
    assert b["events"] == [{"name": "PPI", "time_ct": "07:30", "impact": "high"}]
    assert [p["title"] for p in b["proposals"]] == ["ok"]


def test_malformed_midday_reply_still_applies_and_size_up_runs(monkeypatch):
    e, c = online(monkeypatch, json.dumps(BAD), now=at_ct(D, time(11, 30)))
    e.apply_tweak = lambda item, now: None
    e.vwap = SimpleNamespace(value=764.0)
    asyncio.run(c._consult("midday", ["macro"], at_ct(D, time(11, 30))))
    assert "PPI" in c.directive["blackouts"]
    mult, checks = c.size_up(at_ct(D, time(11, 31)), "SWING", 765.0)       # formats every desk's confidence
    assert mult == 1.0 and checks


def test_new_day_drops_yesterdays_briefs_and_directive():
    e, c = make(now=at_ct(D, time(15, 5)))
    c.briefs["macro"] = {"headline": "FOMC at 13:00 CT. Flat into it.", "day": str(D), "size_multiplier": 0.75}
    c.directive = {"size_mult": 0.75, "blackouts": ["FOMC"], "summary": "Size 75%. Blackouts: FOMC.", "votes": {"macro": 0.75}}
    c.conviction = {"mult": 1.25, "checks": [{"name": "x", "ok": True}], "ts": at_ct(D, time(10, 0))}
    nxt = at_ct(D + timedelta(days=1), time(7, 0))
    e.feed.t = nxt
    asyncio.run(c.on_clock(nxt))
    assert c.briefs == {} and c.directive["blackouts"] == [] and "FOMC" not in c.directive["summary"]
    assert c.conviction["checks"] == []
