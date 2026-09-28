"""Shared fakes for the book F tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CFG = {"enabled": True, "paper_only": True, "instrument": "shares", "scan_et": "09:35", "rvol5_min": 2.0, "top_n": 5,
       "first_candle": "green", "entry_cutoff_et": "10:30", "stop_atr_frac": 0.10, "exit_et": "15:55",
       "risk_per_trade": 25, "max_notional": 1000, "max_positions": 5, "daily_loss": 75,
       "universe": {"min_price": 10, "min_atr": 0.50, "min_dollar_vol_20d": 100_000_000, "top_sp500_by_dollar_vol": 130,
                    "extra": ["NVDA", "AMD", "AVGO", "MU", "TSM", "ARM", "MRVL", "SMCI", "WDC", "STX", "MSFT", "META",
                              "GOOGL", "AMZN", "AAPL", "ORCL", "PLTR", "TSLA"]},
       "news_tag": "observe", "shorts": "log_only"}

from agentdesk.books.f_stocks_in_play import Q


class FakeEquityQuotes:
    def __init__(self, now: float = 1000.0):
        self.book, self.now, self.calls = {}, now, 0

    def set(self, sym, bid, ask, ts=None, last=None):
        self.book[sym] = Q(bid, ask, last if last is not None else round((bid + ask) / 2, 4),
                           self.now if ts is None else ts)

    async def quote(self, sym):
        self.calls += 1
        return self.book.get(sym)

    async def quotes(self, syms):
        self.calls += 1
        return {s: self.book[s] for s in syms if s in self.book}


class FakeRH:
    """Records tool calls. order_states: successive (state, filled qty, avg price) answers to get_equity_orders."""

    def __init__(self, order_states=None, positions=None):
        self.calls, self.states, self.positions = [], list(order_states or []), positions or []
        self.account = "123456"
        self.tools = {}

    async def call(self, tool, args):
        self.calls.append((tool, dict(args)))
        if tool == "review_equity_order":
            return {"alerts": []}
        if tool == "place_equity_order":
            return {"id": "ord-1", "state": "queued"}
        if tool == "get_equity_orders":
            st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"orders": [{"id": "ord-1", "state": st[0], "cumulative_quantity": str(st[1]),
                                "average_price": str(st[2])}]}
        if tool == "cancel_equity_order":
            return {}
        if tool == "get_equity_positions":
            return {"positions": self.positions}
        raise AssertionError(f"unexpected tool {tool}")
