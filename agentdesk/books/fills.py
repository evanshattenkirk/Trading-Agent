"""Working a multi-leg order: start at mid, step toward natural, last try at natural. One ref_id per logical
order. In shadow/live the first price is also sent to review_option_order; the fill is always paper."""
from __future__ import annotations

import uuid

from ..brokers.base import OrderResult
from .combo import ComboQuote


def rh_legs(legs, ids, opening: bool) -> list[dict]:
    """Robinhood legs. Closing flips every side, so a credit structure closes as a debit (order_args reads the
    direction from the first leg)."""
    out = []
    for l, oid in zip(legs, ids):
        side = l.side if opening else ("buy" if l.side == "sell" else "sell")
        out.append({"option_id": oid, "side": side, "position_effect": "open" if opening else "close",
                    "ratio_quantity": l.ratio})
    return out


class ComboExecutor:
    def __init__(self, broker, fills: dict, reviewer=None):
        self.broker, self.f, self.reviewer = broker, fills, reviewer

    async def work(self, legs, contracts, qty: int, credit: bool, opening: bool, urgent: bool, now: float) -> OrderResult:
        ref = str(uuid.uuid4())
        n = sum(l.ratio for l in legs)
        step = self.f["reprice_step_c"] / 100 * n
        receiving = credit == opening
        review, tries = None, 0
        while True:
            qs = [await self.broker.quotes.quote(c) for c in contracts]
            if any(q is None for q in qs):
                return OrderResult("rejected", message="no quote on a leg", raw={"ref_id": ref, "tries": tries})
            cq = ComboQuote(legs, qs)
            mid, nat = cq.mid(credit), cq.natural(credit, opening)
            if urgent or tries >= self.f["max_reprices"]:
                limit = nat
            else:
                limit = mid - step * tries if receiving else mid + step * tries
                limit = max(limit, nat) if receiving else min(limit, nat)
            limit = round(limit, 2)
            if self.reviewer is not None and review is None:
                review = await self._review(legs, contracts, qty, limit, opening)
            res = await self.broker.submit_combo(legs, contracts, qty, limit, credit, opening, now)
            tries += 1
            res.raw = {**res.raw, "ref_id": ref, "limit": limit, "tries": tries}
            res.review = review
            if res.status != "unfilled" or res.filled_qty or limit == round(nat, 2):
                return res

    async def _review(self, legs, contracts, qty: int, limit: float, opening: bool) -> dict:
        from ..brokers.robinhood import _short, order_args
        try:
            ids = [await self.reviewer.instrument_id(c) for c in contracts]
            args = order_args(self.reviewer.account, rh_legs(legs, ids, opening), qty, limit, True, contracts[0].symbol)
            return _short(await self.reviewer.call("review_option_order", args))
        except Exception as ex:
            return {"error": str(ex)[:200]}
