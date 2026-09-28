"""Shared fakes for the books tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.exits import Contract
from agentdesk.feeds.base import Quote, QuoteSource

FILLS = {"model": "mid_offset", "cents_per_leg": 1, "reprice_step_c": 1, "max_reprices": 4,
         "max_quote_age_s": 5, "max_leg_spread_pct": 0.25, "tick_exempt": 0.02}


class FakeQuotes(QuoteSource):
    name = "fake"

    def __init__(self, now: float = 100.0):
        self.book: dict = {}
        self.now = now
        self.calls = 0

    def set(self, right, strike, bid, ask, ts=None):
        self.book[(right, float(strike))] = (bid, ask, ts)

    async def quote(self, c):
        self.calls += 1
        v = self.book.get((c.right, float(c.strike)))
        if v is None:
            return None
        b, a, ts = v
        return Quote(b, a, self.now if ts is None else ts)


def contracts(legs, expiry="2026-09-28"):
    return [Contract("SPY", expiry, float(l.strike), l.right) for l in legs]
