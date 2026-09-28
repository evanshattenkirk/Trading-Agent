"""Strike selection: time-of-day schedule + nearest resistance.

offset +1 = first OTM strike, +2 = second OTM, 0 = ATM, -1 = first ITM (for calls;
mirrored for puts). SPY 0DTE strikes are $1 apart.

Rule:
  1. Look up (base, max) for the current CT time from the schedule.
  2. If max > base and the nearest resistance sits beyond the `max` strike + buffer,
     there is room to run -> go to `max`.
  3. If the nearest resistance is *below* the base strike - buffer (price has to break
     a ceiling before the strike comes into play) -> step one strike toward the money.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .clock import ct_time
from .config import hhmm


@dataclass
class StrikeChoice:
    strike: float
    offset: int
    reason: str


def strike_for(spot: float, offset: int, side: str) -> float:
    if side == "call":
        first_otm = math.floor(spot) + 1
        first_itm = math.ceil(spot) - 1
        atm = round(spot)
        return float(atm if offset == 0 else first_otm + offset - 1 if offset > 0 else first_itm + offset + 1)
    first_otm = math.ceil(spot) - 1
    first_itm = math.floor(spot) + 1
    atm = round(spot)
    return float(atm if offset == 0 else first_otm - (offset - 1) if offset > 0 else first_itm - (offset + 1))


def schedule_for(now: float, schedule: list[dict]) -> tuple[int, int]:
    t = ct_time(now)
    base, mx = schedule[0]["base"], schedule[0]["max"]
    for row in schedule:
        if t >= hhmm(row["from"]):
            base, mx = row["base"], row["max"]
    return base, mx


def choose_strike(spot: float, now: float, side: str, levels, vwap, cfg) -> StrikeChoice:
    base, mx = schedule_for(now, cfg["schedule"])
    buf = cfg["level_buffer"]
    if side == "call":
        walls = levels.resistance_above(spot, vwap)
        beyond = lambda lvl, k: lvl > k + buf
        inside = lambda lvl, k: lvl < k - buf
    else:
        walls = levels.support_below(spot, vwap)
        beyond = lambda lvl, k: lvl < k - buf
        inside = lambda lvl, k: lvl > k + buf

    nearest = walls[0] if walls else None
    offset, why = base, f"schedule {base:+d}"
    if nearest is None:
        if mx > base:
            offset, why = mx, "no level overhead, room to run"
    else:
        lvl, name = nearest
        if mx > base and beyond(lvl, strike_for(spot, mx, side)):
            offset, why = mx, f"next wall {name} {lvl:.2f} clears {strike_for(spot, mx, side):.0f}"
        elif base > 0 and inside(lvl, strike_for(spot, base, side)):
            offset, why = base - 1, f"{name} {lvl:.2f} caps {strike_for(spot, base, side):.0f}, stepping in"
        else:
            why = f"schedule {base:+d}, next wall {name} {lvl:.2f}"
    mo = cfg.get("max_offset")
    if mo is not None and offset > mo:
        offset, why = int(mo), why + f"; capped at {int(mo):+d} by crew tweak"
    return StrikeChoice(strike_for(spot, offset, side), offset, why)
