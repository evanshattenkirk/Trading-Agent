"""Shared fakes for the books tests."""
import sys
from datetime import date, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.bars import VWAP
from agentdesk.brokers.paper import PaperBroker
from agentdesk.clock import at_ct
from agentdesk.config import load_config
from agentdesk.exits import Contract
from agentdesk.feeds.base import Quote, QuoteSource
from agentdesk.journal import Journal
from agentdesk.risk import RiskManager

FILLS = {"model": "mid_offset", "cents_per_leg": 1, "reprice_step_c": 1, "max_reprices": 4,
         "max_quote_age_s": 5, "max_leg_spread_pct": 0.25, "tick_exempt": 0.02}


class FakeQuotes(QuoteSource):
    name = "fake"

    def __init__(self, now: float = 100.0):
        self.book: dict = {}
        self.now = now
        self.calls = 0

    def set(self, right, strike, bid, ask, ts=None, expiry=None):
        """expiry=None quotes every expiry at that strike; an expiry quotes only that one (calendars)."""
        self.book[(right, float(strike)) + ((expiry,) if expiry else ())] = (bid, ask, ts)

    async def quote(self, c):
        self.calls += 1
        v = self.book.get((c.right, float(c.strike), c.expiry)) or self.book.get((c.right, float(c.strike)))
        if v is None:
            return None
        b, a, ts = v
        return Quote(b, a, self.now if ts is None else ts)


def contracts(legs, expiry="2026-09-28"):
    return [Contract("SPY", expiry, float(l.strike), l.right) for l in legs]


DAY = date(2026, 9, 28)


def ct_ts(h, m, s=0, day=DAY):
    return at_ct(day, time(h, m, s))


class FakeFeed:
    is_sim, name = False, "fake"

    def __init__(self):
        self.t = ct_ts(8, 30)

    def now(self):
        return self.t

    async def history_1m(self, days):
        return []


class RecBus:
    def __init__(self):
        self.events = []

    def emit(self, type_, ts, **data):
        self.events.append((type_, ts, data))

    def of(self, type_):
        return [d for t, _, d in self.events if t == type_]


class FakeEngine:
    def __init__(self, quotes, cfg=None):
        self.cfg = cfg or load_config()
        self.quotes, self.broker = quotes, PaperBroker(quotes)
        self.bus, self.journal = RecBus(), Journal(None)
        self.risk = RiskManager(self.cfg)
        self.price, self.vwap = 765.0, VWAP()
        self.vwap.add(765.0, 1)
        self.open, self.crew, self.symbol, self.mode, self.feed = [], None, "SPY", "paper", FakeFeed()
