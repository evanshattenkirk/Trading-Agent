"""Leg quotes for a combo: one batched get_option_quotes call on Robinhood (legs quoted within the quote source's
max_age are reused), per-leg quotes elsewhere."""
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
    cache = getattr(quotes, "cache", None)
    max_age = getattr(quotes, "max_age", 0) if cache is not None else 0
    now, by = time.time(), {}
    for i in ids:                   # a quote fetched this instant (the host's batched tick) is reused, not re-asked
        q = cache.get(i) if max_age else None
        if q is not None and now - q.ts < max_age:
            by[i] = q
    need = list(dict.fromkeys(i for i in ids if i not in by))
    if not need:
        return [by[i] for i in ids]
    data = await rh.call("get_option_quotes", {"instrument_ids": need})
    now = time.time()
    for item in dict_items(data):
        q = item.get("quote") if isinstance(item.get("quote"), dict) else item   # results[].quote on Robinhood
        oid = str(item.get("instrument_id") or q.get("instrument_id") or find_key(item, ["instrument_id", "id"]) or "")
        b = q.get("bid_price", q.get("bid"))
        a = q.get("ask_price", q.get("ask"))
        if oid and a is not None:           # no bid on a far wing means nobody bids: 0, not "no quote"
            by[oid] = Quote(float(b or 0), float(a), now)
    if cache is not None:
        cache.update({i: by[i] for i in need if i in by})             # the paper fill right after reads these instead of re-fetching
    return [by.get(i) for i in ids]
