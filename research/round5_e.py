"""E1 and E2 (research/round5_prereg.md section 3): book E's pre-earnings straddle and calendar, replayed on real
ThetaData quotes for book E's 30 names with X1's EDGAR report times. Research only.

This first part holds the event rules the quote pull (`research/fetch_thetadata_e.py`) also uses: the report day D
and timing for each EDGAR row, T-n sessions, the exit day, the 15:45 ET clock and the two expiries.

Amendment 1 (2026-10-30, before any book E quote was pulled): for `dmh` rows the pre-registration says "D =
`entry_session` (the filing day)". In X1's file the filing day of a `dmh` row is `report_session`; `entry_session` is
the session before it. D is the filing day, as the parenthesis and the reason given ("this keeps every exit before
it") say, so D = `report_session` for `dmh`.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nyse_calendar import half_days, holidays  # noqa: E402

FB_UNTIL = date(2022, 6, 9)          # first session META's options traded under META
ENTRY_MINUTE, HALF_DAY_MINUTE = 15 * 60 + 45, 12 * 60 + 20


def is_session(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def t_minus(d: date, n: int) -> date:
    """The n-th session before session d (T-0 = d)."""
    while n > 0:
        d -= timedelta(days=1)
        if is_session(d):
            n -= 1
    return d


def sessions_between(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if is_session(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def clock_minute(d: date) -> int:
    """Entry and normal exit minute (ET minute of day): 15:45, or 12:20 on a 13:00 close."""
    return HALF_DAY_MINUTE if d in half_days(d.year) else ENTRY_MINUTE


def root(symbol: str, day: date) -> str:
    return "FB" if symbol == "META" and day < FB_UNTIL else symbol


def event(r: dict) -> dict | None:
    """One kept row of X1's earnings.csv -> {symbol, D, timing}; None for dropped rows."""
    if r["status"] != "kept":
        return None
    kind = r["timing"]
    if kind == "bmo":
        d, timing = r["report_session"], "am"
    elif kind == "amc":
        d, timing = r["entry_session"], "pm"
    elif kind == "dmh":
        d, timing = r["report_session"], "unknown"            # amendment 1: the filing day
    else:
        raise ValueError(f"unknown timing {kind!r}")
    return {"symbol": r["symbol"], "D": date.fromisoformat(d), "timing": timing}


def exit_day(d: date, timing: str) -> date:
    return d if timing == "pm" else t_minus(d, 1)


def post_expiry(listed, d: date, timing: str) -> date | None:
    later = [e for e in listed if (e >= d if timing == "am" else e > d)]
    return min(later) if later else None


def pre_expiry(listed, d: date, timing: str) -> date | None:
    before = [e for e in listed if (e <= d if timing == "pm" else e < d)]
    return max(before) if before else None
