"""MassiveQuotes stamps a quote with the quote's own time, so the stale-quote watchdog sees an old quote as old
(Evan, 2026-10-01, sweep item 13)."""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.config import load_config
from agentdesk.exits import Contract
from agentdesk.feeds.massive import MassiveQuotes


class Client:
    def __init__(self, lq):
        self.lq = lq

    async def get(self, url, params=None):
        lq = self.lq
        return type("R", (), {"status_code": 200, "json": lambda s: {"results": {"last_quote": lq}}})()


def quote(lq, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    q = MassiveQuotes(load_config())
    q.client = Client(lq)
    return asyncio.run(q.quote(Contract("SPY", "2026-10-01", 660.0, "call")))


@pytest.mark.parametrize("field,scale", [("last_updated", 1e9), ("sip_timestamp", 1e9), ("last_updated", 1e3)])
def test_the_quote_carries_its_own_timestamp(monkeypatch, field, scale):
    ts = time.time() - 45
    q = quote({"bid": 1.0, "ask": 1.1, field: int(ts * scale)}, monkeypatch)
    assert q.bid == 1.0 and q.ts == pytest.approx(ts, abs=0.01)


def test_a_quote_without_a_timestamp_falls_back_to_now(monkeypatch):
    q = quote({"bid": 1.0, "ask": 1.1}, monkeypatch)
    assert abs(q.ts - time.time()) < 2
