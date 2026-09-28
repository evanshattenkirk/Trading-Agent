"""New strategy candidates F1-F4 (pre-registered in research/strategies_new_prereg.md before any result was seen).

  F1  afternoon iron condor, 13:30 -> 15:25 ET, shorts at +-0.9 EM, $2 wings, TP 50%, stop 2x credit
  F2  10:00 ET put credit spread (D's put side alone), plus the F2-trend variant (prior close > 50-day average)
  F3  0DTE / 1DTE ATM call calendar, 10:00 -> 15:25 ET, TP +25%, stop -35% of the debit
  F4  overnight put credit spread in next-day expiry, 15:25 ET -> 10:00 ET next day, TP 50%, stop 2x credit

Same data and option model as research/strategies_bcd.py (Black-Scholes, flat IV from the prior VIX close, sessions
re-based to SPY 765), extended with an overnight variance term for next-day legs. Nothing is fitted.
Fills per leg per side: "mid-1c" and "natural" (2c same-day legs, 3c next-day legs), plus $0.04 fees.

Usage (from the repo root, after research/fetch_data.sh and research/load_oanda.py):
    python research/strategies_new.py            # writes research/strategies_new_results.json and _report.md
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent

FEE = 0.04 / 100                      # $ per share per leg per side
M_RTH, M_ON = 0.80, 0.60              # priced sd of the regular session / overnight, in VIX-implied days
COSTS = {"mid-1c": {0: 0.01 + FEE, 1: 0.01 + FEE}, "natural": {0: 0.02 + FEE, 1: 0.03 + FEE}}
K_1000, K_1330, K_1525, K_CLOSE = 5, 47, 70, 77
REBASE = 765.0
CAP_PER_POSITION = 300.0              # HANDOFF section 11, $10k row: B, C, D max loss per position

N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))


def call(S, K, sd):
    if sd < 1e-9:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + 0.5 * sd * sd) / sd
    return S * N(d1) - K * N(d1 - sd)


def put(S, K, sd):
    return call(S, K, sd) - S + K


# ---------------------------------------------------------------- pricing

def rth_frac(k: int) -> float:
    """Share of a regular session's variance left after 5m bar k closes, plus SPY's 15-minute tail."""
    return max(0.0, (77 - k) / 78) + 15 / 390


def profile_frac(shares: np.ndarray):
    """rth_frac built from a per-bar variance profile (shares sum to 1) instead of the flat share."""
    tail = np.r_[np.cumsum(shares[::-1])[::-1], 0.0]           # tail[j] = sum(shares[j:])
    return lambda k: float(tail[k + 1]) + 15 / 390


def leg_sd(vix: float, expiry: int, d: int, k: int, s: float = 1.0, frac=rth_frac) -> float:
    """Priced sd (fraction of spot) of an option expiring at the close of session `expiry`, valued after bar k of
    session d (0 = entry session). s scales every premium term."""
    v = (vix / 100) ** 2 / 252
    mr, mo = M_RTH * s, M_ON * s
    if expiry == d:
        var = mr * mr * v * frac(k)
    elif expiry == d + 1:
        var = mr * mr * v * max(0.0, (77 - k) / 78) + mo * mo * v + mr * mr * v * rth_frac(-1)
    else:
        raise ValueError("only same-day and next-day legs are modeled")
    return math.sqrt(var)


def value(legs, S: float, vix: float, d: int, k: int, s: float = 1.0, frac=rth_frac) -> float:
    """Signed model value of the structure (long legs positive)."""
    tot = 0.0
    for r, K, q, e in legs:
        sd = leg_sd(vix, e, d, k, s, frac if e == d else rth_frac)
        tot += q * (call(S, K, sd) if r == "C" else put(S, K, sd))
    return tot


# ---------------------------------------------------------------- structures (right, strike, qty, expiry session)

def legs_condor(S: float, em: float, wing: int = 2):
    kc, kp = math.ceil(S + em), math.floor(S - em)
    return [("C", kc, -1, 0), ("C", kc + wing, 1, 0), ("P", kp, -1, 0), ("P", kp - wing, 1, 0)]


def legs_put_spread(S: float, em: float, wing: int = 2, expiry: int = 0):
    kp = math.floor(S - em)
    return [("P", kp, -1, expiry), ("P", kp - wing, 1, expiry)]


def legs_calendar(S: float):
    k = round(S)
    return [("C", k, -1, 0), ("C", k, 1, 1)]


# ---------------------------------------------------------------- trade simulation

def _cost(legs, cost) -> float:
    return sum(cost[e] for *_, e in legs)


def credit_trade(path, legs, vix, cost, width, tp=0.5, stop=2.0, min_credit=0.10, s=1.0, frac=rth_frac):
    """Short defined-risk structure. path: [(session, bar, spot)], first point is the entry.
    TP when the closing debit <= tp x credit, stop when it >= stop x credit (as strategies_bcd.condor_like)."""
    d0, k0, S0 = path[0]
    c = _cost(legs, cost)
    cr = -value(legs, S0, vix, d0, k0, s, frac) - c
    if min_credit is not None and cr < min_credit:
        return None
    v, why, exit_at = None, "time", path[-1][:2]
    for d, k, S in path[1:]:
        v = -value(legs, S, vix, d, k, s, frac) + c
        if v <= cr * tp:
            why, exit_at = "take 50%", (d, k)
            break
        if v >= stop * cr:
            why, exit_at = "stop", (d, k)
            break
    pnl = cr - v
    return {"credit": cr, "risk": width - cr, "pnl": pnl, "ret": pnl / (width - cr), "why": why, "exit": exit_at}


def debit_trade(path, legs, vix, cost, tp=0.25, stop=0.35, s=1.0):
    """Long structure (calendar). TP at +tp of the debit, stop at -stop, else the last path point."""
    d0, k0, S0 = path[0]
    c = _cost(legs, cost)
    debit = value(legs, S0, vix, d0, k0, s) + c
    if debit <= 0:
        return None
    w, why, exit_at = None, "time", path[-1][:2]
    for d, k, S in path[1:]:
        w = value(legs, S, vix, d, k, s) - c
        if w >= debit * (1 + tp):
            why, exit_at = "take profit", (d, k)
            break
        if w <= debit * (1 - stop):
            why, exit_at = "stop", (d, k)
            break
    pnl = w - debit
    return {"debit": debit, "risk": debit, "pnl": pnl, "ret": pnl / debit, "why": why, "exit": exit_at}


# ---------------------------------------------------------------- paths and filters

def intraday_path(C, i, k0, k1, f):
    return [(0, k, C[i, k] * f) for k in range(k0, k1 + 1)]


def next_is_consecutive(d0, d1) -> bool:
    """Next session is the next calendar day and the entry is Monday-Thursday (no weekend, no holiday gap)."""
    return d0.weekday() < 4 and (d1 - d0).days == 1


def overnight_path(C, days, i, f):
    if i + 1 >= len(days) or not next_is_consecutive(days[i], days[i + 1]):
        return None
    return [(0, k, C[i, k] * f) for k in range(K_1525, K_CLOSE + 1)] + [(1, k, C[i + 1, k] * f) for k in range(0, K_1000 + 1)]


def sma_gate(closes, i, n=50) -> bool:
    """Prior close above the average of the prior n closes (closes[:i] only; closes[i] is today's, unknown)."""
    if i < n:
        return False
    past = np.asarray(closes[:i], dtype=float)
    return bool(past[-1] > past[-n:].mean())


def trailing_profile(O, C, i, lookback=250):
    """Per-bar share of regular-session variance over the lookback sessions before i (known at entry)."""
    if i < lookback:
        return None
    Oi, Ci = O[i - lookback:i], C[i - lookback:i]
    prev = np.c_[Oi[:, :1], Ci[:, :-1]]
    r2 = np.log(Ci / prev) ** 2
    prof = r2.mean(0)
    return prof / prof.sum()


# ---------------------------------------------------------------- candidates on one session

def _em(vix, k, S, s=1.0, frac=rth_frac):
    return leg_sd(vix, 0, 0, k, s, frac) * S


def run_D(M, i, vix, cost, s=1.0):
    """Book D unfiltered (reference), same rules as strategies_bcd.condor_like(kind='condor')."""
    C = M["C"]; f = REBASE / M["O"][i, 0]
    S = C[i, K_1000] * f
    return credit_trade(intraday_path(C, i, K_1000, K_1525, f), legs_condor(S, 0.9 * _em(vix, K_1000, S)), vix, cost,
                        width=2, min_credit=None, s=s)


def run_F1(M, i, vix, cost, s=1.0, frac=rth_frac):
    C = M["C"]; f = REBASE / M["O"][i, 0]
    S = C[i, K_1330] * f
    return credit_trade(intraday_path(C, i, K_1330, K_1525, f), legs_condor(S, 0.9 * _em(vix, K_1330, S, 1.0, frac)),
                        vix, cost, width=2, s=s, frac=frac)


def run_F2(M, i, vix, cost, s=1.0):
    C = M["C"]; f = REBASE / M["O"][i, 0]
    S = C[i, K_1000] * f
    return credit_trade(intraday_path(C, i, K_1000, K_1525, f), legs_put_spread(S, 0.9 * _em(vix, K_1000, S)), vix,
                        cost, width=2, s=s)


def run_F3(M, days, i, vix, cost, s=1.0):
    if i + 1 >= len(days) or not next_is_consecutive(days[i], days[i + 1]):
        return None
    C = M["C"]; f = REBASE / M["O"][i, 0]
    S = C[i, K_1000] * f
    return debit_trade(intraday_path(C, i, K_1000, K_1525, f), legs_calendar(S), vix, cost, tp=0.25, stop=0.35, s=s)


def run_F4(M, days, i, vix, cost, s=1.0):
    C = M["C"]; f = REBASE / M["O"][i, 0]
    path = overnight_path(C, days, i, f)
    if path is None:
        return None
    S = path[0][2]
    v = (vix / 100) ** 2 / 252
    sd_exit = math.sqrt(M_RTH ** 2 * v * (77 - K_1525) / 78 + M_ON ** 2 * v + M_RTH ** 2 * v * (K_1000 + 1) / 78)
    return credit_trade(path, legs_put_spread(S, sd_exit * S, expiry=1), vix, cost, width=2, s=s)


# ---------------------------------------------------------------- stats

def summ(x) -> dict:
    x = pd.Series(x, dtype=float).dropna()
    if len(x) < 3:
        return {"n": int(len(x))}
    w, l, sd = x[x > 0].sum(), -x[x < 0].sum(), x.std(ddof=1)
    return {"n": int(len(x)), "avg": float(x.mean() * 100), "win": float((x > 0).mean() * 100),
            "pf": float(w / l) if l > 0 else float("inf"),
            "t": float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else float("nan")}


def max_drawdown(pnl) -> float:
    eq = np.r_[0.0, np.cumsum(np.asarray(pnl, dtype=float))]
    return float((eq - np.maximum.accumulate(eq)).min())


def lots_for(risk_usd: float, cap: float = CAP_PER_POSITION) -> int:
    return int(cap // risk_usd) if risk_usd > 0 else 0


def breakeven_scale(fn, lo=0.4, hi=1.3, tol=0.005) -> float:
    """Premium scale s where fn(s) (average return) crosses zero; nan if it doesn't in [lo, hi]."""
    flo, fhi = fn(lo), fn(hi)
    if np.sign(flo) == np.sign(fhi):
        return float("nan")
    while hi - lo > tol:
        mid = (lo + hi) / 2
        fm = fn(mid)
        if np.sign(fm) == np.sign(flo):
            lo, flo = mid, fm
        else:
            hi = mid
    return (lo + hi) / 2
