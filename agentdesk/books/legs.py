"""Leg quotes for a combo: one batched get_option_quotes call on Robinhood, per-leg quotes elsewhere."""
from __future__ import annotations

import time

from ..feeds.base import Quote


async def fetch_quotes(quotes, contracts) -> list:
    rh = getattr(quotes, "rh", None)
    if rh is None or len(contracts) < 2:
        return [await quotes.quote(c) for c in contracts]
    from ..brokers.robinhood import dict_items, find_key
    ids = [await rh.instrument_id(c) for c in contracts]
    if not all(ids):
        return [await quotes.quote(c) for c in contracts]
    data = await rh.call("get_option_quotes", {"instrument_ids": ids})
    now, by = time.time(), {}
    for item in dict_items(data):
        q = item.get("quote") if isinstance(item.get("quote"), dict) else item   # results[].quote on Robinhood
        oid = str(item.get("instrument_id") or q.get("instrument_id") or find_key(item, ["instrument_id", "id"]) or "")
        b = q.get("bid_price", q.get("bid"))
        a = q.get("ask_price", q.get("ask"))
        if oid and b is not None and a is not None:
            by[oid] = Quote(float(b), float(a), now)
    cache = getattr(quotes, "cache", None)
    if cache is not None:
        cache.update(by)             # the paper fill right after reads these instead of re-fetching
    return [by.get(i) for i in ids]
