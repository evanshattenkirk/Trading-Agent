"""Prior VIX close for book D (strikes) from Robinhood's index data (read-only), or the simulator's IV."""
from __future__ import annotations

from datetime import datetime, time, timedelta


class SimVix:
    def __init__(self, feed):
        self.feed = feed

    async def prior_close(self, day) -> float | None:
        return round(getattr(self.feed, "base_iv", 0.16) * 100, 2)

    async def current(self) -> float | None:
        return await self.prior_close(None)


class RobinhoodVix:
    def __init__(self, rh):
        self.rh, self._id = rh, None

    async def _vix_id(self) -> str | None:
        from ..brokers.robinhood import dict_items
        await self.rh.start()
        if self._id is None:
            data = _unwrap(await self.rh.call("get_indexes", {"symbols": "VIX"}))
            rows = dict_items(data.get("indexes", data) if isinstance(data, dict) else data)
            ids = [r.get("id") for r in rows if r.get("symbol") == "VIX" and r.get("id")]
            if not ids:
                return None
            self._id = ids[0]
        return self._id

    async def current(self) -> float | None:
        """VIX now, from get_index_quotes (read-only). Robinhood lists VIX but not VIX1D (checked 2026-09-29)."""
        from ..brokers.robinhood import dict_items
        vid = await self._vix_id()
        if vid is None:
            return None
        data = _unwrap(await self.rh.call("get_index_quotes", {"instrument_ids": [vid]}))
        rows = dict_items(data.get("quotes", data) if isinstance(data, dict) else data)
        for r in rows:
            if r.get("instrument_id") == vid and r.get("value") not in (None, ""):
                return float(r["value"])
        return None

    async def prior_close(self, day) -> float | None:
        if await self._vix_id() is None:
            return None
        start = (datetime.combine(day, time()) - timedelta(days=10)).strftime("%Y-%m-%dT00:00:00Z")
        data = _unwrap(await self.rh.call("get_index_historicals", {"instrument_ids": [self._id], "start_time": start, "interval": "day"}))
        res = data.get("results", []) if isinstance(data, dict) else []
        best = None
        for b in (res[0].get("bars", []) if res else []):
            if b.get("interpolated") or b.get("close_value") is None:
                continue
            if str(b.get("begins_at", ""))[:10] < str(day):
                best = float(b["close_value"])
        return best


def _unwrap(data):
    """Some Robinhood MCP replies put the payload under "data"."""
    return data["data"] if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
