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


# ================================================================ replay (part 2)

import math  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

OPEN_M, CLOSE_M = 9 * 60 + 30, 16 * 60
SHORT_EXPIRY_MINUTE = 15 * 60 + 15
ALT_EXIT_MINUTE = 15 * 60                 # reported row: normal exits at 15:00 ET
FEE = 0.04                                # $ per contract per leg per side
VIX_MAX, IV_PCT_MAX, IV_MIN_CYCLES, LEG_SPREAD = 30.0, 0.80, 4, 0.05
E1_DTE = (4, 10)
TP = {"E1": 1.20, "E2": 1.15}
STOP = 0.70
MAX_DEBIT = {"E1": 500.0, "E2": 250.0}
MAX_OPEN = 3
IS_RANGE = (date(2018, 1, 1), date(2021, 12, 31))
OOS_START = date(2022, 1, 1)
T_BAR, T_SINGLE = 2.58, 2.33
MODELS = ("mid", "patient", "taker")


def fill(bid: float, ask: float, side: str, model: str) -> float:
    mid = (bid + ask) / 2
    if model == "mid":
        return mid
    if model == "taker":
        return bid if side == "sell" else ask
    return max(bid, mid - 0.01) if side == "sell" else min(ask, mid + 0.01)


class FreshPanel:
    """One expiry on one session: bid/ask per contract per minute, NaN where no valid (uncrossed) row exists.
    `fresh` reads only that minute; `last` takes the latest valid quote at or before it in the session."""

    def __init__(self, df: pd.DataFrame):
        self.df = df
        ok = (df["ask"] > 0) & (df["bid"] >= 0) & (df["ask"] >= df["bid"])
        d = df[ok]
        n = CLOSE_M - OPEN_M + 1
        self.q: dict[tuple[str, float], np.ndarray] = {}
        for (r, k), g in d.groupby(["right", "strike"]):
            a = np.full((n, 2), np.nan)
            i = g["minute"].to_numpy(dtype=int) - OPEN_M
            m = (i >= 0) & (i < n)
            a[i[m], 0], a[i[m], 1] = g["bid"].to_numpy()[m], g["ask"].to_numpy()[m]
            self.q[(r, float(k))] = a
        self._ff: dict = {}

    def strikes(self, right: str) -> list[float]:
        return sorted(k for r, k in self.q if r == right)

    def fresh(self, r: str, k: float, m: int):
        a = self.q.get((r, float(k)))
        i = m - OPEN_M
        if a is None or not 0 <= i < len(a) or np.isnan(a[i, 0]):
            return None
        return float(a[i, 0]), float(a[i, 1])

    def last(self, r: str, k: float, m: int):
        key = (r, float(k))
        if key not in self.q:
            return None
        if key not in self._ff:
            self._ff[key] = pd.DataFrame(self.q[key]).ffill().to_numpy()
        a, i = self._ff[key], min(m - OPEN_M, len(self._ff[key]) - 1)
        if i < 0 or np.isnan(a[i, 0]):
            return None
        return float(a[i, 0]), float(a[i, 1])


def panel_from_raw(raw: pd.DataFrame) -> FreshPanel:
    import bd_real_quotes as bd
    return FreshPanel(bd.normalize(raw))


def spot(p: FreshPanel, m: int) -> float | None:
    """Parity on the five strikes with the smallest |call mid - put mid|: median of K + C - P."""
    est = []
    for k in set(p.strikes("C")) & set(p.strikes("P")):
        c, q = p.last("C", k, m), p.last("P", k, m)
        if c and q:
            cm, pm = sum(c) / 2, sum(q) / 2
            est.append((abs(cm - pm), k + cm - pm))
    if not est:
        return None
    est.sort()
    return float(np.median([s for _, s in est[:5]]))


# ---------------------------------------------------------------- implied volatility

def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S: float, K: float, T: float, r: float, sig: float, right: str) -> float:
    if T <= 0 or sig <= 0:
        return max(S - K, 0.0) if right == "C" else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + sig * sig / 2) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if right == "C":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def implied_vol(price: float, S: float, K: float, T: float, r: float, right: str) -> float | None:
    lo, hi = 1e-4, 5.0
    if T <= 0 or price <= bs_price(S, K, T, r, lo, right) or price >= bs_price(S, K, T, r, hi, right):
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, r, mid, right) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-8:
            break
    return (lo + hi) / 2


def years_to_expiry(day: date, minute: int, expiry: date) -> float:
    return ((expiry - day).days * 1440 + (CLOSE_M - minute)) / 1440 / 365


def iv_ok(iv: float | None, history: list) -> tuple[bool, float | None]:
    """Book E's rule: skip when today's IV ranks above 80% of earlier cycles at the same T (4+ cycles needed)."""
    if iv is None or len(history) < IV_MIN_CYCLES:
        return True, None
    pct = sum(h <= iv for h in history) / len(history)
    return pct <= IV_PCT_MAX, pct


@dataclass
class IVHistory:
    """Earlier events' ATM IV per (symbol, structure, T)."""
    vals: dict = field(default_factory=dict)

    def get(self, sym: str, st: str, n: int) -> list:
        return list(self.vals.get((sym, st, n), []))

    def add(self, sym: str, st: str, n: int, iv: float | None) -> None:
        if iv is not None:
            self.vals.setdefault((sym, st, n), []).append(iv)


@dataclass
class Context:
    vix: pd.Series                    # CBOE VIX close by date
    rate: pd.Series                   # FRED DTB3 (percent) by date
    iv_filter: bool = True
    exit_minute: int | None = None    # None = 15:45 ET (12:20 on half days); reported row uses 15:00

    def prev(self, s: pd.Series, d: date) -> float | None:
        before = s[s.index < d].dropna()
        return float(before.iloc[-1]) if len(before) else None


# ---------------------------------------------------------------- entry and path

def _nearest(strikes, S: float) -> float | None:
    return min(strikes, key=lambda k: (abs(k - S), k)) if strikes else None


def _wide(q) -> bool:
    mid = (q[0] + q[1]) / 2
    return mid <= 0 or (q[1] - q[0]) > LEG_SPREAD * mid


def _entry(ev, quotes, st: str, day: date, model: str, ctx: Context, hist: IVHistory, n: int) -> dict:
    """Checks and price for one entry day. Legs are (right, strike, expiry, +1 long / -1 short)."""
    post, pre = ev["post"], ev.get("pre")
    m = clock_minute(day)
    pp = quotes.get((day, post))
    if pp is None or (st == "E2" and quotes.get((day, pre)) is None):
        return {"skip": "no_entry_quotes"}
    S = spot(pp, m)
    if S is None:
        return {"skip": "no_spot"}
    if st == "E1":
        K = _nearest(sorted(set(pp.strikes("C")) & set(pp.strikes("P"))), S)
        legs = [("C", K, post, 1), ("P", K, post, 1)]
    else:
        K = _nearest(sorted(set(pp.strikes("C")) & set(quotes[(day, pre)].strikes("C"))), S)
        legs = [("C", K, pre, -1), ("C", K, post, 1)]
    if K is None:
        return {"skip": "no_strike"}
    qs = [quotes[(day, e)].last(r, k, m) for r, k, e, _ in legs]
    if any(q is None for q in qs):
        return {"skip": "missing_leg"}
    rate = ctx.prev(ctx.rate, day)
    c, p = pp.last("C", K, m), pp.last("P", K, m)
    T = years_to_expiry(day, m, post)
    ivs = [implied_vol(sum(q) / 2, S, K, T, (rate or 0.0) / 100, r) for r, q in (("C", c), ("P", p)) if q]
    ivs = [v for v in ivs if v is not None]
    iv = sum(ivs) / len(ivs) if ivs else None
    out = {"S": S, "K": K, "legs": legs, "iv": iv, "minute": m}
    vix = ctx.prev(ctx.vix, day)
    if vix is None:
        return {**out, "skip": "no_vix"}
    if vix > VIX_MAX:
        return {**out, "skip": "vix"}
    if any(_wide(q) for q in qs):
        return {**out, "skip": "wide_leg"}
    ok, pct = iv_ok(iv, hist.get(ev["symbol"], st, n))
    out["iv_pct"] = pct
    if ctx.iv_filter and not ok:
        return {**out, "skip": "iv_percentile"}
    debit = sum(fill(*q, "buy", model) if s > 0 else -fill(*q, "sell", model) for (_, _, _, s), q in zip(legs, qs))
    if debit <= 0:
        return {**out, "skip": "no_debit"}
    return {**out, "debit": debit}


def _value(quotes, legs, day: date, m: int, model: str, fresh: bool):
    """Closing value: sell longs, buy back shorts. None if a leg has no quote."""
    tot = 0.0
    for r, k, e, s in legs:
        p = quotes.get((day, e))
        q = None if p is None else (p.fresh(r, k, m) if fresh else p.last(r, k, m))
        if q is None:
            return None
        tot += fill(*q, "sell", model) if s > 0 else -fill(*q, "buy", model)
    return tot


def _hold(ev, quotes, st: str, e: dict, entry_day: date, model: str, ctx: Context, tp_stop: bool) -> dict:
    legs, debit = e["legs"], e["debit"]
    x_day = exit_day(ev["D"], ev["timing"])
    x_min = ctx.exit_minute if ctx.exit_minute is not None else clock_minute(x_day)
    x_min, why_time = min(x_min, clock_minute(x_day)), "time"
    if st == "E2" and ev["pre"] <= x_day:
        cut = min(SHORT_EXPIRY_MINUTE, clock_minute(ev["pre"]))
        if ev["pre"] < x_day or cut < x_min:
            x_day, x_min, why_time = ev["pre"], cut, "short expiry"
    exit_at, why = (x_day, x_min), why_time
    if tp_stop:
        for d in sessions_between(entry_day, x_day):
            start = e["minute"] + 1 if d == entry_day else OPEN_M
            end = x_min if d == x_day else CLOSE_M + 1
            for m in range(start, end):
                v = _value(quotes, legs, d, m, "mid", fresh=True)
                if v is None:
                    continue
                if v >= TP[st] * debit - 1e-9:
                    exit_at, why = (d, m), "take profit"
                    break
                if v <= STOP * debit + 1e-9:
                    exit_at, why = (d, m), "stop"
                    break
            if why != why_time:
                break
    d, m = exit_at
    v = _value(quotes, legs, d, m, model, fresh=why != why_time)
    if v is None:
        return {"skip": "no_exit_quotes"}
    pnl = 100 * (v - debit) - 2 * len(legs) * FEE
    return {"entry_day": entry_day, "exit_day": d, "exit_minute": m, "why": why, "debit": debit, "value": v,
            "pnl": pnl, "ret": pnl / (100 * debit), "K": e["K"], "iv": e["iv"], "iv_pct": e.get("iv_pct")}


def e1(ev, quotes, model: str, ctx: Context, hist: IVHistory, tp_stop: bool = True) -> dict:
    day = t_minus(ev["D"], 3)
    if not E1_DTE[0] <= (ev["post"] - day).days <= E1_DTE[1]:
        return {"skip": "dte"}
    e = _entry(ev, quotes, "E1", day, model, ctx, hist, 3)
    if "skip" in e:
        return {"skip": e["skip"]}
    return _hold(ev, quotes, "E1", e, day, model, ctx, tp_stop)


def e2(ev, quotes, model: str, ctx: Context, hist: IVHistory, tp_stop: bool = True) -> dict:
    if ev.get("pre") is None:
        return {"skip": "no_pre_expiry"}
    why = "no_entry_day"
    for n in (10, 9, 8):
        day = t_minus(ev["D"], n)
        if ev["pre"] <= day:
            why = "short_leg_expired"
            continue
        e = _entry(ev, quotes, "E2", day, model, ctx, hist, n)
        if "skip" in e:
            why = e["skip"]
            continue
        return {**_hold(ev, quotes, "E2", e, day, model, ctx, tp_stop), "t": n}
    return {"skip": why}


def record_iv(ev, quotes, ctx: Context, hist: IVHistory) -> None:
    """After an event is evaluated, its ATM IVs at each entry T join the history for later events."""
    for st, ns in (("E1", (3,)), ("E2", (10, 9, 8))):
        for n in ns:
            day = t_minus(ev["D"], n)
            if st == "E2" and (ev.get("pre") is None or ev["pre"] <= day):
                continue
            e = _entry(ev, quotes, st, day, "mid", Context(ctx.vix, ctx.rate, iv_filter=False), IVHistory(), n)
            hist.add(ev["symbol"], st, n, e.get("iv"))


# ---------------------------------------------------------------- statistics

def clustered(df: pd.DataFrame, col: str = "ret") -> dict:
    """Events entered the same day are averaged first; t over entry dates."""
    if not len(df):
        return {"trades": 0, "dates": 0, "mean": None, "t": None}
    x = df.groupby("entry_day")[col].mean()
    n = len(x)
    sd = x.std(ddof=1) if n > 1 else float("nan")
    r = df[col]
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    return {"trades": int(len(df)), "dates": int(n), "mean": float(x.mean()),
            "t": float(x.mean() / (sd / math.sqrt(n))) if n > 1 and sd > 0 else None,
            "trade_mean": float(r.mean()), "win": float((r > 0).mean()), "pf": float(pos / neg) if neg > 0 else None}


def verdict(d: dict) -> dict:
    checks = {"in-sample mean > 0 (patient)": d["is_mean"] is not None and d["is_mean"] > 0,
              "out-of-sample t >= 2.58 (patient)": d["oos_t"] is not None and d["oos_t"] >= T_BAR,
              "out-of-sample mean > 0 (taker)": d["oos_taker_mean"] is not None and d["oos_taker_mean"] > 0}
    return {"pass": all(checks.values()), "checks": checks,
            "oos_t_clears_2.33": d["oos_t"] is not None and d["oos_t"] >= T_SINGLE}


# ---------------------------------------------------------------- driver

VARIANTS = {"main": {}, "no_tp_stop": {"tp_stop": False}, "exit_1500": {"exit_minute": ALT_EXIT_MINUTE},
            "iv_filter_off": {"iv_filter": False}}


def load_quotes(path: Path) -> dict:
    raw = pd.read_parquet(path)
    out = {}
    for (s, e), g in raw.groupby(["session", "expiry"]):
        try:
            out[(date.fromisoformat(str(s)[:10]), date.fromisoformat(str(e)[:10]))] = panel_from_raw(g)
        except Exception:  # noqa: BLE001 - an unreadable slice is just missing
            continue
    return out


def run_symbol(args) -> list[dict]:
    """Every event of one symbol in D order (the IV history only looks back)."""
    sym, events, qdir, vix, rate = args
    hist, rows = IVHistory(), []
    for ev in sorted(events, key=lambda e: e["D"]):
        path = Path(qdir) / f"{sym}_{ev['D'].isoformat()}.parquet"
        base = {"symbol": sym, "D": ev["D"], "timing": ev["timing"], "sector": ev.get("sector")}
        if not path.exists() or ev.get("post") is None:
            rows.append({**base, "st": "both", "variant": "main", "model": "all", "skip": "no_quote_file"})
            continue
        quotes = load_quotes(path)
        for var, kw in VARIANTS.items():
            ctx = Context(vix, rate, iv_filter=kw.get("iv_filter", True), exit_minute=kw.get("exit_minute"))
            for model in MODELS:
                for st, fn in (("E1", e1), ("E2", e2)):
                    r = fn(ev, quotes, model, ctx, hist, tp_stop=kw.get("tp_stop", True))
                    rows.append({**base, "st": st, "variant": var, "model": model, **r})
        record_iv(ev, quotes, Context(vix, rate), hist)
    return rows


def portfolio(df: pd.DataFrame, sectors: dict) -> dict:
    """Book E's limits on the main patient trades: 3 open, one per sector, one per symbol, $500 / $250 max debit."""
    t = df.sort_values(["entry_day", "symbol", "st"])
    open_, kept = [], []
    for r in t.itertuples():
        open_ = [o for o in open_ if (o.exit_day, o.exit_minute) > (r.entry_day, 0)]
        lots = int(MAX_DEBIT[r.st] // (100 * r.debit)) if r.debit > 0 else 0
        sec = next((s for s, names in sectors.items() if r.symbol in names), None)
        if (lots < 1 or len(open_) >= MAX_OPEN or any(o.symbol == r.symbol for o in open_)
                or any(o.sector == sec for o in open_)):
            continue
        open_.append(r)
        kept.append({"entry_day": r.entry_day, "ret": r.ret, "pnl": r.pnl * lots, "lots": lots})
    k = pd.DataFrame(kept)
    if not len(k):
        return {"trades": 0}
    return {**clustered(k), "total_pnl": float(k["pnl"].sum()), "mean_lots": float(k["lots"].mean())}


def summarize(df: pd.DataFrame, sectors: dict) -> dict:
    out = {}
    traded = df[df["skip"].isna()] if "skip" in df else df
    for st in ("E1", "E2"):
        sel = lambda var="main", model="patient": traded[(traded.st == st) & (traded.variant == var)  # noqa: E731
                                                         & (traded.model == model)]
        is_ = lambda x: x[(x.D >= IS_RANGE[0]) & (x.D <= IS_RANGE[1])]  # noqa: E731
        oos = lambda x: x[x.D >= OOS_START]  # noqa: E731
        bars = {"is_mean": clustered(is_(sel()))["mean"], "oos_t": clustered(oos(sel()))["t"],
                "oos_taker_mean": clustered(oos(sel(model="taker")))["mean"]}
        main = sel()
        res = {"bars": bars, "verdict": verdict(bars),
               "in_sample": {m: clustered(is_(sel(model=m))) for m in MODELS},
               "out_of_sample": {m: clustered(oos(sel(model=m))) for m in MODELS},
               "reported_only": {
                   **{f"{v} (OOS patient)": clustered(oos(sel(var=v))) for v in VARIANTS if v != "main"},
                   **{f"{v} (IS patient)": clustered(is_(sel(var=v))) for v in VARIANTS if v != "main"},
                   "as_built_portfolio_2018_on (patient)": portfolio(main[main.D >= IS_RANGE[0]], sectors),
                   "by_year": {int(y): clustered(g) for y, g in main.groupby(main.D.map(lambda d: d.year))},
                   "by_sector": {str(s): clustered(g) for s, g in main.groupby("sector")},
                   "by_timing": {str(s): clustered(g) for s, g in main.groupby("timing")},
                   "exit_reasons": main["why"].value_counts().to_dict(),
               }}
        allm = df[(df.st.isin([st, "both"])) & (df.variant == "main") & (df.model.isin(["patient", "all"]))]
        res["skips (main, patient)"] = allm["skip"].fillna("traded").value_counts().to_dict()
        out[st] = res
    return out


def main() -> int:
    import argparse
    import json
    from concurrent.futures import ProcessPoolExecutor
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--earnings", type=Path, default=Path("research/data/x1/earnings.csv"))
    ap.add_argument("--quotes", type=Path, default=Path("data/thetadata/e"))
    ap.add_argument("--manifest", type=Path, default=Path("data/thetadata/e_manifest.csv"))
    ap.add_argument("--vix", type=Path, default=Path("research/data/vix_cboe.csv"))
    ap.add_argument("--rate", type=Path, default=Path("research/data/dtb3.csv"))
    ap.add_argument("--out", type=Path, default=Path("research/data/round5/e"))
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    import csv
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agentdesk.config import load_config
    sectors = load_config()["books"]["E_earnings_iv"]["sectors"]
    exp = {}
    if a.manifest.exists():
        for r in csv.DictReader(a.manifest.open()):
            if r["status"] == "ok" and r["post"]:
                exp[(r["symbol"], r["D"])] = (date.fromisoformat(r["post"]),
                                              date.fromisoformat(r["pre"]) if r["pre"] else None)
    events = []
    with open(a.earnings) as f:
        for r in csv.DictReader(f):
            ev = event(r)
            if ev is None:
                continue
            post, pre = exp.get((ev["symbol"], ev["D"].isoformat()), (None, None))
            sec = next((s for s, names in sectors.items() if ev["symbol"] in names), None)
            events.append({**ev, "post": post, "pre": pre, "sector": sec})
    v = pd.read_csv(a.vix)
    v.columns = [c.upper() for c in v.columns]
    vix = pd.Series(v["CLOSE"].values, index=pd.to_datetime(v["DATE"]).dt.date).sort_index()
    rt = pd.read_csv(a.rate, na_values=["."])
    rate = pd.Series(pd.to_numeric(rt.iloc[:, 1], errors="coerce").values,
                     index=pd.to_datetime(rt.iloc[:, 0]).dt.date).sort_index()
    by_sym: dict = {}
    for ev in events:
        by_sym.setdefault(ev["symbol"], []).append(ev)
    jobs = [(s, evs, str(a.quotes), vix, rate) for s, evs in sorted(by_sym.items())]
    rows = []
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for r in ex.map(run_symbol, jobs):
            rows += r
    df = pd.DataFrame(rows)
    a.out.mkdir(parents=True, exist_ok=True)
    df.to_parquet(a.out / "e_trades.parquet", index=False)
    res = {"events": len(events), "with_quotes": int(sum(1 for e in events if e["post"] is not None)),
           **summarize(df, sectors)}
    (a.out / "e_results.json").write_text(json.dumps(res, indent=1, default=str))
    for st in ("E1", "E2"):
        b, v_ = res[st]["bars"], res[st]["verdict"]
        print(f"{st} {'PASS' if v_['pass'] else 'FAIL'}: IS mean {b['is_mean']}, OOS t {b['oos_t']}, "
              f"OOS taker mean {b['oos_taker_mean']}")
    print(f"-> {a.out / 'e_results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
