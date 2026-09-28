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
    session d (0 = entry session). s scales every premium term. Only same-day legs use `frac`."""
    v = (vix / 100) ** 2 / 252
    mr, mo = M_RTH * s, M_ON * s
    n = expiry - d
    if n == 0:
        var = mr * mr * v * frac(k)
    elif n >= 1:                                   # trading-day clock: a night and a session per session ahead
        var = mr * mr * v * max(0.0, (77 - k) / 78) + n * mo * mo * v + (n - 1) * mr * mr * v + mr * mr * v * rth_frac(-1)
    else:
        raise ValueError("leg already expired")
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

def _cost(legs, cost, d=0) -> float:
    """Round-trip friction for one side of the trade, priced by each leg's sessions left at the fill."""
    return sum(cost[min(e - d, 1)] for *_, e in legs)


def credit_trade(path, legs, vix, cost, width, tp=0.5, stop=2.0, min_credit=0.10, s=1.0, frac=rth_frac):
    """Short defined-risk structure. path: [(session, bar, spot)], first point is the entry.
    TP when the closing debit <= tp x credit, stop when it >= stop x credit (as strategies_bcd.condor_like)."""
    d0, k0, S0 = path[0]
    cr = -value(legs, S0, vix, d0, k0, s, frac) - _cost(legs, cost, d0)
    if min_credit is not None and cr < min_credit:
        return None
    v, why, exit_at = None, "time", path[-1][:2]
    for d, k, S in path[1:]:
        v = -value(legs, S, vix, d, k, s, frac) + _cost(legs, cost, d)
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
    debit = value(legs, S0, vix, d0, k0, s) + _cost(legs, cost, d0)
    if debit <= 0:
        return None
    w, why, exit_at = None, "time", path[-1][:2]
    for d, k, S in path[1:]:
        w = value(legs, S, vix, d, k, s) - _cost(legs, cost, d)
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


def run_F1(M, i, vix, cost, s=1.0, frac=rth_frac, min_credit=0.10):
    C = M["C"]; f = REBASE / M["O"][i, 0]
    S = C[i, K_1330] * f
    return credit_trade(intraday_path(C, i, K_1330, K_1525, f), legs_condor(S, 0.9 * _em(vix, K_1330, S, 1.0, frac)),
                        vix, cost, width=2, s=s, frac=frac, min_credit=min_credit)


def run_F2(M, i, vix, cost, s=1.0, min_credit=0.10):
    C = M["C"]; f = REBASE / M["O"][i, 0]
    S = C[i, K_1000] * f
    return credit_trade(intraday_path(C, i, K_1000, K_1525, f), legs_put_spread(S, 0.9 * _em(vix, K_1000, S)), vix,
                        cost, width=2, s=s, min_credit=min_credit)


def run_F3(M, days, i, vix, cost, s=1.0):
    if i + 1 >= len(days) or not next_is_consecutive(days[i], days[i + 1]):
        return None
    C = M["C"]; f = REBASE / M["O"][i, 0]
    S = C[i, K_1000] * f
    return debit_trade(intraday_path(C, i, K_1000, K_1525, f), legs_calendar(S), vix, cost, tp=0.25, stop=0.35, s=s)


def run_F4(M, days, i, vix, cost, s=1.0, min_credit=0.10):
    C = M["C"]; f = REBASE / M["O"][i, 0]
    path = overnight_path(C, days, i, f)
    if path is None:
        return None
    S = path[0][2]
    v = (vix / 100) ** 2 / 252
    sd_exit = math.sqrt(M_RTH ** 2 * v * (77 - K_1525) / 78 + M_ON ** 2 * v + M_RTH ** 2 * v * (K_1000 + 1) / 78)
    return credit_trade(path, legs_put_spread(S, sd_exit * S, expiry=1), vix, cost, width=2, s=s,
                        min_credit=min_credit)


# ---------------------------------------------------------------- G1 / G2: multi-day call debit spreads

K_1555 = 76


def month_turn_entries(days):
    """(entry, exit) session indexes: second-to-last session of a month -> third session of the next month."""
    out = []
    months = [(d.year, d.month) for d in days]
    for i in range(len(days) - 1):
        if months[i] == months[i + 1] and (i + 2 >= len(days) or months[i + 2] != months[i]):
            first = i + 2                                   # first session of the next month
            if first + 2 < len(days) and months[first] != months[i] and months[first + 2] == months[first]:
                out.append((i, first + 2))
    return out


def vix_stretch_entries(vixp, n_sessions, hold=5, ratio=1.20, lookback=10):
    """vixp[i] = VIX close before session i. Enter at session i's close when vixp[i] > ratio x mean(vixp[i-10:i]);
    exit at session i + hold. One position at a time."""
    out, busy_until = [], -1
    for i in range(lookback, n_sessions - hold):
        if i < busy_until:
            continue
        past = vixp[i - lookback:i]
        if np.all(np.isfinite(past)) and np.isfinite(vixp[i]) and vixp[i] > ratio * past.mean():
            out.append((i, i + hold))
            busy_until = i + hold
    return out


def run_G(M, i, j, vix, cost, s=1.0):
    """$5 call debit spread at session i's 15:55 close, expiring and closed at session j's 15:25 bar."""
    C = M["C"]; f = REBASE / M["O"][i, 0]
    S0, S1 = C[i, K_1555] * f, C[j, K_1525] * f
    n = j - i
    k = round(S0)
    legs = [("C", k, 1, n), ("C", k + 5, -1, n)]
    r = debit_trade([(0, K_1555, S0), (n, K_1525, S1)], legs, vix, cost, tp=math.inf, stop=math.inf, s=s)
    if r is None:
        return None
    return {**r, "legs": legs, "und_bp": (S1 / S0 - 1) * 1e4 - 2.0}


# ---------------------------------------------------------------- runner

CANDIDATES = ("D", "F1", "F1-profile", "F2", "F2-trend", "F3", "F4", "G1", "G2")   # D first: the others correlate against it
_DEFAULT = object()


def period_of(d) -> str:
    return "2005-14" if d.year <= 2014 else "2015-20" if d.year <= 2020 else "2025-26"


def run_one(name, days, M, i, vix, cost, s=1.0, min_credit=_DEFAULT, closes=None):
    mc = {} if min_credit is _DEFAULT else {"min_credit": min_credit}
    if name == "F1":
        return run_F1(M, i, vix, cost, s, **mc)
    if name == "F1-profile":
        prof = trailing_profile(M["O"], M["C"], i)
        return None if prof is None else run_F1(M, i, vix, cost, s, frac=profile_frac(prof), **mc)
    if name == "F2":
        return run_F2(M, i, vix, cost, s, **mc)
    if name == "F2-trend":
        return run_F2(M, i, vix, cost, s, **mc) if sma_gate(closes, i, 50) else None
    if name == "F3":
        return run_F3(M, days, i, vix, cost, s)
    if name == "F4":
        return run_F4(M, days, i, vix, cost, s, **mc)
    if name == "D":
        return run_D(M, i, vix, cost, s)
    raise ValueError(name)


def collect(name, days, M, vixp, cost, s=1.0, min_credit_override=_DEFAULT, keep_dates=None) -> pd.DataFrame:
    closes = M["C"][:, -1]
    rows = []
    if name in ("G1", "G2"):
        pairs = month_turn_entries(days) if name == "G1" else vix_stretch_entries(np.asarray(vixp, float), len(days))
        for i, j in pairs:
            if not np.isfinite(vixp[i]) or (keep_dates is not None and days[i] not in keep_dates):
                continue
            r = run_G(M, i, j, float(vixp[i]), cost, s)
            if r is not None:
                r.pop("legs")
                rows.append({"i": i, "date": days[i], "period": period_of(days[i]), "year": days[i].year, **r})
    for i, d in enumerate(days if name not in ("G1", "G2") else []):
        if not np.isfinite(vixp[i]) or (keep_dates is not None and d not in keep_dates):
            continue
        r = run_one(name, days, M, i, float(vixp[i]), cost, s, min_credit_override, closes)
        if r is not None:
            rows.append({"i": i, "date": d, "period": period_of(d), "year": d.year, **r})
    cols = ["i", "date", "period", "year", "ret", "pnl", "risk", "why"]
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=cols)


def _block(x: pd.DataFrame) -> dict:
    s = summ(x.ret)
    if len(x) == 0:
        return s
    usd = x.pnl * 100
    lots = np.array([lots_for(r * 100) for r in x.risk])
    yrs = max(1e-9, (x.date.max() - x.date.min()).days / 365.25) if len(x) > 1 else 1.0
    s.update({"avg_usd_1lot": float(usd.mean()), "worst_usd_1lot": float(usd.min()),
              "max_dd_usd_1lot": max_drawdown(usd), "max_risk_usd_1lot": float(x.risk.max() * 100),
              "lots_at_cap": int(np.floor(np.median(lots))), "share_trades_fitting_cap": float((lots >= 1).mean()),
              "max_dd_usd_at_cap": max_drawdown(usd.to_numpy() * lots),
              "trades_per_year": float(len(x) / yrs), "usd_per_year_at_cap": float((usd.to_numpy() * lots).sum() / yrs),
              "exits": {str(k): int(v) for k, v in x.why.value_counts().items()}})
    return s


def summarize(df: pd.DataFrame) -> dict:
    out = {"all": _block(df)}
    for p in ("2005-14", "2015-20", "2025-26"):
        out[p] = _block(df[df.period == p])
    out["by_year"] = {str(y): summ(g.ret) for y, g in df.groupby("year")}
    return out


def corr_with(a: pd.DataFrame, b: pd.DataFrame) -> float:
    m = a[["date", "pnl"]].merge(b[["date", "pnl"]], on="date")
    return float(m.pnl_x.corr(m.pnl_y)) if len(m) > 2 else float("nan")


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


# ---------------------------------------------------------------- decision rule (frozen in the pre-registration)

T_BAR = 2.86


def decision(mid: dict, nat: dict, be_s: float) -> dict:
    checks = {
        "t_2005-14_mid>2.86": mid.get("2005-14", {}).get("t", float("nan")) > T_BAR,
        "natural_avg_2015-20>0": nat.get("2015-20", {}).get("avg", float("nan")) > 0,
        "natural_avg_2025-26>0": nat.get("2025-26", {}).get("avg", float("nan")) > 0,
        "breakeven_s<=0.85": bool(np.isfinite(be_s) and be_s <= 0.85),
        "fits_300_cap": mid.get("all", {}).get("lots_at_cap", 0) >= 1,
    }
    return {"pass": all(checks.values()), "checks": checks}


# ---------------------------------------------------------------- data and main

def load_raw(data: Path):
    """Un-rebased 5m matrices: S&P 500 CFD 2005-2020 and SPY 2025-26 (the run functions re-base per trade)."""
    sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
    from research import matrices
    from strategies_bcd import to5, prev_vix
    spx = pd.read_pickle(data / "spx_rth_1m.pkl")
    d1, M1, _ = matrices(spx, 1)
    M1 = to5(M1)
    spy = pd.read_csv(data / "spy_5m_2025_2026.csv")
    spy["et"] = pd.to_datetime(spy["Datetime"], utc=True).dt.tz_convert("America/New_York")
    spy["day"] = spy["et"].dt.date; spy["hm"] = spy["et"].dt.hour * 60 + spy["et"].dt.minute
    d2, M2, _ = matrices(spy.rename(columns=str.lower), 5)
    vix = pd.read_csv(data / "vix.csv", parse_dates=["DATE"]).set_index("DATE")["CLOSE"]
    vix.index = vix.index.date
    return [(d1, M1, prev_vix(vix, d1)), (d2, M2, prev_vix(vix, d2))]


def collect_all(sets, name, cost, s=1.0) -> pd.DataFrame:
    parts = [collect(name, days, M, vp, cost, s) for days, M, vp in sets]
    parts = [p for p in parts if len(p)]
    return pd.concat(parts, ignore_index=True) if parts else collect(name, [], {"C": np.zeros((0, 78))}, [], cost)


def breakeven_for(sets, name, period, base: pd.DataFrame):
    """Premium scale where the average trade is zero, on the fixed set of trades taken at s = 1 (addendum 1)."""
    keep = set(base[base.period == period].date)
    if not keep:
        return float("nan")

    def avg(s):
        parts = [collect(name, days, M, vp, COSTS["mid-1c"], s, min_credit_override=None, keep_dates=keep)
                 for days, M, vp in sets]
        x = pd.concat([p for p in parts if len(p)], ignore_index=True).ret if any(len(p) for p in parts) else []
        return float(np.mean(x)) if len(x) else float("nan")
    be = breakeven_scale(avg)
    if not np.isfinite(be) and avg(0.4) > 0:
        return 0.4                                   # profitable across the whole range: report the bound
    return be


def run_all(data: Path = HERE / "data"):
    sets = load_raw(data)
    res, trades = {}, {}
    for name in CANDIDATES:
        r = {}
        for cname, cost in COSTS.items():
            df = collect_all(sets, name, cost)
            trades[(name, cname)] = df
            r[cname] = summarize(df)
        r["mid-1c, s=0.75"] = summarize(collect_all(sets, name, COSTS["mid-1c"], 0.75))
        r["breakeven_s"] = {p: breakeven_for(sets, name, p, trades[(name, "mid-1c")]) for p in ("2005-14", "2015-20", "2025-26")}
        if "und_bp" in trades[(name, "mid-1c")]:
            u = trades[(name, "mid-1c")]
            r["underlying_after_2bp"] = {p: {**summ(u[u.period == p].und_bp / 1e4), "avg_bp": float(u[u.period == p].und_bp.mean())}
                                         for p in ("2005-14", "2015-20", "2025-26") if (u.period == p).any()}
        r["corr_with_D_mid-1c"] = corr_with(trades[(name, "mid-1c")], trades[("D", "mid-1c")]) if name != "D" else 1.0
        r["decision"] = decision(r["mid-1c"], r["natural"], r["breakeven_s"]["2005-14"])
        res[name] = r
        print(name, "done", flush=True)
    return res, trades


def _f(x, spec="+.2f"):
    return "n/a" if x is None or (isinstance(x, float) and not np.isfinite(x)) else format(x, spec)


def report(res: dict) -> str:
    L = ["# New strategy candidates F1-F4: modeled backtest", "",
         "Rules frozen in `research/strategies_new_prereg.md` before this ran. Returns are % of max risk per trade",
         "(F3, G1, G2: % of the debit). Fills per leg per side: mid-1c = 1c from mid; natural = 2c same-day / 3c",
         "for legs with a session or more left. Capacity is per trade: floor($300 / that trade's max loss).",
         "s = premium scale (1.0 = base model, 0.75 = little variance premium). Break-even s is at mid-1c.", ""]
    for name, r in res.items():
        L += [f"## {name}", "", "| period | fills | n | avg | win | PF | t | avg $/lot | worst $/lot | max DD $/lot | "
              "lots at $300 (share fitting) | $/yr at cap | max DD $ at cap |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for p in ("2005-14", "2015-20", "2025-26"):
            for label in ("mid-1c", "natural", "mid-1c, s=0.75"):
                b = r[label][p]
                if b.get("n", 0) < 3:
                    L.append(f"| {p} | {label} | {b.get('n', 0)} | | | | | | | | | | |"); continue
                L.append(f"| {p} | {label} | {b['n']} | {_f(b['avg'])}% | {b['win']:.0f}% | {_f(b['pf'], '.2f')} | "
                         f"{_f(b['t'])} | {_f(b['avg_usd_1lot'], '+.1f')} | {_f(b['worst_usd_1lot'], '+.0f')} | "
                         f"{_f(b['max_dd_usd_1lot'], '+.0f')} | {b['lots_at_cap']} ({b['share_trades_fitting_cap']:.0%}) | "
                         f"{_f(b['usd_per_year_at_cap'], '+.0f')} | {_f(b['max_dd_usd_at_cap'], '+.0f')} |")
        be = r["breakeven_s"]
        L += ["", "Break-even premium scale s (mid-1c): " + ", ".join(f"{p} {_f(v, '.2f')}" for p, v in be.items()),
              f"Daily P&L correlation with book D (mid-1c): {_f(r['corr_with_D_mid-1c'], '.2f')}", ""]
        for p, u in r.get("underlying_after_2bp", {}).items():
            if u.get("n", 0) >= 3:
                L.append(f"- Underlying only, {p}: n={u['n']} avg {u['avg_bp']:+.1f} bp, win {u['win']:.0f}%, t {u['t']:+.2f}")
        if r.get("underlying_after_2bp"):
            L.append("")
        if name != "D":
            d = r["decision"]
            L += [f"Pre-registered decision rule: **{'PASS' if d['pass'] else 'FAIL'}** "
                  + ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in d["checks"].items()), ""]
        yrs = r["mid-1c"]["by_year"]
        L += ["By year (mid-1c, avg % / t): " + "; ".join(f"{y} {_f(v.get('avg'), '+.1f')}/{_f(v.get('t'), '+.1f')}"
                                                          for y, v in yrs.items() if v.get("n", 0) >= 3), ""]
    return "\n".join(L)


if __name__ == "__main__":
    res, trades = run_all()
    (HERE / "strategies_new_results.json").write_text(json.dumps(res, indent=1, default=float))
    (HERE / "strategies_new_report.md").write_text(report(res))
    print(report(res))
