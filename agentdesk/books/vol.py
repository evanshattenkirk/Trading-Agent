"""Prior VIX close for book D (strikes) from Robinhood's index data (read-only), or the simulator's IV."""
from __future__ import annotations

from datetime import datetime, time, timedelta


class SimVix:
    def __init__(self, feed):
        self.feed = feed

    async def prior_close(self, day) -> float | None:
        return round(getattr(self.feed, "base_iv", 0.16) * 100, 2)


class RobinhoodVix:
    def __init__(self, rh):
        self.rh, self._id = rh, None

    async def prior_close(self, day) -> float | None:
        from ..brokers.robinhood import dict_items
        await self.rh.start()
        if self._id is None:
            data = await self.rh.call("get_indexes", {"symbols": "VIX"})
            rows = dict_items(data.get("indexes", data) if isinstance(data, dict) else data)
            ids = [r.get("id") for r in rows if r.get("symbol") == "VIX" and r.get("id")]
            if not ids:
                return None
            self._id = ids[0]
        start = (datetime.combine(day, time()) - timedelta(days=10)).strftime("%Y-%m-%dT00:00:00Z")
        data = await self.rh.call("get_index_historicals", {"instrument_ids": [self._id], "start_time": start, "interval": "day"})
        res = data.get("results", []) if isinstance(data, dict) else []
        best = None
        for b in (res[0].get("bars", []) if res else []):
            if b.get("interpolated") or b.get("close_value") is None:
                continue
            if str(b.get("begins_at", ""))[:10] < str(day):
                best = float(b["close_value"])
        return best
