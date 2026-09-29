"""Paper broker: fills marketable limits against the current quote."""
from __future__ import annotations

import itertools

from .base import Broker, OrderResult

_ids = itertools.count(1)


class PaperBroker(Broker):
    name = "paper"
    combo_model = "mid_offset"      # mid_offset | natural (books.fills.model)
    combo_cents = 0.01              # $ per leg off mid (mid_offset)
    combo_frac = 0.0                # share of the way from mid to natural (mid_frac)

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

    async def submit_combo(self, legs, contracts, qty: int, limit: float, credit: bool, opening: bool, now: float) -> OrderResult:
        """Multi-leg paper fill. The fair price is mid -/+ combo_cents per leg, never worse than natural; a limit at
        or through it fills there, a less aggressive one rests unfilled. All-or-nothing."""
        from ..books.combo import ComboQuote, paper_fair
        qs = [await self.quotes.quote(c) for c in contracts]
        if any(q is None for q in qs):
            return OrderResult("rejected", message="no quote on a leg")
        cq = ComboQuote(legs, qs)
        fair = paper_fair(cq, credit, opening, self.combo_model, self.combo_cents, self.combo_frac)
        receiving = credit == opening
        marketable = limit <= fair + 1e-9 if receiving else limit >= fair - 1e-9
        oid = f"paper-{next(_ids)}"
        extra = {"mid": cq.mid(credit), "natural": cq.natural(credit, opening)}
        if marketable:
            return OrderResult("filled", qty, fair, oid, raw=extra)
        return OrderResult("unfilled", 0, 0.0, oid, f"limit {limit:.2f} vs fair {fair:.2f}", raw=extra)
