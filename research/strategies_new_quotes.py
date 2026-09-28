"""Candidates F1 and F2 (research/strategies_new_prereg.md) replayed on real 1-minute SPY 0DTE NBBO quotes.

Same loaders, fill models (mid; patient = mid -+ 1c capped at natural; taker = natural) and exit loop as
research/bd_real_quotes.py, so the numbers line up with the B/D replay. Strikes are placed exactly as in the
modeled backtest (prior VIX close, 0.80 RTH factor, bar-share of the session left).
  F1        13:30 ET iron condor, shorts ceil/floor(S +- 0.9 EM), $2 wings, TP 50%, stop 2x credit, close 15:25 ET
  F2        10:00 ET put credit spread, short floor(S - 0.9 EM), long $2 lower, same exits
  F2-trend  F2 only when the prior SPY close is above the average of the prior 50 closes (needs --spy bars)
All skip when the opening credit after fees is below $0.10.
F3 (0DTE/1DTE calendar) and F4 (overnight put spread) need next-day-expiry quotes, which the 0DTE pull lacks.

Usage (Mac, repo root, after the ThetaData pull):
    .venv/bin/python research/strategies_new_quotes.py --quotes data/thetadata/spy_0dte \\
        --out data/thetadata/new_results [--spy data/spy_1m]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bd_real_quotes as bd  # noqa: E402
import strategies_new as sn  # noqa: E402

F1_ENTRY, F2_ENTRY, CLOSE_MIN = 13 * 60 + 30, 10 * 60, 15 * 60 + 25
MIN_CREDIT = 0.10
BOOKS = ("F1", "F2", "F2-trend")


def _bd(legs):
    return [(r, float(k), q) for r, k, q, _ in legs]


def legs_F1(S: float, vix: float):
    return _bd(sn.legs_condor(S, 0.9 * sn.leg_sd(vix, 0, 0, sn.K_1330) * S))


def legs_F2(S: float, vix: float):
    return _bd(sn.legs_put_spread(S, 0.9 * sn.leg_sd(vix, 0, 0, sn.K_1000) * S))


def daily_closes(bars: pd.DataFrame) -> pd.Series:
    b = bars[(bars.minute >= bd.OPEN) & (bars.minute < bd.CLOSE)].sort_values(["date", "minute"])
    return b.groupby("date").c.last()


def gate_for(closes: pd.Series, d: date, n: int = 50) -> bool:
    prior = closes[closes.index < d].sort_index()
    return sn.sma_gate(prior.to_numpy(), len(prior), n)


def day_rows(p: bd.QuotePanel, d: date, vix: float, gate: bool) -> list[dict]:
    rows = []
    base = {"date": d, "year": d.year, "era": "daily" if d >= bd.DAILY_ERA else "mon-wed-fri", "vix": vix}
    plans = []
    s1, s2 = bd.spot(p, F1_ENTRY), bd.spot(p, F2_ENTRY)
    if s1 is not None:
        plans.append(("F1", legs_F1(s1, vix), F1_ENTRY))
    if s2 is not None:
        plans.append(("F2", legs_F2(s2, vix), F2_ENTRY))
        if gate:
            plans.append(("F2-trend", legs_F2(s2, vix), F2_ENTRY))
    for book, legs, entry in plans:
        for model in bd.MODELS:
            r = bd.replay(p, legs, entry, CLOSE_MIN, model, width=2)
            if r and r["credit"] >= MIN_CREDIT:
                rows.append({**base, "book": book, "model": model, **r})
    return rows


def run(quotes: Path, out: Path, spy_dir: Path | None, vix_path: Path | None) -> dict:
    vix = bd.load_vix(vix_path)
    bars = bd.load_spy_bars(spy_dir)
    closes = daily_closes(bars) if bars is not None else None
    rows, skipped = [], {"short_day": 0, "unreadable": 0, "no_vix": 0}
    files = sorted(quotes.glob("*.parquet"))
    for f in files:
        d = date.fromisoformat(f.stem)
        try:
            p = bd.QuotePanel(bd.normalize(pd.read_parquet(f)))
        except Exception as e:  # noqa: BLE001
            print(f"{f.name}: unreadable ({e})", file=sys.stderr)
            skipped["unreadable"] += 1
            continue
        if p.last_minute < CLOSE_MIN:
            skipped["short_day"] += 1
            continue
        vx = bd.prev_close(vix, d)
        if math.isnan(vx):
            skipped["no_vix"] += 1
            continue
        rows += day_rows(p, d, vx, gate=bool(closes is not None and gate_for(closes, d)))
    df = pd.DataFrame(rows)
    out.mkdir(parents=True, exist_ok=True)
    if df.empty:
        raise SystemExit(f"no usable trades in {quotes} ({len(files)} files, skipped {skipped})")
    df.to_parquet(out / "trades.parquet")
    text, res = _report(df, skipped, len(files), closes is not None)
    (out / "report.md").write_text(text)
    (out / "results.json").write_text(json.dumps(res, indent=1, default=str))
    print(text)
    return res


def _report(df: pd.DataFrame, skipped: dict, n_files: int, have_gate: bool):
    res, L = {}, []
    L += [f"# Candidates F1 and F2 on real SPY 0DTE quotes ({df.date.min()} to {df.date.max()})", "",
          f"{n_files} quote files, {df.date.nunique()} sessions with a trade, skipped {skipped}.",
          "Returns are % of max risk per trade, fees included. patient = mid -+ 1c (the approved paper fill rule).",
          "" if have_gate else "F2-trend needs --spy bars for the 50-day average; not run.", ""]
    for book in BOOKS:
        sub = df[df.book == book]
        if sub.empty:
            continue
        L += [f"## {book}", "", "| period | model | stats | avg $/1-lot | worst $ | max DD $ | avg credit $ |",
              "|---|---|---|---|---|---|---|"]
        for model in bd.MODELS:
            sm = sub[sub.model == model].sort_values("date")
            periods = [("all", sm)] + [(e, sm[sm.era == e]) for e in ("mon-wed-fri", "daily")] + \
                      [(str(y), sm[sm.year == y]) for y in sorted(sm.year.unique())]
            for name, x in periods:
                if len(x) == 0:
                    continue
                s = bd.summ(x.ret)
                s.update({"avg_usd": float(x.pnl.mean() * 100), "worst_usd": float(x.pnl.min() * 100),
                          "max_dd_usd": sn.max_drawdown(x.pnl * 100), "avg_credit_usd": float(x.credit.mean() * 100),
                          "exits": {str(k): int(v) for k, v in x.why.value_counts().items()}})
                res[f"{book}|{model}|{name}"] = s
                L.append(f"| {name} | {model} | {bd.fmt(s)} | {s['avg_usd']:+.1f} | {s['worst_usd']:+.0f} | "
                         f"{s['max_dd_usd']:+.0f} | {s['avg_credit_usd']:.0f} |")
        L.append("")
    return "\n".join(L), res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quotes", type=Path, default=Path("data/thetadata/spy_0dte"))
    ap.add_argument("--out", type=Path, default=Path("data/thetadata/new_results"))
    ap.add_argument("--spy", type=Path, default=None)
    ap.add_argument("--vix", type=Path, default=None)
    a = ap.parse_args()
    run(a.quotes, a.out, a.spy, a.vix)


if __name__ == "__main__":
    main()
