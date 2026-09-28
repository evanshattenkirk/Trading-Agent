"""Book E's earnings screen (HANDOFF 7E), run by the Earnings desk at the 08:15 catch-up.

Counts trading days T from today to each announcement and flags the windows the book trades:
  E2  calendar entry, T-10 .. T-8
  E1  straddle entry at the close, T-3
  exit  at today's close: T-1 for pre-market ("am") reporters, T-0 for after-close ("pm") reporters.
        Unknown timing exits at T-1, the earlier of the two. Positions are never held through the announcement.
It only flags. Sizing, the spread/VIX/IV filters and orders belong to book E.
"""
from __future__ import annotations

from datetime import date, timedelta

HORIZON = 10          # the earliest window (E2) opens at T-10


def _day(v) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def parse_calendar(data) -> list[dict]:
    """Robinhood get_earnings_calendar results (verified 2026-09-28:
    {symbol, report: {date, timing: am|pm|"", verified}}) or config entries {symbol, date, timing}."""
    if isinstance(data, dict):
        data = data.get("results", data.get("data", []))
        if isinstance(data, dict):
            data = data.get("results", [])
    out = []
    for r in data or []:
        if not isinstance(r, dict) or not r.get("symbol"):
            continue
        rep = r.get("report") if isinstance(r.get("report"), dict) else r
        d = _day(rep.get("date"))
        if d is None:
            continue
        timing = str(rep.get("timing") or "").lower()
        out.append({"symbol": str(r["symbol"]).upper(), "date": d,
                    "timing": timing if timing in ("am", "pm") else "",
                    "verified": bool(rep.get("verified", True))})
    return out


def is_trading_day(d: date, holidays: set) -> bool:
    return d.weekday() < 5 and d not in holidays


def trading_days_between(d0: date, d1: date, holidays: set) -> int:
    """Trading days from d0 to d1: 0 on the same day, negative when d1 is earlier."""
    if d1 < d0:
        return -trading_days_between(d1, d0, holidays)
    n, d = 0, d0
    while d < d1:
        d += timedelta(days=1)
        n += is_trading_day(d, holidays)
    return n


def previous_trading_day(d: date, holidays: set) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d, holidays):
        d -= timedelta(days=1)
    return d


def _flag(t: int, timing: str) -> str | None:
    if 8 <= t <= 10:
        return "E2"
    if t == 3:
        return "E1"
    if (t == 0 and timing == "pm") or (t == 1 and timing in ("am", "")):
        return "exit"
    return None


def screen(calendar: list[dict], today: date, universe, holidays: set) -> list[dict]:
    """Universe names reporting within the next HORIZON trading days, soonest first, each with its window flag."""
    names = {s.upper() for s in universe}
    out = []
    for r in calendar:
        if r["symbol"] not in names:
            continue
        t = trading_days_between(today, r["date"], holidays)
        if t < 0 or t > HORIZON or (t == 0 and r["timing"] != "pm"):
            continue                     # already reported, or too far out
        out.append({**r, "T": t, "flag": _flag(t, r["timing"]), "tentative": not r["verified"]})
    return sorted(out, key=lambda r: (r["T"], r["symbol"]))


def heavyweights_overnight(calendar: list[dict], today: date, names, holidays: set) -> list[dict]:
    """SPY heavyweights that reported after the last close or before today's open: the open may gap."""
    want = {s.upper() for s in names}
    prev = previous_trading_day(today, holidays)
    return [r for r in calendar if r["symbol"] in want and
            ((r["date"] == prev and r["timing"] == "pm") or (r["date"] == today and r["timing"] == "am"))]
