"""Paper broker: fills marketable limits against the current quote."""
from __future__ import annotations

import itertools

from .base import Broker, OrderResult

_ids = itertools.count(1)


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, quotes, slippage: float = 0.0):
        self.quotes = quotes
        self.slippage = slippage

    async def submit(self, contract, side: str, qty: int, limit: float, now: float) -> OrderResult:
        q = await self.quotes.quote(contract)
        if q is None:
            return OrderResult("rejected", message="no quote")
        oid = f"paper-{next(_ids)}"
        if side == "buy" and limit >= q.ask:
            return OrderResult("filled", qty, round(q.ask + self.slippage, 2), oid)
        if side == "sell" and limit <= q.bid:
            return OrderResult("filled", qty, round(max(0.0, q.bid - self.slippage), 2), oid)
        # a limit inside the spread gets filled half the time the market trades through it;
        # model conservatively: only if within one cent of the touch
        if side == "buy" and q.ask - limit <= 0.01:
            return OrderResult("filled", qty, round(limit, 2), oid)
        if side == "sell" and limit - q.bid <= 0.01:
            return OrderResult("filled", qty, round(limit, 2), oid)
        return OrderResult("unfilled", 0, 0.0, oid, f"limit {limit:.2f} vs {q.bid:.2f}/{q.ask:.2f}")
