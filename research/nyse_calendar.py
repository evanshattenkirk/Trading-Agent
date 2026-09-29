"""NYSE full-day closures and 13:00 ET early closes, 2016 onward. Standard library only, so research scripts can use
it without ThetaData or gRPC installed. The closure rules are the same as fetch_thetadata_spy0dte.nyse_holidays."""
from __future__ import annotations

from datetime import date, timedelta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date | None:
    """Saturday -> Friday, Sunday -> Monday; NYSE doesn't close the Friday before a Saturday New Year's Day."""
    if d.weekday() == 5:
        return None if (d.month, d.day) == (1, 1) else d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return date(y, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)


def holidays(year: int) -> set[date]:
    days = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),                # MLK
        _nth_weekday(year, 2, 0, 3),                # Presidents
        _easter(year) - timedelta(days=2),          # Good Friday
        _last_weekday(year, 5, 0),                  # Memorial
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),                # Labor
        _nth_weekday(year, 11, 3, 4),               # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))      # Juneteenth
    days |= {d for d in (date(2018, 12, 5), date(2025, 1, 9)) if d.year == year}     # national days of mourning
    return {d for d in days if d is not None}


def half_days(year: int) -> set[date]:
    """13:00 ET closes: July 3 and December 24 when they're weekdays and not closures, and the day after
    Thanksgiving."""
    closed = holidays(year)
    out = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1)}
    out |= {d for d in (date(year, 7, 3), date(year, 12, 24)) if d.weekday() < 5 and d not in closed}
    return out


def holidays_between(start_year: int, end_year: int) -> set[date]:
    return set().union(*(holidays(y) for y in range(start_year, end_year + 1)))


def half_days_between(start_year: int, end_year: int) -> set[date]:
    return set().union(*(half_days(y) for y in range(start_year, end_year + 1)))
