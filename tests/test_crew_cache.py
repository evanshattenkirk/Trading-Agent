"""Explicit prompt-cache breakpoints on the crew's Claude calls (Evan, 2026-09-28).

Each call puts cache_control on its stable system block: tools render before system, so that one breakpoint caches
the web search tool definition plus the desk prompt. Everything that changes per call (time, SPY price, briefs,
engine stats, the F picks) stays in the user message after the breakpoint, and web search results get the
server's own cache writes because the request already uses caching.
"""
import asyncio
import copy
import json
import sys
from datetime import date, time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.books.f_news import NewsTagger
from agentdesk.clock import at_ct
from agentdesk.config import load_config
from agentdesk.crew import DESKS

from test_crew_desks import make

CFG = load_config()
D = date(2026, 9, 28)
EPHEMERAL = {"type": "ephemeral"}


class FakeMessages:
    def __init__(self, text='{"headline": "ok", "lines": [], "tags": []}', usage=None):
        self.calls, self.text, self.usage = [], text, usage

    async def create(self, **kw):
        self.calls.append(copy.deepcopy(kw))
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)], usage=self.usage)


def fake_client(**kw):
    return SimpleNamespace(messages=FakeMessages(**kw))


def breakpoints(kw) -> list[dict]:
    """Every cache_control marker in a request, in render order (tools, system, messages)."""
    out = [t["cache_control"] for t in kw.get("tools", []) if "cache_control" in t]
    system = kw.get("system")
    if isinstance(system, list):
        out += [b["cache_control"] for b in system if "cache_control" in b]
    for m in kw["messages"]:
        if isinstance(m["content"], list):
            out += [b["cache_control"] for b in m["content"] if "cache_control" in b]
    return out


def user_text(kw) -> str:
    c = kw["messages"][-1]["content"]
    return c if isinstance(c, str) else "".join(b.get("text", "") for b in c)


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------------- desk briefs
def test_desk_brief_caches_its_system_block():
    e, c = make(now=at_ct(D, time(8, 15)))
    c._client = fake_client()
    run(c._llm("macro", e.feed.now()))
    kw = c._client.messages.calls[0]
    assert isinstance(kw["system"], list) and len(kw["system"]) == 1
    block = kw["system"][-1]
    assert block["type"] == "text" and block["cache_control"] == EPHEMERAL
    assert "Macro desk" in block["text"] and '"headline"' in block["text"]
    assert breakpoints(kw) == [EPHEMERAL]                  # one marker, well under the API's 4


def test_desk_brief_prefix_is_identical_across_huddles_and_the_volatile_part_follows_it():
    e, c = make(now=at_ct(D, time(8, 15)))
    c._client = fake_client()
    run(c._llm("macro", e.feed.now()))
    e.price = 771.25
    run(c._llm("macro", at_ct(D, time(11, 30)), extra=json.dumps({"trades": 3})))
    a, b = c._client.messages.calls
    assert a["system"] == b["system"] and a.get("tools") == b.get("tools") and a["model"] == b["model"]
    assert "11:30" in user_text(b) and "771.25" in user_text(b) and '"trades": 3' in user_text(b)
    for kw in (a, b):
        cached = json.dumps(kw["system"]) + json.dumps(kw.get("tools", []))
        assert "08:15" not in cached and "11:30" not in cached and "771.25" not in cached


def test_web_desks_keep_web_search_ahead_of_the_system_breakpoint():
    e, c = make(now=at_ct(D, time(8, 15)))
    c._client = fake_client()
    run(c._llm("fed", e.feed.now()))
    kw = c._client.messages.calls[0]
    assert DESKS["fed"].uses_web and kw["tools"][0]["name"] == "web_search"
    assert "cache_control" not in kw["tools"][0]          # covered by the system marker (tools render first)
    assert kw["system"][-1]["cache_control"] == EPHEMERAL


def test_each_desk_gets_its_own_cached_prefix():
    e, c = make(now=at_ct(D, time(8, 15)))
    c._client = fake_client()
    for k in ("macro", "rates", "quant"):
        run(c._llm(k, e.feed.now()))
    systems = [kw["system"][0]["text"] for kw in c._client.messages.calls]
    assert len(set(systems)) == 3
    assert all(kw["system"][0]["cache_control"] == EPHEMERAL for kw in c._client.messages.calls)


# ----------------------------------------------------------------------------- roundtable
def test_roundtable_caches_its_system_block_and_keeps_briefs_after_it():
    e, c = make(now=at_ct(D, time(8, 25)))
    c._client = fake_client()
    c.briefs = {"macro": {"headline": "CPI 07:30", "bias": "bullish"}, "rates": {"headline": "2y flat", "bias": "neutral"}}
    run(c._llm_roundtable("premarket", ["macro", "rates"], e.feed.now()))
    e.price = 770.0
    c.briefs["macro"]["headline"] = "CPI hot"
    run(c._llm_roundtable("midday", ["macro", "rates"], at_ct(D, time(11, 30))))
    a, b = c._client.messages.calls
    assert a["system"] == b["system"] and a["system"][-1]["cache_control"] == EPHEMERAL
    assert breakpoints(a) == [EPHEMERAL]
    assert "CPI hot" in user_text(b) and "midday" in user_text(b)
    assert "CPI" not in json.dumps(a["system"])


# ----------------------------------------------------------------------------- book F news tag
def test_f_news_tag_caches_its_system_block():
    e, c = make(now=at_ct(D, time(8, 35)))
    c._client = fake_client()
    e.crew = c
    tagger = NewsTagger(e, CFG)
    pick = SimpleNamespace(symbol="NVDA", rvol5=3.2, open=180.0, close=182.5, picked=True)
    run(tagger._ask(e.feed.now(), [pick], []))
    kw = c._client.messages.calls[0]
    assert kw["system"][-1]["cache_control"] == EPHEMERAL and breakpoints(kw) == [EPHEMERAL]
    assert "NVDA" in user_text(kw) and "NVDA" not in json.dumps(kw["system"])
    assert kw["tools"][0]["name"] == "web_search"


# ----------------------------------------------------------------------------- cache usage
def test_cache_usage_is_tallied_so_hits_can_be_checked():
    e, c = make(now=at_ct(D, time(8, 15)))
    usage = SimpleNamespace(input_tokens=120, cache_read_input_tokens=900, cache_creation_input_tokens=0, output_tokens=80)
    c._client = fake_client(usage=usage)
    run(c._llm("macro", e.feed.now()))
    run(c._llm_roundtable("premarket", ["macro", "rates"], e.feed.now()))
    assert c.cache_usage == {"calls": 2, "input": 240, "cache_read": 1800, "cache_write": 0}
    assert c.state()["cache_usage"]["cache_read"] == 1800


def test_missing_usage_does_not_break_a_brief():
    e, c = make(now=at_ct(D, time(8, 15)))
    c._client = fake_client(usage=None)
    assert run(c._llm("macro", e.feed.now())) == {"headline": "ok", "lines": [], "tags": []}
    assert c.cache_usage["calls"] == 1 and c.cache_usage["cache_read"] == 0
