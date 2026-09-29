"""Book F2: single-name call and put debit spreads (research/strategy_f2_prereg.md). Paper only.

Two pre-registered setups on the option-liquid names in `books.F2_debit_spreads.universe`:
  C  call debit spread on an opening-range breakout: a green first 5-minute candle with RVOL5 >= 2 (from F1's 09:35
     scan), bought when the stock trades above the OR high before 10:30 ET. Held up to 3 trading days.
  P  put debit spread fading a high-volume up day: at 15:40 ET, a name up >= 3% on the day whose volume so far is
     >= 1.8 x its 20-day average daily volume. Held to the next trading day's 15:40 ET.
Structure: the nearest expiry 5-12 calendar days out; long leg at the money, short leg about one straddle-width
out (1 standard deviation to expiry), at least 2 strike steps; skip if the debit is above 60% of the width.
Exits: take profit at 2 x the debit or 80% of the width, stop at 0.5 x the debit, C's first-day stop when the stock
trades below the OR low, and the time exit. The bought leg is listed first, so a shadow review goes out as a debit.
Pure functions only; F2Host (f2_host.py) drives them.
"""
from __future__ import annotations

import math
from datetime import date, time, timedelta

from ..config import hhmm
from .base import ExitIntent, Leg

KEY = "F2_debit_spreads"
BOOK = "F2"
CALL, PUT = "C", "P"
RIGHT = {CALL: "call", PUT: "put"}
SETUP = {CALL: "CALL SPREAD", PUT: "PUT SPREAD"}


# ------------------------------------------------------------------ calendar
def is_session(d: date, holidays: set) -> bool:
    return d.weekday() < 5 and d not in holidays


def add_sessions(d: date, n: int, holidays: set) -> date:
    """The n-th trading day after d (n=0: d itself)."""
    while n > 0:
        d += timedelta(days=1)
        if is_session(d, holidays):
            n -= 1
    return d


def exit_day(entry_day: date, setup: str, holidays: set, cfg: dict) -> date:
    """C holds to day `hold_days_c` (the entry day is day 1); P to the next trading day."""
    return add_sessions(entry_day, int(cfg["hold_days_c"]) - 1 if setup == CALL else 1, holidays)


def exit_time_et(setup: str, cfg: dict, half_day: bool) -> time:
    t = hhmm(cfg["exit_et_c"] if setup == CALL else cfg["exit_et_p"])
    return min(t, time(12, 45)) if half_day else t


# ------------------------------------------------------------------ signals
def call_signals(rows, universe: set, cfg: dict) -> list:
    """F1 scan rows (F.ScanRow) on F2's names: green first candle, RVOL5 >= rvol5_min, top max_armed_c by RVOL5."""
    lo = cfg["rvol5_min"]
    ok = [r for r in rows if r.symbol in universe and r.rvol5 is not None and r.rvol5 >= lo and r.direction == "green"]
    return sorted(ok, key=lambda r: (-r.rvol5, r.symbol))[:int(cfg["max_armed_c"])]


def day_stats(daily: list[dict], today_bars: list[dict]) -> dict | None:
    """Today's change vs the prior close and today's volume vs the 20-day average daily volume, from daily bars
    through yesterday and today's 1-minute bars so far."""
    if len(daily) < 20 or not today_bars:
        return None
    prev, avg20 = daily[-1]["c"], sum(b["v"] for b in daily[-20:]) / 20
    last, vol = today_bars[-1]["c"], sum(b["v"] for b in today_bars)
    if prev <= 0 or avg20 <= 0:
        return None
    return {"chg_pct": round((last / prev - 1) * 100, 3), "vol_ratio": round(vol / avg20, 3), "last": last,
            "prev_close": prev}


def put_signals(stats: dict[str, dict], cfg: dict) -> list[tuple[str, dict]]:
    """Names up >= up_pct_p with volume so far >= vol_ratio_p x the 20-day average, top max_new_p by the move."""
    ok = [(s, x) for s, x in stats.items() if x and x["chg_pct"] >= cfg["up_pct_p"] and x["vol_ratio"] >= cfg["vol_ratio_p"]]
    return sorted(ok, key=lambda sx: (-sx[1]["chg_pct"], sx[0]))[:int(cfg["max_new_p"])]


def earnings_conflict(symbol: str, calendar: list[dict] | None, entry: date, exit_: date) -> str | None:
    """A report between the entry day and the exit day (both included) means the spread would hold through it."""
    for r in calendar or []:
        if r.get("symbol") == symbol and entry <= r["date"] <= exit_:
            return f"reports {r['date']} {r.get('timing') or ''}".strip() + " inside the hold"
    return None


# ------------------------------------------------------------------ structure
def pick_expiry(expiries, today: date, cfg: dict) -> date | None:
    lo, hi = cfg["dte"]
    ok = sorted(e for e in expiries if lo <= (e - today).days <= hi)
    return ok[0] if ok else None


def spread_strikes(strikes, long_k: float, right: str, width_target: float, min_steps: int) -> float | None:
    """The short strike: the listed strike nearest to long_k +/- width_target (up for calls, down for puts), at
    least min_steps listed strikes away from the long strike. None when the chain is too short."""
    ks = sorted(strikes)
    if long_k not in ks:
        return None
    i = ks.index(long_k)
    side = ks[i + 1:] if right == "call" else list(reversed(ks[:i]))
    if len(side) < min_steps:
        return None
    target = long_k + width_target if right == "call" else long_k - width_target
    best = min(side, key=lambda k: (abs(k - target), abs(k - long_k)))
    return best if side.index(best) + 1 >= min_steps else side[min_steps - 1]


def legs_for(setup: str, long_k: float, short_k: float, dte: int) -> list[Leg]:
    r = RIGHT[setup]
    return [Leg(r, long_k, "buy", 1, dte), Leg(r, short_k, "sell", 1, dte)]     # bought leg first: a debit review


def structure_problem(debit: float, width: float, cfg: dict) -> str | None:
    if debit <= 0:
        return f"no debit ({debit:.2f})"
    if width <= 0:
        return "zero width"
    if debit > cfg["max_debit_width"] * width:
        return f"debit {debit:.2f} is {debit / width:.0%} of the {width:g} width > {cfg['max_debit_width']:.0%}"
    return None


def lots_for(debit: float, cfg: dict) -> tuple[int, str]:
    per = debit * 100
    n = math.floor(cfg["max_debit"] / per + 1e-9) if per > 0 else 0
    return n, f"{n} x {debit:.2f} debit (max ${cfg['max_debit']:g} per position)"


def tp_stop(entry: float, mark: float, width: float, cfg: dict) -> ExitIntent | None:
    if mark >= entry * cfg["take_profit_x"]:
        return ExitIntent(f"take profit: {mark:.2f} >= {cfg['take_profit_x']:g} x {entry:.2f}")
    if mark >= cfg["take_profit_width"] * width:
        return ExitIntent(f"take profit: {mark:.2f} >= {cfg['take_profit_width']:.0%} of the width")
    if mark <= entry * cfg["stop_x"]:
        return ExitIntent(f"stop: {mark:.2f} <= {cfg['stop_x']:g} x {entry:.2f}", urgent=True)
    return None
