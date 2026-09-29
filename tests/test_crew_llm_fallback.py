"""Online desks whose LLM reply has no usable brief fall back to the offline read, say so honestly, and ask for
enough output room that a thinking model with web search can finish the JSON."""
import asyncio
import sys
from datetime import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.clock import at_ct
from agentdesk.crew import DESK_MAX_TOKENS
from test_crew_desks import D, make


class FakeMessages:
    def __init__(self, text, stop_reason):
        self.text, self.stop_reason, self.calls = text, stop_reason, []

    async def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)], stop_reason=self.stop_reason,
                               usage=SimpleNamespace(input_tokens=10, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))


def online(monkeypatch, text, stop_reason):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    e, c = make(now=at_ct(D, time(8, 15)), sim=False)
    assert not c.offline
    c._client = SimpleNamespace(messages=FakeMessages(text, stop_reason))
    return e, c


def test_truncated_reply_falls_back_with_an_honest_label(monkeypatch):
    e, c = online(monkeypatch, '{"headline": "JOLTS at 09:00", "notes": ["cut off mid', "max_tokens")
    b = asyncio.run(c._brief("macro", "premarket", e.feed.now()))
    assert "live research unavailable" in b["headline"]
    assert "set ANTHROPIC_API_KEY" not in b["headline"]
    warns = [kw["msg"] for kind, _, kw in e.bus.events if kind == "log" and kw.get("level") == "warn"]
    assert any("stop_reason=max_tokens" in m for m in warns)


def test_desk_calls_leave_room_for_thinking_and_search(monkeypatch):
    e, c = online(monkeypatch, '{"headline": "Quiet day", "bias": "neutral", "confidence": 0.5}', "end_turn")
    b = asyncio.run(c._brief("macro", "premarket", e.feed.now()))
    assert b["headline"] == "Quiet day"
    assert c._client.messages.calls[0]["max_tokens"] == DESK_MAX_TOKENS >= 16000
