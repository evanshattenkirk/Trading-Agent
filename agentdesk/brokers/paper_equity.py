"""Paper fills for book F's whole-share orders (docs/BOOK_F_HANDOFF.md section 4.1).

Buys fill at max(trigger price, ask) + 1 bp, if that is within the limit. Sells fill at min(stop, bid) - 1 bp (the
bid - 1 bp without a stop), if that is at or above the limit. A quote older than 5 s, or none, rejects the order
(fail closed). On 1-minute bars: a bar that gaps through the stop fills at its open; see bar_stop().
"""
from __future__ import annotations

import itertools

from ..books import f_stocks_in_play as F
from .base import OrderResult

_ids = itertools.count(1)


class PaperEquityBroker:
    name = "paper-equity"
    live = False

    def __init__(self, quotes, max_age: float = 5.0, slip_bp: float = 1.0):
        self.quotes, self.max_age, self.slip_bp = quotes, max_age, slip_bp

    async def _fresh(self, sym: str, now: float):
        q = await self.quotes.quote(sym)
        if q is None:
            return None, "no quote"
        if now - q.ts > self.max_age:
            return None, f"stale quote ({now - q.ts:.1f}s old)"
        return q, ""

    async def buy(self, sym: str, qty: int, limit: float, trigger: float, now: float) -> OrderResult:
        q, why = await self._fresh(sym, now)
        oid = f"paper-eq-{next(_ids)}"
        if q is None:
            return OrderResult("rejected", message=why, order_id=oid)
        px = round(F._up(max(trigger, q.ask), self.slip_bp), 4)
        raw = {"bid": q.bid, "ask": q.ask, "limit": limit}
        if px > limit + 1e-9:
            return OrderResult("unfilled", 0, 0.0, oid, f"ask {q.ask:.2f} through limit {limit:.2f}", raw=raw)
        return OrderResult("filled", qty, px, oid, raw=raw)

    async def sell(self, sym: str, qty: int, limit: float, now: float, stop: float | None = None) -> OrderResult:
        q, why = await self._fresh(sym, now)
        oid = f"paper-eq-{next(_ids)}"
        if q is None:
            return OrderResult("rejected", message=why, order_id=oid)
        px = round(F._dn(min(stop, q.bid) if stop is not None else q.bid, self.slip_bp), 4)
        raw = {"bid": q.bid, "ask": q.ask, "limit": limit}
        if px < limit - 1e-9:
            return OrderResult("unfilled", 0, 0.0, oid, f"bid {q.bid:.2f} below limit {limit:.2f}", raw=raw)
        return OrderResult("filled", qty, px, oid, raw=raw)

    def bar_stop(self, bar: dict, stop: float) -> float | None:
        px = F.bar_stop_fill(bar, stop, self.slip_bp)
        return None if px is None else round(px, 4)
