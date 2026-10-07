"""Book F2-C's premise on free data: does the stock drift up after the breakout? (research/f2c_drift_prereg.md)

A call debit spread is still a long bet on the stock: for the same day it earns about its net delta times the move,
minus option costs. So before any option history is bought, this measures the stock's own drift after F2-C's entry
on the shares cache that research/strategy_f_intraday.py already wrote (no network):

    python research/f2c_drift.py

Variants (pre-registered): C-hold (no price stop, exit 15:55 ET) and C-orlow (F2-C's first-day stop at the opening-
range low). Both use F2's names only and the top 3 by RVOL5, as F2-C arms them.
Outputs: research/f2c_drift_results.json, research/f2c_drift.md
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentdesk.books import f_stocks_in_play as F  # noqa: E402
from agentdesk.books.f_report import clustered_t  # noqa: E402

_spec = importlib.util.spec_from_file_location("strategy_f_intraday", Path(__file__).resolve().parent / "strategy_f_intraday.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

PRIMARY = R.PRIMARY                     # F's scan inputs (universe, RVOL5 window)
TOP_C = 3                               # F2-C arms the top 3 of its own names
MFE_BP = (100, 200, 300)
MFE_ATR = (0.5, 1.0)
SENSITIVITY = [("all section 3 picks (top 5, any name)", {"f2_only": False}), ("slip 5 bp", {"slip_bp": 5.0})]


def f2_universe() -> set[str]:
    from agentdesk.config import load_config
    return {s.upper() for s in load_config()["books"]["F2_debit_spreads"]["universe"]}


def f2_universe_for_day(today: date, data: dict, names: set[str], cfg: dict) -> dict[str, dict]:
    """F2's names that pass the section 3 floors (price, ATR14, 20-day dollar volume) on prior data only. Unlike F's
    universe there is no top-130 S&P cap and no S&P membership test (prereg: "F2's 34 names only"), so names such as
    SHOP, or an F2 name outside the day's top 130, can be candidates."""
    names = set(names)
    return R.universe_for_day(today, data, names, [], {**cfg, "universe": {**cfg["universe"],
                                                                           "top_sp500_by_dollar_vol": len(names)}})


def f2_picks(rows: list, names: set[str], cfg: dict, top: int = TOP_C) -> list:
    """F2-C's arming rule on one day's scan rows: F2's names, green first candle, RVOL5 >= rvol5_min, top by RVOL5."""
    mine = [r for r in rows if r.symbol in names]
    return F.rank_candidates(mine, {**cfg, "top_n": top}).picks


# ------------------------------------------------------------------ one day
def simulate_hold_day(picks: list, bars_by_sym: dict, cfg, slip_bp: float = 2.0, half_day: bool = False,
                      or_low_stop: bool = False) -> list[dict]:
    """Buy-stop at the OR high (limit x1.0005, at most 3 sends) until 10:30 ET, sized floor(max_notional / limit),
    exit at the 15:55 ET open (12:55 on half-days). With or_low_stop, a bar at or below the OR low exits there (F2-C's
    first-day stop); it is checked from the bar after the entry bar. MFE/MAE use the bars after the entry bar."""
    ex_t = F.exit_time_et(cfg, half_day)
    exit_m = ex_t.hour * 60 + ex_t.minute
    by = {p.symbol: {b["t"]: b for b in bars_by_sym.get(p.symbol, [])} for p in picks}
    armed = {p.symbol: p for p in picks}
    sends: dict[str, int] = defaultdict(int)
    pos: dict[str, dict] = {}
    last: dict[str, float] = {}
    trades: list[dict] = []

    def close(sym, px, why):
        p = pos.pop(sym)
        trades.append({**p, "exit": px, "why": why, "pnl": (px - p["entry"]) * p["qty"],
                       "bp": (px / p["entry"] - 1) * 1e4,
                       "mfe_bp": (p["hi"] / p["entry"] - 1) * 1e4, "mae_bp": (p["lo"] / p["entry"] - 1) * 1e4,
                       "mfe_atr": (p["hi"] - p["entry"]) / p["atr"], "mae_atr": (p["lo"] - p["entry"]) / p["atr"]})

    for m in range(R.SCAN, exit_m + 1):
        for p in picks:
            s = p.symbol
            b = by[s].get(m)
            if s in pos and m == exit_m:
                close(s, F._dn(b["o"] if b else last.get(s, pos[s]["entry"]), slip_bp), f"exit {ex_t:%H:%M}")
                continue
            if b is None or m == exit_m:
                continue
            last[s] = b["c"]
            if s in pos:
                q = pos[s]
                q["hi"], q["lo"] = max(q["hi"], b["h"]), min(q["lo"], b["l"])
                if or_low_stop:
                    px = F.bar_stop_fill(b, q["or_low"], slip_bp)
                    if px is not None:
                        close(s, px, "OR-low stop")
            elif s in armed and m < R.CUTOFF and len(pos) < cfg["max_positions"] and b["h"] > p.or_high:
                limit = F.entry_limit(p.or_high)
                qty = math.floor(cfg["max_notional"] / limit + 1e-9) if limit > 0 else 0
                if qty < 1:
                    armed.pop(s)
                    continue
                fill = F.bar_entry_fill(b, p.or_high, limit, slip_bp)
                if fill is None:
                    sends[s] += 1
                    if sends[s] >= 3:
                        armed.pop(s)
                    continue
                armed.pop(s)
                pos[s] = {"symbol": s, "rank": p.rank, "rvol5": p.rvol5, "atr": p.atr, "entry": fill, "qty": qty,
                          "entry_t": m, "hi": fill, "lo": fill, "or_low": p.or_low}
    for o in list(pos):                                           # data ended early: flat at the last print
        close(o, F._dn(last.get(o, pos[o]["entry"]), slip_bp), "end of data")
    return trades


# ------------------------------------------------------------------ stats
def summarize(trades: list[dict], all_days: list[date], balance: float = R.BALANCE) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0}
    bps = [t["bp"] for t in trades]
    wins = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    losses = sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    by_day = defaultdict(float)
    for t in trades:
        by_day[t["day"]] += t["pnl"]
    series = [by_day.get(d, 0.0) for d in all_days] or [0.0]
    mu = sum(series) / len(series)
    mfe, mae = [t["mfe_bp"] for t in trades], [t["mae_bp"] for t in trades]
    exits = defaultdict(int)
    for t in trades:
        exits[t["why"]] += 1
    return {"trades": n, "win_rate": sum(1 for t in trades if t["pnl"] > 0) / n,
            "mean_bp": sum(bps) / n, "median_bp": statistics.median(bps),
            "pf": wins / -losses if losses < 0 else None, "t": clustered_t(bps, [t["day"] for t in trades]),
            "pnl": sum(t["pnl"] for t in trades), "ann_return": mu * 252 / balance, "exits": dict(exits),
            "mfe": {"mean_bp": sum(mfe) / n, "median_bp": statistics.median(mfe),
                    **{f"reach_{x}bp": sum(1 for v in mfe if v >= x) / n for x in MFE_BP},
                    **{f"reach_{x}atr": sum(1 for t in trades if t["mfe_atr"] >= x) / n for x in MFE_ATR}},
            "mae": {"mean_bp": sum(mae) / n, "median_bp": statistics.median(mae)}}


def passes(full: dict, first: dict, second: dict) -> bool:
    """Same bar as the replication: PF >= 1.1, t > 2, and a positive mean in both halves."""
    return bool(full.get("pf") and full["pf"] >= 1.1 and full.get("t") and full["t"] > 2
                and first.get("mean_bp", 0) > 0 and second.get("mean_bp", 0) > 0)


def verdict(hold: dict, hold_pass: bool) -> str:
    if not hold.get("trades") or hold["mean_bp"] <= 0:
        return "NO DRIFT"
    return "DRIFT" if hold_pass else "POSITIVE, NOT SIGNIFICANT"


# ------------------------------------------------------------------ backtest over the cache
def backtest(store, data, sp500, extras, dates: list[date], cfg, names: set[str] | None, slip_bp: float = 2.0,
             or_low_stop: bool = False) -> list[dict]:
    trades, hist = [], []
    for d in dates:
        orb = store.read_bars(store.or_path(d)) if store.or_path(d).exists() else {}
        if len(hist) >= 14 and orb and store.day_path(d).exists():
            uni = (f2_universe_for_day(d, data, names, cfg) if names is not None
                   else R.universe_for_day(d, data, sp500, extras, cfg))
            res = R.scan_day(uni, orb, hist[-14:], cfg)
            picks = f2_picks(res.rows, names, cfg) if names is not None else res.picks
            bars = store.read_bars(store.day_path(d))
            half = bool(bars) and max(b["t"] for bs in bars.values() for b in bs) < 15 * 60
            for t in simulate_hold_day(picks, bars, cfg, slip_bp, half, or_low_stop):
                trades.append({**t, "day": d})
        hist.append({s: sum(b["v"] for b in bs) for s, bs in orb.items()})
    return trades


def _block(trades, dates) -> dict:
    d1 = [d for d in dates if d.year <= 2020]
    d2 = [d for d in dates if d.year >= 2021]
    first = summarize([t for t in trades if t["day"].year <= 2020], d1)
    second = summarize([t for t in trades if t["day"].year >= 2021], d2)
    full = summarize(trades, dates)
    return {"full": full, "2016_2020": first, "2021_2026": second, "pass": passes(full, first, second),
            "by_year": {y: summarize([t for t in trades if t["day"].year == y], [d for d in dates if d.year == y])
                        for y in sorted({d.year for d in dates})}}


def report(store, data, sp500, extras, dates: list[date], out_dir: Path, names: set[str]) -> dict:
    hold = _block(backtest(store, data, sp500, extras, dates, PRIMARY, names), dates)
    orlow = _block(backtest(store, data, sp500, extras, dates, PRIMARY, names, or_low_stop=True), dates)
    res = {"prereg": "research/f2c_drift_prereg.md", "period": [str(dates[0]), str(dates[-1])],
           "names": sorted(names), "c_hold": hold, "c_orlow": orlow, "verdict": verdict(hold["full"], hold["pass"]),
           "sensitivity": {},
           "caveats": ["Survivorship bias: today's S&P 500 list is used for every year.",
                       "The universe is F2's names with the section 3 floors (2026-10-07; before that it was F's "
                       "S&P-130 universe filtered to F2's names). Names the shares cache never pulled have no bars "
                       "and can't be candidates.",
                       "The cache holds each candidate's day only, so this measures the same-day drift; F2-C holds up "
                       "to 3 days, which only the option replay (research/f2_real_quotes.py) covers.",
                       "Shares only: no option prices, so option costs, skew and IV are not tested here.",
                       "Fills on 1-minute bars, 2 bp slippage per side; MFE/MAE from the bars after the entry bar."]}
    for label, over in SENSITIVITY:
        tr = backtest(store, data, sp500, extras, dates, PRIMARY, None if over.get("f2_only") is False else names,
                      slip_bp=over.get("slip_bp", 2.0))
        res["sensitivity"][label] = summarize(tr, dates)
    (out_dir / "f2c_drift_results.json").write_text(json.dumps(res, indent=1, default=str))
    _write_md(res, out_dir / "f2c_drift.md")
    return res


def _fmt(s: dict) -> str:
    if not s.get("trades"):
        return "no trades"
    f = lambda x, p=2: "n/a" if x is None else f"{x:.{p}f}"
    return (f"{s['trades']} trades, win {100 * s['win_rate']:.1f}%, mean {f(s['mean_bp'], 1)} bp, median "
            f"{f(s['median_bp'], 1)} bp, PF {f(s['pf'])}, t {f(s['t'])}, ann {100 * s['ann_return']:.1f}%")


def _fmt_mfe(s: dict) -> str:
    if not s.get("trades"):
        return "no trades"
    m, a = s["mfe"], s["mae"]
    return (f"MFE mean {m['mean_bp']:.0f} bp, median {m['median_bp']:.0f} bp; reaches +1% {100 * m['reach_100bp']:.0f}%, "
            f"+2% {100 * m['reach_200bp']:.0f}%, +3% {100 * m['reach_300bp']:.0f}%, +0.5 ATR {100 * m['reach_0.5atr']:.0f}%, "
            f"+1 ATR {100 * m['reach_1.0atr']:.0f}%. MAE mean {a['mean_bp']:.0f} bp, median {a['median_bp']:.0f} bp")


def _write_md(res: dict, path: Path) -> None:
    v = res["verdict"]
    note = {"NO DRIFT": "The stock doesn't drift up after F2-C's entry, so call spreads on it are expected to lose on "
                        "paper too. Reported to Evan, who decides whether F2-C keeps running.",
            "DRIFT": "The stock drifts up after F2-C's entry and passes the bar; the option replay decides whether "
                     "spreads keep the edge after their costs.",
            "POSITIVE, NOT SIGNIFICANT": "Positive but under the bar; reported to Evan."}[v]
    lines = [f"**{v}**: F2-C same-day stock drift, {res['period'][0]} to {res['period'][1]}, after 2 bp costs per side. "
             f"{note}", "", f"Pre-registration: `{res['prereg']}`. Names: {len(res['names'])} (F2's universe). Pass bar: "
             "PF >= 1.1, t > 2 (clustered by day), positive in both 2016-2020 and 2021-2026.", ""]
    for key, name in (("c_hold", "C-hold (no stop, exit 15:55 ET)"), ("c_orlow", "C-orlow (stop at the OR low)")):
        b = res[key]
        lines += [f"## {name}", "", f"- Full: {_fmt(b['full'])}", f"- 2016-2020: {_fmt(b['2016_2020'])}",
                  f"- 2021-2026: {_fmt(b['2021_2026'])}", f"- Passes the bar: {'yes' if b['pass'] else 'no'}",
                  f"- {_fmt_mfe(b['full'])}", "", "By year:", ""]
        lines += [f"- {y}: {_fmt(s)}" for y, s in b["by_year"].items()] + [""]
    lines += ["Sensitivity (reported, never selected from):", ""]
    lines += [f"- {k}: {_fmt(s)}" for k, s in res["sensitivity"].items()]
    lines += ["", "Caveats:", ""] + [f"- {c}" for c in res["caveats"]]
    path.write_text("\n".join(lines) + "\n")


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-04")
    ap.add_argument("--end", default="2026-09-25")
    ap.add_argument("--data", default=str(ROOT / "research" / "data" / "f_intraday"))
    a = ap.parse_args()
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    store = R.Store(Path(a.data))
    if not (store.root / "constituents.csv").exists():
        raise SystemExit(f"No cache at {store.root}: run research/strategy_f_intraday.py --pull-only first.")
    sp500 = R.load_constituents(store)
    extras = list(F.AI_LIST)
    names = f2_universe()
    symbols = sorted(sp500 | set(extras) | names | {"SPY"})
    data = {s: store.read_daily(s) for s in symbols if store.daily_path(s).exists()}
    dates = [b["d"] for b in data.get("SPY", []) if start <= b["d"] <= end]
    if not dates:
        raise SystemExit("No SPY daily bars cached; run research/strategy_f_intraday.py --pull-only first.")
    missing = sorted(n for n in names if n not in data)
    if missing:
        print(f"warning: no cached daily bars for F2 names {missing}; they can't be candidates. Pull them with "
              "research/strategy_f_intraday.py --pull-only to cover all of F2's names.", flush=True)
    res = report(store, data, sp500, extras, dates, ROOT / "research", names)
    print((ROOT / "research" / "f2c_drift.md").read_text())
    print(f"verdict: {res['verdict']}")


if __name__ == "__main__":
    main()
