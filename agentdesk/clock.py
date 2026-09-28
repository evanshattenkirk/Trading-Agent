"""Market-time helpers. All strategy times are US/Central."""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
RTH_OPEN = time(8, 30)
RTH_CLOSE = time(15, 0)


def ct(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, CT)


def ct_time(ts: float) -> time:
    return ct(ts).time()


def is_rth(ts: float) -> bool:
    d = ct(ts)
    return d.weekday() < 5 and RTH_OPEN <= d.time() < RTH_CLOSE


def session_date(ts: float) -> date:
    return ct(ts).date()


def at_ct(d: date, t: time) -> float:
    return datetime.combine(d, t, CT).timestamp()


def hm(ts: float) -> str:
    return ct(ts).strftime("%H:%M:%S")
