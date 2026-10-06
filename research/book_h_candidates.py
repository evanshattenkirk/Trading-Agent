"""New book candidates H1-H3 (shares), rules frozen in research/book_h_candidates_prereg.md before this ran.

  H1  SPY overnight hold: buy at 15:55 ET, sell at the next session's 09:35 ET.
  H2  H1 only when the 15:55 price is below the previous session's close.
  H3  RSI(2) < 10 above the 200-day SMA, buy at 15:55 ET; sell at the first 15:55 price above the 5-day SMA.

Stage 1 (default): S&P 500 CFD 1-minute bars 2005-2020 (data/spx_rth_1m.pkl from load_oanda.py), in-sample
2005-2014, out-of-sample 2015-01..2020-05, plus SPY 5-minute 2025-04..2026-03 as an information-only recent check.
Holdout (Mac): SPY SIP 1-minute bars (bd_real_quotes.py fetch-spy layout), judged 2020-06-01..2026-09-30.

    python research/book_h_candidates.py                                   # stage 1, from research/
    .venv/bin/python research/book_h_candidates.py --spy-1m data/spy_1m --holdout     # Mac holdout
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import nyse_calendar  # noqa: E402
from agentdesk.indicators import RSI  # noqa: E402

OPEN, CLOSE = 570, 960
# (buy, sell) cost per side in bp of notional; every sale also pays 0.3 bp of regulatory fees
SALE_FEE = 0.3
COSTS = {"mid1": (0.131, 0.131 + SALE_FEE), "taker": (1.0, 1.0 + SALE_FEE)}
SENS_COSTS = {"cost 0.5bp": (0.5, 0.5 + SALE_FEE), "cost 2bp": (2.0, 2.0 + SALE_FEE)}
ALL_COSTS = {**COSTS, **SENS_COSTS}
EXTRA_CLOSURES = {date(2007, 1, 2), date(2012, 10, 29), date(2012, 10, 30)}
PERIODS = {"is": (date(2005, 1, 1), date(2014, 12, 31)), "oos": (date(2015, 1, 1), date(2020, 5, 31))}
HOLDOUT = (date(2020, 6, 1), date(2026, 9, 30))
T_OOS, T_HOLDOUT = 2.33, 1.65
TIMES = {"p0931": 571, "p0935": 575, "p1000": 600, "p1500": 900, "p1555": 955}


# ---------------------------------------------------------------- calendar

class Calendar:
    def __init__(self, y0: int, y1: int):
        self.closed = nyse_calendar.holidays_between(y0 - 1, y1 + 1) | EXTRA_CLOSURES

    def _open(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.closed

    def next_session(self, d: date) -> date:
        d += timedelta(days=1)
        while not self._open(d):
            d += timedelta(days=1)
        return d

    def prev_session(self, d: date) -> date:
        d -= timedelta(days=1)
        while not self._open(d):
            d -= timedelta(days=1)
        return d


# ---------------------------------------------------------------- session prices

def _prices(bars: pd.DataFrame, bar_min: int) -> pd.DataFrame:
    """bars: day, hm (bar start, ET minutes), close. Price at T = close of the last bar that ends by T."""
    b = bars[(bars.hm >= OPEN) & (bars.hm < CLOSE)].sort_values(["day", "hm"])
    g = b.groupby("day")
    out = pd.DataFrame(index=sorted(b.day.unique()))
    for name, t in TIMES.items():
        if bar_min > 1 and (t - OPEN) % bar_min:
            out[name] = np.nan
            continue
        sub = b[b.hm + bar_min <= t]
        out[name] = sub.groupby("day").close.last()
    out["close"] = g.close.last()
    return out


def session_prices_1m(bars: pd.DataFrame) -> pd.DataFrame:
    return _prices(bars, 1)


def session_prices_5m(bars: pd.DataFrame) -> pd.DataFrame:
    return _prices(bars, 5)


def load_oanda(path: Path) -> pd.DataFrame:
    df = pd.read_pickle(path)                       # already only sessions with >= 370 RTH minutes
    return session_prices_1m(df[["day", "hm", "close"]])


def load_spy_5m(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    et = pd.to_datetime(d["Datetime"], utc=True).dt.tz_convert("America/New_York")
    bars = pd.DataFrame({"day": et.dt.date, "hm": et.dt.hour * 60 + et.dt.minute, "close": d["Close"]})
    full = bars.groupby("day").size()
    bars = bars[bars.day.isin(full[full == 78].index)]
    return session_prices_5m(bars)


def load_spy_1m(folder: Path) -> pd.DataFrame:
    files = sorted(Path(folder).glob("*.parquet"))
    b = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    b = b.rename(columns={"date": "day", "minute": "hm", "c": "close"})
    b["day"] = pd.to_datetime(b["day"]).dt.date
    b = b[(b.hm >= OPEN) & (b.hm < CLOSE)].drop_duplicates(["day", "hm"])
    half = nyse_calendar.half_days_between(2015, 2027)
    n = b.groupby("day").size()
    keep = [d for d in n.index if n[d] >= 370 and d not in half]
    return session_prices_1m(b[b.day.isin(keep)][["day", "hm", "close"]])


# ---------------------------------------------------------------- trades

def trade_bp(entry: float, exit_: float, model: str) -> float:
    buy, sell = ALL_COSTS[model]
    return (exit_ / entry - 1) * 1e4 - buy - sell


def down_day(px: pd.DataFrame, d: date, prev: date) -> bool:
    return px.at[d, "p1555"] < px.at[prev, "close"]


def down_half_pct(px: pd.DataFrame, d: date, prev: date) -> bool:
    return px.at[d, "p1555"] <= px.at[prev, "close"] * 0.995


def down_last_hour(px: pd.DataFrame, d: date, prev: date) -> bool:
    return px.at[d, "p1555"] < px.at[d, "p1500"]


def overnight(px: pd.DataFrame, cal: Calendar, model: str, cond=None, exit_col: str = "p0935",
              weekday_only: bool = False, div_bp: float = 0.0) -> pd.Series:
    """bp per session d for the night d -> next session (0 when not traded)."""
    have = set(px.index)
    out = {}
    for d in px.index:
        nxt = cal.next_session(d)
        v = 0.0
        ok = nxt in have and not (weekday_only and (nxt - d).days > 1)
        if ok and cond is not None:
            prev = cal.prev_session(d)
            ok = prev in have and cond(px, d, prev)
        if ok:
            e, x = px.at[d, "p1555"], px.at[nxt, exit_col]
            if not (np.isnan(e) or np.isnan(x)):
                v = trade_bp(e, x, model) + div_bp
        out[d] = v
    return pd.Series(out)


def rsi2_dip(px: pd.DataFrame, model: str, rsi_max: float = 10.0, time_stop: int | None = None):
    """H3 on the full sessions in px (in order). Returns (daily bp Series, trades list)."""
    buy, sell = ALL_COSTS[model]
    days = list(px.index)
    p = px["p1555"].to_numpy(float)
    c = px["close"].to_numpy(float)
    rsi = RSI(2)
    daily = {d: 0.0 for d in days}
    trades, pos = [], None
    for i, d in enumerate(days):
        P = p[i]
        if pos is not None:
            daily[d] += (P - p[i - 1]) / pos["px"] * 1e4
            pos["sessions"] += 1
            sma5 = (c[i - 4:i].sum() + P) / 5 if i >= 4 else np.inf
            if P > sma5 or (time_stop is not None and pos["sessions"] >= time_stop):
                daily[d] -= sell
                pos["bp"] += sum(daily[x] for x in days[pos["i"] + 1:i + 1])
                trades.append({"entry": pos["day"], "exit": d, "bp": pos["bp"], "sessions": pos["sessions"]})
                pos = None
        elif i >= 199 and not np.isnan(P):
            r = rsi.preview(P)
            sma200 = (c[i - 199:i].sum() + P) / 200
            if r is not None and r < rsi_max and P > sma200:
                daily[d] -= buy
                pos = {"day": d, "i": i, "px": P, "sessions": 0, "bp": -buy}
        rsi.update(c[i])
    if pos is not None:                              # still open at the end of the data: marked, not closed
        pos["bp"] += sum(daily[x] for x in days[pos["i"] + 1:])
        trades.append({"entry": pos["day"], "exit": None, "bp": pos["bp"], "sessions": pos["sessions"], "open": True})
    return pd.Series(daily), trades


# ---------------------------------------------------------------- stats

def stats(x: pd.Series, trades: list[float]) -> dict:
    x = x.astype(float)
    n = len(x)
    mu = float(x.mean()) if n else 0.0
    sd = float(x.std(ddof=1)) if n > 1 else 0.0
    tr = np.asarray(trades, float)
    gains, losses = tr[tr > 0].sum(), -tr[tr < 0].sum()
    cum = x.cumsum()
    return {
        "sessions": n, "mean": mu, "sd": sd, "t": mu / (sd / math.sqrt(n)) if sd > 0 else 0.0,
        "trades": int(len(tr)), "win": float((tr > 0).mean()) if len(tr) else 0.0,
        "bp_per_trade": float(tr.mean()) if len(tr) else 0.0,
        "pf": float(gains / losses) if losses > 0 else float("inf") if gains > 0 else 0.0,
        "worst": float(tr.min()) if len(tr) else 0.0,
        "ann_pct": mu * 252 / 100, "sharpe": mu / sd * math.sqrt(252) if sd > 0 else 0.0,
        "max_dd_bp": float((cum - cum.cummax()).min()) if n else 0.0,
    }


def window(x: pd.Series, lo: date, hi: date) -> pd.Series:
    return x[(x.index >= lo) & (x.index <= hi)]


def stage1_pass(r: dict) -> bool:
    return r["is"]["mid1"]["mean"] > 0 and r["oos"]["mid1"]["t"] >= T_OOS and r["oos"]["taker"]["mean"] > 0


def holdout_pass(r: dict) -> bool:
    return r["mid1"]["t"] >= T_HOLDOUT and r["taker"]["mean"] > 0


# ---------------------------------------------------------------- runs

def candidate_series(px: pd.DataFrame, cal: Calendar, model: str, name: str, **kw):
    """(daily Series, {entry day: trade bp}) for a candidate or sensitivity row."""
    if name.startswith("H3"):
        daily, trades = rsi2_dip(px, model, **kw)
        return daily, {t["entry"]: t["bp"] for t in trades}
    cond = {"H1": None, "H2": down_day}[name[:2]]
    cond = kw.pop("cond", cond)
    daily = overnight(px, cal, model, cond=cond, **kw)
    return daily, {d: v for d, v in daily.items() if v != 0.0}


CANDIDATES = {"H1": {}, "H2": {}, "H3": {}}
SENSITIVITIES = {
    "H1 exit 09:31": ("H1", {"exit_col": "p0931"}),
    "H1 exit 10:00": ("H1", {"exit_col": "p1000"}),
    "H1 weekday nights only": ("H1", {"weekday_only": True}),
    "H1 + 0.6bp dividends": ("H1", {"div_bp": 0.6}),
    "H2 down >= 0.5%": ("H2", {"cond": down_half_pct}),
    "H2 last hour down": ("H2", {"cond": down_last_hour}),
    "H3 RSI(2) < 5": ("H3", {"rsi_max": 5.0}),
    "H3 10-session time stop": ("H3", {"time_stop": 10}),
}
REFERENCE = ("buy and hold 15:55-15:55", "intraday 09:35-15:55")


def reference(px: pd.DataFrame, cal: Calendar) -> dict[str, pd.Series]:
    """Buy and hold over consecutive trading sessions only (a gap in the data would span several days)."""
    days = list(px.index)
    p = px["p1555"]
    bh_ = {d: (p[d] / p[days[i - 1]] - 1) * 1e4 for i, d in enumerate(days)
           if i and cal.prev_session(d) == days[i - 1]}
    intra = (px["p1555"] / px["p0935"] - 1).dropna() * 1e4
    return {REFERENCE[0]: pd.Series(bh_), REFERENCE[1]: intra}


def load_oanda_raw(folder: Path, min_minutes: int) -> pd.DataFrame:
    """Coverage check only: sessions with >= min_minutes RTH bars and a bar in 09:30-09:34 and in 15:45-15:54."""
    fs = sorted(Path(folder).glob("oanda-SPX500_USD-*.csv"))
    df = pd.concat([pd.read_csv(f) for f in fs], ignore_index=True)
    t = pd.to_datetime(df["time"], utc=True)
    df = df.assign(t=t).drop_duplicates("t").sort_values("t")
    et = df.t.dt.tz_convert("America/New_York")
    df = df.assign(day=et.dt.date, hm=et.dt.hour * 60 + et.dt.minute, wd=et.dt.weekday)
    df = df[(df.hm >= OPEN) & (df.hm < CLOSE) & (df.wd < 5)]
    g = df.groupby("day")
    ok = (g.size() >= min_minutes) & g.hm.apply(lambda h: ((h >= 570) & (h < 575)).any() and ((h >= 945) & (h < 955)).any())
    half = nyse_calendar.half_days_between(2005, 2020)
    keep = [d for d in ok[ok].index if d not in half]
    return session_prices_1m(df[df.day.isin(keep)][["day", "hm", "close"]])


def summarize(daily: pd.Series, trades: dict, lo: date, hi: date) -> dict:
    w = window(daily, lo, hi)
    tr = [v for d, v in trades.items() if lo <= d <= hi]
    return stats(w, tr)


def by_year(daily: pd.Series) -> dict:
    out = {}
    for y in sorted({d.year for d in daily.index}):
        w = daily[[d.year == y for d in daily.index]]
        out[y] = {"mean": float(w.mean()), "t": stats(w, [])["t"], "total_bp": float(w.sum())}
    return out


def run_stage1(oanda: Path, spy5: Path | None, px: pd.DataFrame | None = None) -> dict:
    px = load_oanda(oanda) if px is None else px
    cal = Calendar(px.index[0].year, px.index[-1].year)
    rec = load_spy_5m(spy5) if spy5 and Path(spy5).exists() else None
    cal_r = Calendar(2025, 2026)
    res = {"meta": {"sessions": len(px), "first": str(px.index[0]), "last": str(px.index[-1]),
                    "recent_sessions": 0 if rec is None else len(rec)}, "candidates": {}, "sensitivities": {},
           "reference": {}}
    for name in CANDIDATES:
        r = {"is": {}, "oos": {}, "recent": {}, "by_year_mid1": {}}
        for model in ALL_COSTS:
            daily, trades = candidate_series(px, cal, model, name)
            for per, (lo, hi) in PERIODS.items():
                r[per][model] = summarize(daily, trades, lo, hi)
            if model == "mid1":
                r["by_year_mid1"] = by_year(daily)
            if rec is not None:
                d2, t2 = candidate_series(rec, cal_r, model, name)
                r["recent"][model] = summarize(d2, t2, date(2025, 1, 1), date(2026, 12, 31))
        r["stage1_pass"] = stage1_pass(r)
        res["candidates"][name] = r
    for label, (base, kw) in SENSITIVITIES.items():
        r = {}
        for model in COSTS:
            daily, trades = candidate_series(px, cal, model, base, **dict(kw))
            r[model] = {per: summarize(daily, trades, lo, hi) for per, (lo, hi) in PERIODS.items()}
        res["sensitivities"][label] = r
    for label, s in reference(px, cal).items():
        res["reference"][label] = {per: stats(window(s, lo, hi), []) for per, (lo, hi) in PERIODS.items()}
    return res


def run_holdout(spy1m: Path, prior: dict | None) -> dict:
    px = load_spy_1m(spy1m)
    cal = Calendar(px.index[0].year, px.index[-1].year)
    lo, hi = HOLDOUT
    out = {"meta": {"sessions": int(((px.index >= lo) & (px.index <= hi)).sum()), "first": str(px.index[0]),
                    "last": str(px.index[-1])}, "candidates": {}}
    for name in CANDIDATES:
        r = {}
        for model in ALL_COSTS:
            daily, trades = candidate_series(px, cal, model, name)
            r[model] = summarize(daily, trades, lo, hi)
            if model == "mid1":
                r["by_year_mid1"] = by_year(window(daily, lo, hi))
        s1 = None if prior is None else prior["candidates"][name]["stage1_pass"]
        r["stage1_pass"] = s1
        r["holdout_pass"] = holdout_pass(r)
        r["judged"] = bool(s1)
        out["candidates"][name] = r
    out["reference"] = {k: stats(window(s, lo, hi), []) for k, s in reference(px, cal).items()}
    return out


# ---------------------------------------------------------------- report

def _row(s: dict) -> str:
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"{s['mean']:+.2f} | {s['t']:+.2f} | {s['trades']} | {s['win'] * 100:.0f}% | {s['bp_per_trade']:+.1f} | "
            f"{pf} | {s['ann_pct']:+.1f}% | {s['sharpe']:.2f} | {s['worst']:+.0f} | {s['max_dd_bp']:+.0f}")


HDR = ("| Row | Fills | mean bp/session | t | trades | win | bp/trade | PF | ann. on notional | Sharpe | worst trade bp "
       "| max DD bp |\n|---|---|---|---|---|---|---|---|---|---|---|---|")


def report_stage1(res: dict) -> str:
    m = res["meta"]
    L = ["# Book H candidates H1-H3: stage 1 results", "",
         f"Rules frozen in `research/book_h_candidates_prereg.md` (commit 300b931) before this ran. S&P 500 CFD 1-minute, "
         f"{m['first']}..{m['last']} ({m['sessions']} full sessions); recent check SPY 5-minute "
         f"({m['recent_sessions']} full sessions, information only). Returns in bp of a $10,000 notional per session "
         "(0 when flat); t over sessions.", "",
         "## Stage 1 bar (in-sample mean > 0 at mid1, out-of-sample t >= 2.33 at mid1, out-of-sample mean > 0 at taker)",
         "", "| Candidate | IS mean mid1 | OOS t mid1 | OOS mean taker | Stage 1 |", "|---|---|---|---|---|"]
    for k, r in res["candidates"].items():
        L.append(f"| {k} | {r['is']['mid1']['mean']:+.2f} | {r['oos']['mid1']['t']:+.2f} | "
                 f"{r['oos']['taker']['mean']:+.2f} | **{'PASS' if r['stage1_pass'] else 'fail'}** |")
    for per, title in (("is", "In-sample 2005-2014"), ("oos", "Out-of-sample 2015-01..2020-05"),
                       ("recent", "Recent SPY 2025-04..2026-03 (inside the Mac holdout; information only)")):
        L += ["", f"## {title}", "", HDR]
        for k, r in res["candidates"].items():
            for model in ALL_COSTS:
                if model in r[per]:
                    L.append(f"| {k} | {model} | " + _row(r[per][model]) + " |")
        if per != "recent":
            for k, r in res["reference"].items():
                L.append(f"| {k} | none | " + _row(r[per]) + " |")
    L += ["", "## Sensitivities (reported, never selected from)", "", HDR.replace("| Row | Fills", "| Row (period) | Fills")]
    for k, r in res["sensitivities"].items():
        for model, pers in r.items():
            for per, s in pers.items():
                L.append(f"| {k} ({per}) | {model} | " + _row(s) + " |")
    L += ["", "## By year at mid1 (mean bp per session, t)", "",
          "| Year | " + " | ".join(res["candidates"]) + " |", "|---|" + "---|" * len(res["candidates"])]
    years = sorted({y for r in res["candidates"].values() for y in r["by_year_mid1"]})
    for y in years:
        cells = []
        for r in res["candidates"].values():
            v = r["by_year_mid1"].get(y) or r["by_year_mid1"].get(str(y))
            cells.append(f"{v['mean']:+.2f} ({v['t']:+.1f})" if v else "")
        L.append(f"| {y} | " + " | ".join(cells) + " |")
    return "\n".join(L) + "\n"


def report_holdout(res: dict) -> str:
    m = res["meta"]
    L = ["# Book H candidates H1-H3: Mac holdout (SPY SIP 1-minute)", "",
         f"Judged window {HOLDOUT[0]}..{HOLDOUT[1]} ({m['sessions']} full sessions; bars {m['first']}..{m['last']}). "
         "Bar: t >= 1.65 at mid1 and mean > 0 at taker, judged only for stage-1 survivors.", "",
         "| Candidate | stage 1 | holdout t mid1 | holdout mean taker | holdout bar | judged |", "|---|---|---|---|---|---|"]
    for k, r in res["candidates"].items():
        L.append(f"| {k} | {r['stage1_pass']} | {r['mid1']['t']:+.2f} | {r['taker']['mean']:+.2f} | "
                 f"{'PASS' if r['holdout_pass'] else 'fail'} | {'yes' if r['judged'] else 'no (failed stage 1)'} |")
    L += ["", HDR]
    for k, r in res["candidates"].items():
        for model in ALL_COSTS:
            L.append(f"| {k} | {model} | " + _row(r[model]) + " |")
    for k, s in res["reference"].items():
        L.append(f"| {k} | none | " + _row(s) + " |")
    L += ["", "## By year at mid1", "", "| Year | " + " | ".join(res["candidates"]) + " |",
          "|---|" + "---|" * len(res["candidates"])]
    years = sorted({y for r in res["candidates"].values() for y in r["by_year_mid1"]})
    for y in years:
        L.append(f"| {y} | " + " | ".join(f"{r['by_year_mid1'][y]['mean']:+.2f} ({r['by_year_mid1'][y]['t']:+.1f})"
                                          for r in res["candidates"].values()) + " |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--oanda", type=Path, default=HERE / "data" / "spx_rth_1m.pkl")
    ap.add_argument("--spy-5m", type=Path, default=HERE / "data" / "spy_5m_2025_2026.csv")
    ap.add_argument("--spy-1m", type=Path, default=None, help="SIP 1-minute folder (Mac holdout)")
    ap.add_argument("--holdout", action="store_true")
    ap.add_argument("--out", type=Path, default=HERE)
    ap.add_argument("--coverage-min-minutes", type=int, default=None,
                    help="coverage check (not judged): rebuild sessions from the raw Oanda CSVs with this minimum")
    a = ap.parse_args()
    if a.coverage_min_minutes:
        px = load_oanda_raw(HERE / "data" / "oanda", a.coverage_min_minutes)
        res = run_stage1(a.oanda, None, px=px)
        txt = report_stage1(res).replace("stage 1 results", f"coverage check, sessions with >= "
                                         f"{a.coverage_min_minutes} minutes (NOT judged)")
        (a.out / "book_h_coverage_check.md").write_text(txt)
        print(txt)
        return 0
    if a.holdout:
        prior_f = HERE / "book_h_candidates_results.json"
        prior = json.loads(prior_f.read_text()) if prior_f.exists() else None
        res = run_holdout(a.spy_1m, prior)
        (a.out / "book_h_holdout_results.json").write_text(json.dumps(res, indent=1, default=str))
        (a.out / "book_h_holdout.md").write_text(report_holdout(res))
        print(report_holdout(res))
        return 0
    res = run_stage1(a.oanda, a.spy_5m)
    (a.out / "book_h_candidates_results.json").write_text(json.dumps(res, indent=1, default=str))
    (a.out / "book_h_candidates.md").write_text(report_stage1(res))
    print(report_stage1(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
