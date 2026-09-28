"""Book F daily study, rebuilt (HANDOFF v3.1 section 7F; docs/BOOK_F_HANDOFF.md section 5 "Also").

The original research/strategy_f.py lives only in the claude.ai session that designed F. This script is rebuilt from
the definitions that section 7F states, and checks itself against the published 2013-2018 numbers (`reference`) when
run on the same data (github.com/CNuge/kaggle-code, stock_data/individual_stocks_5yr.zip).

    python research/strategy_f_daily.py <folder of per-symbol CSVs> <out.json>

The folder holds one CSV per symbol with `date,open,high,low,close,volume` (the Kaggle files' extra `Name` column and
`_data` suffix are fine). For 2016-2026 use research/data/f_intraday/daily/, written by strategy_f_intraday.py.

Definitions (section 7F):
  universe       prior 20-day mean dollar volume >= $50M and prior close >= $10 (known before the event day)
  RVOL           the day's volume / its prior 20-day mean volume
  near high/low  close in the top / bottom 30% of the day's range
  returns        in excess of the equal-weight universe over the same window, 10 bp round trip
  t              Newey-West on the daily mean of trades grouped by entry day, lag = holding days
  mean           per trade (`mean_bp`) and per entry day (`day_mean_bp`, the figure 7F's table reports)
  momentum       RVOL >= 2 / 3, close-to-close up >= 3% / 5%, close near high; buy the next open, sell the close
                 1 / 5 / 10 / 20 days later (16 cells, Bonferroni bar |t| > 2.96)
  down side      the mirror: down >= 3% / 5% with the close near the low, same holds (long, net of cost; 7F's row
                 matches the two 1-day, 3% cells)
  control        up >= 3% / 5% on RVOL < 1.5 (no close-location condition); buy the next open, sell the next close
  gap chase      open >= 2% / 4% above the prior close; buy the open, sell the close
  gap down       open <= -2%; buy the open, sell the close (noted in 7F, not pre-registered)
"""
from __future__ import annotations

import csv
import json
import math
import sys
from datetime import date
from pathlib import Path
from statistics import NormalDist

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agentdesk.books.f_stocks_in_play import AI_LIST  # noqa: E402

COST = 0.0010                       # 10 bp round trip
MIN_DOLLAR_VOL = 50e6
MIN_PRICE = 10.0
LOOKBACK = 20
NEAR = 0.30                         # top / bottom 30% of the range
HOLDS = (1, 5, 10, 20)

# section 7F table (2013-02 -> 2018-02, 505 S&P 500 stocks); momentum reports only its range
REFERENCE = {
    "momentum": {"n": [548, 2296], "mean_bp": [-27, 25], "best_t": 1.31},
    "down 3% hold 1d": {"n": [1135, 3029], "t": [-2.2, -0.6]},
    "control up>=3%": {"n": 8359, "mean_bp": -18, "t": -4.5},
    "control up>=5%": {"n": 1017, "mean_bp": -31, "t": -2.2},
    "gap up>=2%": {"n": 7160, "mean_bp": -29, "t": -8.2},
    "gap up>=4%": {"n": 1789, "mean_bp": -44, "t": -4.6},
    "gap down<=-2% (not pre-registered)": {"mean_bp": 11, "t": 2.3},
}


# ------------------------------------------------------------------ data
def load_folder(folder) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in sorted(Path(folder).glob("*.csv")):
        sym = f.stem[:-5] if f.stem.endswith("_data") else f.stem
        rows = []
        with f.open(newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    rows.append({"d": date.fromisoformat(r["date"][:10]), "o": float(r["open"]), "h": float(r["high"]),
                                 "l": float(r["low"]), "c": float(r["close"]), "v": float(r["volume"])})
                except (KeyError, TypeError, ValueError):
                    continue            # a missing field drops that day
        if rows:
            out[sym] = sorted(rows, key=lambda x: x["d"])
    return out


def close_loc(b: dict) -> float | None:
    rng = b["h"] - b["l"]
    return (b["c"] - b["l"]) / rng if rng > 0 else None


def hold_window(i: int, hold: int) -> tuple[int, int]:
    """Buy the open after event day i, sell the close `hold` trading days after that open's day - 1."""
    return i + 1, i + hold


class Panel:
    """Per-symbol daily bars on one shared trading calendar (index = position in the sorted union of dates)."""

    def __init__(self, data: dict[str, list[dict]]):
        self.dates = sorted({b["d"] for bars in data.values() for b in bars})
        self.idx = {d: i for i, d in enumerate(self.dates)}
        self.bars: dict[str, dict[int, dict]] = {s: {self.idx[b["d"]]: b for b in bars} for s, bars in data.items()}
        self.pos: dict[str, list[int]] = {s: sorted(m) for s, m in self.bars.items()}
        self._bench: dict[tuple[int, int], float] = {}
        self._feat: dict[tuple[str, int], dict | None] = {}

    def features(self, sym: str, i: int) -> dict | None:
        key = (sym, i)
        if key in self._feat:
            return self._feat[key]
        f = None
        b, pos = self.bars[sym].get(i), self.pos[sym]
        if b is not None:
            k = _bisect(pos, i)
            if k >= LOOKBACK:
                prior = [self.bars[sym][j] for j in pos[k - LOOKBACK:k]]
                avg_v = sum(p["v"] for p in prior) / LOOKBACK
                f = {"rvol": b["v"] / avg_v if avg_v > 0 else None,
                     "dollar_vol20": sum(p["v"] * p["c"] for p in prior) / LOOKBACK,
                     "prior_close": prior[-1]["c"], "ret": b["c"] / prior[-1]["c"] - 1,
                     "gap": b["o"] / prior[-1]["c"] - 1, "loc": close_loc(b)}
        self._feat[key] = f
        return f

    def eligible(self, sym: str, i: int) -> bool:
        f = self.features(sym, i)
        return bool(f) and f["dollar_vol20"] >= MIN_DOLLAR_VOL and f["prior_close"] >= MIN_PRICE

    def bench(self, e: int, x: int) -> float | None:
        """Equal-weight return of every name with an open on day e and a close on day x."""
        key = (e, x)
        if key not in self._bench:
            rs = [m[x]["c"] / m[e]["o"] - 1 for m in self.bars.values() if e in m and x in m and m[e]["o"] > 0]
            self._bench[key] = sum(rs) / len(rs) if rs else None
        return self._bench[key]

    def excess(self, sym: str, e: int, x: int) -> float | None:
        m = self.bars[sym]
        if e not in m or x not in m or m[e]["o"] <= 0:
            return None
        bm = self.bench(e, x)
        return None if bm is None else m[x]["c"] / m[e]["o"] - 1 - bm

    def events(self, rule) -> list[tuple[str, int]]:
        out = []
        for s in sorted(self.bars):
            for i in self.pos[s]:
                if self.eligible(s, i) and rule(self.features(s, i)):
                    out.append((s, i))
        return out

    def trade_returns(self, events, entry: str, hold: int, cost: float) -> list[dict]:
        """entry 'open': buy the event day's open, sell its close (hold 0). 'next_open': hold_window(i, hold)."""
        out = []
        for s, i in events:
            e, x = (i, i) if entry == "open" else hold_window(i, hold)
            if x >= len(self.dates):
                continue
            r = self.excess(s, e, x)
            if r is not None:
                out.append({"sym": s, "entry": self.dates[e], "ret": r - cost})
        return out


def _bisect(xs: list[int], v: int) -> int:
    lo, hi = 0, len(xs)
    while lo < hi:
        mid = (lo + hi) // 2
        if xs[mid] < v:
            lo = mid + 1
        else:
            hi = mid
    return lo


# ------------------------------------------------------------------ rules
def momentum_rule(rvol_min: float, up_min: float):
    return lambda f: (f["rvol"] or 0) >= rvol_min and f["ret"] >= up_min and (f["loc"] or 0) >= 1 - NEAR


def down_rule(rvol_min: float, down_min: float):
    return lambda f: (f["rvol"] or 0) >= rvol_min and f["ret"] <= -down_min and f["loc"] is not None and f["loc"] <= NEAR


def control_rule(up_min: float):
    return lambda f: f["rvol"] is not None and f["rvol"] < 1.5 and f["ret"] >= up_min


def gap_rule(size: float, up: bool = True):
    return (lambda f: f["gap"] >= size) if up else (lambda f: f["gap"] <= -size)


def momentum_cells() -> list[dict]:
    return [{"rvol_min": r, "up_min": u, "hold": h} for r in (2.0, 3.0) for u in (0.03, 0.05) for h in HOLDS]


def bonferroni_t(cells: int, alpha: float = 0.05) -> float:
    return NormalDist().inv_cdf(1 - alpha / (2 * cells))


# ------------------------------------------------------------------ statistics
def nw_t(xs: list[float], lag: int) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    m = sum(xs) / n
    d = [x - m for x in xs]
    s = sum(v * v for v in d) / n
    for k in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - k / (lag + 1)) * sum(d[j] * d[j - k] for j in range(k, n)) / n
    return m / math.sqrt(s / n) if s > 0 else float("nan")


def _stats(rows: list[dict], lag: int) -> dict:
    if not rows:
        return {"n": 0, "days": 0, "mean_bp": None, "day_mean_bp": None, "win": None, "t": None}
    by_day: dict[date, list[float]] = {}
    for r in rows:
        by_day.setdefault(r["entry"], []).append(r["ret"])
    daily = [sum(v) / len(v) for _, v in sorted(by_day.items())]
    t = nw_t(daily, lag)
    return {"n": len(rows), "days": len(by_day), "mean_bp": sum(r["ret"] for r in rows) / len(rows) * 1e4,
            "day_mean_bp": sum(daily) / len(daily) * 1e4,
            "win": sum(r["ret"] > 0 for r in rows) / len(rows), "t": None if math.isnan(t) else t}


def summarize(rows: list[dict], lag: int) -> dict:
    s = _stats(rows, lag)
    ds = sorted({r["entry"] for r in rows})
    if ds:
        cut = ds[len(ds) // 2]
        s["halves"] = [_stats([r for r in rows if r["entry"] < cut], lag), _stats([r for r in rows if r["entry"] >= cut], lag)]
    else:
        s["halves"] = []
    s["periods"] = {name: _stats([r for r in rows if lo <= r["entry"] <= hi], lag)
                    for name, lo, hi in (("2013-2018", date(2013, 1, 1), date(2018, 12, 31)),
                                         ("2016-2020", date(2016, 1, 1), date(2020, 12, 31)),
                                         ("2021-2026", date(2021, 1, 1), date(2026, 12, 31)))}
    s["ai_2023_2026"] = _stats([r for r in rows if r["sym"] in AI_LIST and r["entry"] >= date(2023, 1, 1)], lag)
    return s


# ------------------------------------------------------------------ the study
def study(p: Panel) -> list[dict]:
    tests = []
    for c in momentum_cells():
        ev = p.events(momentum_rule(c["rvol_min"], c["up_min"]))
        rows = p.trade_returns(ev, "next_open", c["hold"], COST)
        tests.append({"test": f"momentum rvol>={c['rvol_min']:g} up>={c['up_min']:.0%} hold {c['hold']}d", **c,
                      "cost_round_trip": COST, **summarize(rows, c["hold"])})
    for c in momentum_cells():
        ev = p.events(down_rule(c["rvol_min"], c["up_min"]))
        rows = p.trade_returns(ev, "next_open", c["hold"], COST)
        tests.append({"test": f"down rvol>={c['rvol_min']:g} down>={c['up_min']:.0%} hold {c['hold']}d", **c,
                      "cost_round_trip": COST, **summarize(rows, c["hold"])})
    for up in (0.03, 0.05):
        rows = p.trade_returns(p.events(control_rule(up)), "next_open", 1, COST)
        tests.append({"test": f"control up>={up:.0%}", **summarize(rows, 1)})
    for g in (0.02, 0.04):
        rows = p.trade_returns(p.events(gap_rule(g)), "open", 0, COST)
        tests.append({"test": f"gap up>={g:.0%}", **summarize(rows, 1)})
    rows = p.trade_returns(p.events(gap_rule(0.02, up=False)), "open", 0, COST)
    tests.append({"test": "gap down<=-2% (not pre-registered)", **summarize(rows, 1)})
    return tests


def run(folder, out) -> dict:
    data = load_folder(folder)
    p = Panel(data)
    res = {"source": "research/strategy_f_daily.py (rebuilt from HANDOFF v3.1 section 7F)",
           "folder": str(folder), "symbols": len(data),
           "period": [str(p.dates[0]), str(p.dates[-1])] if p.dates else None,
           "bonferroni_t": bonferroni_t(16), "cost_round_trip": COST,
           "reference_2013_2018": REFERENCE, "tests": study(p)}
    Path(out).write_text(json.dumps(res, indent=1, default=str))
    return res


def _fmt(x, spec):
    return "—" if x is None else format(x, spec)


def print_table(res: dict) -> None:
    print(f"{res['symbols']} symbols, {res['period'][0]} -> {res['period'][1]}; Bonferroni |t| > {res['bonferroni_t']:.2f}")
    print(f"{'test':46s} {'n':>6s} {'bp/trade':>8s} {'bp/day':>7s} {'win':>5s} {'t':>6s}   halves (bp / t)")
    for t in res["tests"]:
        hv = "  ".join(f"{_fmt(h['mean_bp'], '+.0f')}/{_fmt(h['t'], '+.1f')}" for h in t["halves"])
        print(f"{t['test']:46s} {t['n']:6d} {_fmt(t['mean_bp'], '+8.1f')} {_fmt(t['day_mean_bp'], '+7.1f')} {_fmt(t['win'], '5.0%')} "
              f"{_fmt(t['t'], '+6.2f')}   {hv}")


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        raise SystemExit(__doc__)
    print_table(run(argv[0], argv[1]))


if __name__ == "__main__":
    main()
