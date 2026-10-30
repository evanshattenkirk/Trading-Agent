"""W (research/round5_prereg.md section 2): a 0DTE ATM iron fly with wings 1.0-2.0 straddles out, no take-profit or
stop, closed at 15:40 ET, on real 1-minute ThetaData NBBO quotes for SPY, QQQ and IWM. Research only.

    .venv/bin/python research/round5_w.py --und SPY=data/thetadata/spy_0dte --und QQQ=data/thetadata/qqq_0dte \\
        --und IWM=data/thetadata/iwm_0dte --vix research/data/vix.csv --out research/data/round5/w

Each session file holds one underlying's same-day expiry, all strikes, 09:30-16:00 ET (fetch_thetadata_spy0dte.py).
Per cell (entry E, wing multiple w): spot by parity at E; K = the strike listed for calls and puts nearest spot
(ties lower); M = the K straddle's mid; sell K call and put, buy the call nearest K + w*M and the put nearest K - w*M
(ties farther, at least one strike from K). Skip on a missing leg, a zero-bid short, or a leg wider than 25% of its
mid and $0.02. At 15:40 buy back the shorts and sell each wing bid >= $0.01 (a zero-bid wing expires, no fee); if a
leg has no quote, the latest minute back to 15:30 where all have one. Return = P&L / max loss per lot.
Discovery picks the cell with the best patient t on SPY 2016-2021; confirmation pools SPY, QQQ and IWM by day from
2022, and QQQ + IWM alone are the fresh sample.

Implementation note: a long wing with no quote at all at the exit minute counts as a leg without a quote (its bid
can't be known), so that minute fails like a missing short.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bd_real_quotes as bd  # noqa: E402
from nyse_calendar import half_days  # noqa: E402

ENTRIES = (9 * 60 + 45, 11 * 60, 13 * 60)
WINGS = (1.0, 1.5, 2.0)
EXIT, EXIT_ALT, FALLBACK = 15 * 60 + 40, 15 * 60 + 30, 10
MODELS = ("mid", "patient", "taker")
SPREAD_PCT, SPREAD_EXEMPT = 0.25, 0.02
DISC = (date(2016, 1, 1), date(2021, 12, 31))
CONF_START = date(2022, 1, 1)
DAILY_ERA = date(2022, 11, 14)
T_BAR, T_SINGLE, T_FRESH = 2.58, 2.33, 1.65
VIX_BUCKETS = [(0, 15, "<15"), (15, 20, "15-20"), (20, 30, "20-30"), (30, 1e9, ">30")]


def hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def session_reason(day: date) -> str | None:
    return "half_day" if day in half_days(day.year) else None


# ---------------------------------------------------------------- strikes

def _mid(q) -> float:
    return (q[0] + q[1]) / 2


def pick(p: bd.QuotePanel, minute: int, w: float) -> dict:
    S = bd.spot(p, minute)
    if S is None:
        return {"skip": "no_spot"}
    both = sorted(set(p.strikes("C")) & set(p.strikes("P")))
    K = min(both, key=lambda k: (abs(k - S), k))
    c, q = p.ba("C", K, minute), p.ba("P", K, minute)
    if c is None or q is None:
        return {"skip": "missing_leg"}
    M = _mid(c) + _mid(q)
    calls, puts = [k for k in p.strikes("C") if k > K], [k for k in p.strikes("P") if k < K]
    if not calls or not puts:
        return {"skip": "no_wing_strike"}
    kc = min(calls, key=lambda k: (abs(k - (K + w * M)), -k))
    kp = min(puts, key=lambda k: (abs(k - (K - w * M)), k))
    return {"S": S, "K": K, "M": M, "kc": kc, "kp": kp}


def legs_b(p: bd.QuotePanel, minute: int) -> dict:
    """Book B's strikes: round(spot) and +-$5."""
    S = bd.spot(p, minute)
    if S is None:
        return {"skip": "no_spot"}
    k = float(round(S))
    return {"S": S, "K": k, "M": None, "kc": k + 5, "kp": k - 5}


def _legs(s: dict):
    return [("C", s["K"], -1), ("P", s["K"], -1), ("C", s["kc"], 1), ("P", s["kp"], 1)]


def _open(p: bd.QuotePanel, entry: int, w, model: str, legs_fn=None) -> dict:
    s = pick(p, entry, w) if legs_fn is None else legs_fn(p, entry)
    if "skip" in s:
        return s
    lg = _legs(s)
    qs = [p.ba(r, k, entry) for r, k, _ in lg]
    if any(q is None for q in qs):
        return {"skip": "missing_leg"}
    if any(q[0] <= 0 for (_, _, n), q in zip(lg, qs) if n < 0):
        return {"skip": "zero_bid_short"}
    if any(q[1] - q[0] > SPREAD_EXEMPT and q[1] - q[0] > SPREAD_PCT * _mid(q) for q in qs):
        return {"skip": "wide_leg"}
    credit = sum(bd.fill(*q, "sell", model) if n < 0 else -bd.fill(*q, "buy", model) for (_, _, n), q in zip(lg, qs))
    return {**s, "legs": lg, "credit": credit}


# ---------------------------------------------------------------- trade

def trade(p: bd.QuotePanel, entry: int, w, model: str, exit_min: int = EXIT, legs_fn=None) -> dict:
    """One lot. P&L and max loss in dollars; credit per share."""
    o = _open(p, entry, w, model, legs_fn)
    if "skip" in o:
        return o
    lg, credit = o["legs"], o["credit"]
    for m in range(exit_min, exit_min - FALLBACK - 1, -1):
        qs = [p.ba(r, k, m) for r, k, _ in lg]
        if any(q is None for q in qs):
            continue
        cost, sold = 0.0, 0
        for (_, _, n), q in zip(lg, qs):
            if n < 0:
                cost += bd.fill(*q, "buy", model)
            elif q[0] >= 0.01:
                cost -= bd.fill(*q, "sell", model)
                sold += 1
        break
    else:
        return {"skip": "no_exit_quote"}
    width = max(o["kc"] - o["K"], o["K"] - o["kp"])
    max_loss = 100 * (width - credit) + 100 * 8 * bd.FEE
    if max_loss <= 0:
        return {"skip": "credit_above_width"}
    pnl = 100 * (credit - cost - (4 + 2 + sold) * bd.FEE)
    return {"S": o["S"], "K": o["K"], "kc": o["kc"], "kp": o["kp"], "M": o["M"], "credit": credit, "cost": cost,
            "wings_sold": sold, "exit_minute": m, "pnl": pnl, "max_loss": max_loss, "ret": pnl / max_loss}


def settlement(p: bd.QuotePanel, entry: int, w, model: str, legs_fn=None) -> dict:
    """Held to the close: legs at intrinsic against the 16:00 parity spot, no exit cost (not tradeable on SPY)."""
    o = _open(p, entry, w, model, legs_fn)
    if "skip" in o:
        return o
    s_end = bd.spot(p, min(p.last_minute, bd.CLOSE - 1))
    if s_end is None:
        return {"skip": "no_close_spot"}
    width = max(o["kc"] - o["K"], o["K"] - o["kp"])
    max_loss = 100 * (width - o["credit"]) + 100 * 4 * bd.FEE
    if max_loss <= 0:
        return {"skip": "credit_above_width"}
    pnl = 100 * (o["credit"] - bd._payoff(o["legs"], s_end) - 4 * bd.FEE)
    return {"pnl": pnl, "max_loss": max_loss, "ret": pnl / max_loss, "credit": o["credit"]}


# ---------------------------------------------------------------- one session file

def session_rows(und: str, day: date, path: str) -> list[dict]:
    base = {"und": und, "day": day}
    why = session_reason(day)
    if why:
        return [{**base, "variant": "x1540", "entry": e, "w": w, "model": m, "skip": why}
                for e in ENTRIES for w in WINGS for m in MODELS]
    try:
        p = bd.QuotePanel(bd.normalize(pd.read_parquet(path)))
    except Exception as e:  # noqa: BLE001 - counted as a skip
        return [{**base, "variant": "x1540", "entry": en, "w": w, "model": m, "skip": f"unreadable: {e!r}"[:80]}
                for en in ENTRIES for w in WINGS for m in MODELS]
    out = []

    def add(variant, entry, w, model, r):
        keep = {k: r[k] for k in ("pnl", "max_loss", "ret", "credit", "M", "wings_sold", "exit_minute") if k in r}
        out.append({**base, "variant": variant, "entry": entry, "w": w, "model": model, "skip": r.get("skip"), **keep})

    for e in ENTRIES:
        for w in WINGS:
            for m in MODELS:
                add("x1540", e, w, m, trade(p, e, w, m))
                add("x1530", e, w, m, trade(p, e, w, m, exit_min=EXIT_ALT))
                add("settle", e, w, m, settlement(p, e, w, m))
    if und == "SPY":
        for m in MODELS:
            add("bwings", ENTRIES[0], 0.0, m, trade(p, ENTRIES[0], None, m, legs_fn=legs_b))
    return out


def _job(args):
    return session_rows(*args)


def collect(dirs: dict[str, Path], workers: int = 4) -> pd.DataFrame:
    jobs = []
    for und, folder in dirs.items():
        for f in sorted(Path(folder).glob("*.parquet")):
            try:
                day = date.fromisoformat(f.stem)
            except ValueError:
                continue
            jobs.append((und, day, str(f)))
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, r in enumerate(ex.map(_job, jobs, chunksize=4), 1):
            rows += r
            if i % 200 == 0:
                print(f"  {i}/{len(jobs)} sessions", flush=True)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- statistics

def tstats(x) -> dict:
    x = pd.Series(x, dtype=float).dropna()
    n = len(x)
    if n < 3:
        return {"n": int(n), "mean": float(x.mean()) if n else None, "t": None}
    sd = x.std(ddof=1)
    w, lo = x[x > 0].sum(), -x[x < 0].sum()
    return {"n": int(n), "mean": float(x.mean()), "t": float(x.mean() / (sd / math.sqrt(n))) if sd > 0 else None,
            "win": float((x > 0).mean()), "pf": float(w / lo) if lo > 0 else None,
            "worst": float(x.min()), "best": float(x.max())}


def choose_cell(disc: pd.DataFrame) -> tuple[int, float]:
    """Highest t of per-trade returns; ties: smaller w, then earlier entry."""
    cells = []
    for (e, w), g in disc.groupby(["entry", "w"]):
        t = tstats(g["ret"])["t"]
        cells.append((-(t if t is not None else -1e9), w, e))
    _, w, e = min(cells)
    return int(e), float(w)


def daily(df: pd.DataFrame) -> pd.Series:
    """Mean return of the underlyings that traded each day."""
    return df.groupby("day")["ret"].mean().sort_index()


def verdict(d: dict) -> dict:
    b13 = (d["disc_mean"] is not None and d["disc_mean"] > 0 and d["pool_t"] is not None and d["pool_t"] >= T_BAR
           and d["pool_taker_mean"] is not None and d["pool_taker_mean"] > 0)
    if d.get("fresh_mean") is None:
        return {"pass": False, "provisional": True, "bars_1_3": bool(b13)}
    b4 = d["fresh_mean"] > 0 and d["fresh_t"] is not None and d["fresh_t"] >= T_FRESH
    return {"pass": bool(b13 and b4), "provisional": False, "bars_1_3": bool(b13), "bar_4": bool(b4),
            "pool_t_clears_2.33": d["pool_t"] is not None and d["pool_t"] >= T_SINGLE}


# ---------------------------------------------------------------- report

def _sel(df, variant="x1540", model="patient", entry=None, w=None, unds=None, start=None, end=None):
    m = (df["variant"] == variant) & (df["model"] == model) & df["skip"].isna()
    if entry is not None:
        m &= df["entry"] == entry
    if w is not None:
        m &= df["w"] == w
    if unds is not None:
        m &= df["und"].isin(unds)
    if start is not None:
        m &= df["day"] >= start
    if end is not None:
        m &= df["day"] <= end
    return df[m]


def report(df: pd.DataFrame, vix: pd.Series | None) -> dict:
    unds = sorted(df["und"].unique())
    fresh_unds = [u for u in ("QQQ", "IWM") if u in unds]
    disc = _sel(df, unds=["SPY"], start=DISC[0], end=DISC[1])
    E, W = choose_cell(disc)
    cell = dict(entry=E, w=W)
    pool_p = daily(_sel(df, **cell, start=CONF_START))
    pool_t = daily(_sel(df, model="taker", **cell, start=CONF_START))
    fresh = daily(_sel(df, **cell, unds=fresh_unds)) if fresh_unds else pd.Series(dtype=float)
    d = {"disc_mean": tstats(_sel(disc, **cell)["ret"])["mean"], "pool_t": tstats(pool_p)["t"],
         "pool_taker_mean": tstats(pool_t)["mean"],
         "fresh_mean": tstats(fresh)["mean"] if len(fresh) else None,
         "fresh_t": tstats(fresh)["t"] if len(fresh) else None}
    out = {"chosen_cell": {"entry": hhmm(E), "w": W}, "bars": d, "verdict": verdict(d),
           "underlyings": unds,
           "discovery_spy_2016_2021_patient": {f"{hhmm(e)} w{w}": tstats(_sel(disc, entry=e, w=w)["ret"])
                                               for e in ENTRIES for w in WINGS},
           "confirmation_pooled_patient": tstats(pool_p), "confirmation_pooled_taker": tstats(pool_t),
           "confirmation_pooled_mid": tstats(daily(_sel(df, model="mid", **cell, start=CONF_START))),
           "fresh_only_patient": tstats(fresh) if len(fresh) else None}
    rep = {}
    for u in unds:
        for per, (a, b) in {"2016-2021": (DISC[0], DISC[1]), "2022-": (CONF_START, None)}.items():
            for mdl in MODELS:
                for e in ENTRIES:
                    for w in WINGS:
                        rep[f"{u} {per} {mdl} {hhmm(e)} w{w}"] = tstats(_sel(df, model=mdl, entry=e, w=w, unds=[u],
                                                                              start=a, end=b)["ret"])
    out["all_cells"] = rep
    pooled_all = _sel(df, **cell)
    out["chosen_cell_rows"] = {
        "exit_1530_pooled_patient": tstats(daily(_sel(df, variant="x1530", **cell, start=CONF_START))),
        "settlement_pooled_patient": tstats(daily(_sel(df, variant="settle", **cell, start=CONF_START))),
        "settlement_spy_all_patient": tstats(_sel(df, variant="settle", **cell, unds=["SPY"])["ret"]),
        "b_wings_spy_0945_x1540": {m: tstats(_sel(df, variant="bwings", model=m, unds=["SPY"])["ret"])
                                   for m in MODELS},
        "by_year_per_underlying_patient": {f"{u} {y}": tstats(g["ret"]) for (u, y), g in
                                           pooled_all.groupby([pooled_all["und"],
                                                               pooled_all["day"].map(lambda x: x.year)])},
        "spy_daily_era_vs_before_patient": {
            "before 2022-11-14": tstats(_sel(df, **cell, unds=["SPY"], end=date(2022, 11, 13))["ret"]),
            "from 2022-11-14": tstats(_sel(df, **cell, unds=["SPY"], start=DAILY_ERA)["ret"])},
        "worst_trades_patient": pooled_all.nsmallest(5, "pnl")[["und", "day", "pnl", "max_loss", "ret"]]
        .assign(day=lambda x: x["day"].astype(str)).to_dict("records"),
        "max_loss_per_lot": {u: {"median": float(g["max_loss"].median()), "p90": float(g["max_loss"].quantile(0.9))}
                             for u, g in pooled_all.groupby("und")},
    }
    if vix is not None:
        pv = pooled_all["day"].map(lambda x: bd.prev_close(vix, x))
        out["chosen_cell_rows"]["prior_vix_patient"] = {
            lab: tstats(pooled_all.loc[(pv >= lo) & (pv < hi), "ret"]) for lo, hi, lab in VIX_BUCKETS}
    main = df[(df["variant"] == "x1540") & (df["model"] == "patient") & (df["entry"] == E) & (df["w"] == W)]
    out["skips_chosen_cell"] = {u: {"sessions": int(len(g)), **{str(k): int(v) for k, v in
                                                                g["skip"].fillna("traded").value_counts().items()}}
                                for u, g in main.groupby("und")}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--und", action="append", required=True, help="SYMBOL=folder of YYYY-MM-DD.parquet files")
    ap.add_argument("--vix", type=Path, default=None, help="CSV with DATE, CLOSE (CBOE VIX history)")
    ap.add_argument("--out", type=Path, default=Path("research/data/round5/w"))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--reuse", action="store_true", help="reuse out/trades.parquet for the underlyings it has")
    a = ap.parse_args()
    dirs = dict(x.split("=", 1) for x in a.und)
    a.out.mkdir(parents=True, exist_ok=True)
    cache = a.out / "trades.parquet"
    old = pd.read_parquet(cache) if a.reuse and cache.exists() else None
    if old is not None:
        old["day"] = pd.to_datetime(old["day"]).dt.date
        have = set(old["und"].unique())
        new = collect({u: f for u, f in dirs.items() if u not in have}, a.workers)
        df = pd.concat([old[old["und"].isin(dirs)], new], ignore_index=True)
    else:
        df = collect(dirs, a.workers)
    df.to_parquet(cache, index=False)
    vix = bd.load_vix(a.vix) if a.vix else None
    res = report(df, vix)
    (a.out / "w_results.json").write_text(json.dumps(res, indent=1, default=str))
    v, b = res["verdict"], res["bars"]
    print(f"W chosen cell {res['chosen_cell']} -> {'PASS' if v['pass'] else 'FAIL'}"
          f"{' (provisional, SPY only)' if v['provisional'] else ''}")
    print(f"  discovery mean {b['disc_mean']}, pooled t {b['pool_t']}, pooled taker mean {b['pool_taker_mean']}, "
          f"fresh mean {b['fresh_mean']} t {b['fresh_t']}")
    print(f"-> {a.out / 'w_results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
