"""V1: short VIX futures carry (SVXY, -0.5x) while VIX/VIX3M < 1. Rules frozen in research/vix_carry_prereg.md
(commit 862de63) before any of this data was downloaded.

Mac, from the repo root (CBOE's site isn't reachable from the cloud sessions):

    .venv/bin/python research/fetch_cboe_vx.py --smoke      # one contract + VIX3M, printed; writes nothing
    .venv/bin/python research/fetch_cboe_vx.py --svxy       # data/cboe: VX monthly futures, VIX, VIX3M, SVXY closes
    .venv/bin/python research/vix_carry.py --data data/cboe --out research
                                                            # research/vix_carry.md + vix_carry_results.json

The index is the S&P 500 VIX Short-Term Futures index (excess return) rebuilt from VX settlements with the S&P
roll (prereg section 3). Returns are per dollar of notional; flat sessions count as 0.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nyse_calendar  # noqa: E402

LEV = -0.5
FEE_YR = 0.0095
THRESH = 1.00
COSTS = {"mid1": 3.0, "taker": 10.0, "zero": 0.0}       # bp per position change, one side
SALE_BP = 0.3                                            # on exits, except the zero-cost row
IS = (date(2008, 1, 2), date(2014, 12, 31))
OOS = (date(2015, 1, 2), date(2026, 9, 30))
T_BAR = 2.33
NW_LAGS = 5
MAX_FILL = 2
VALID_CORR = 0.95
VALID_FROM = date(2018, 3, 1)
VALID_MIN = 250
OLD_X10_BEFORE = date(2007, 3, 26)                      # VX was quoted on 10x VIX before this date


# ---------------------------------------------------------------- calendar

def nyse_session(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_calendar.holidays(d.year)


def _prev(d: date, is_session) -> date:
    d -= timedelta(days=1)
    while not is_session(d):
        d -= timedelta(days=1)
    return d


def vx_settlement(y: int, m: int, is_session=nyse_session) -> date:
    """VX monthly final settlement: 30 days before the third Friday of the next month, moved back to the session
    before that Friday if it's a holiday, then back to a session if the Wednesday itself is closed."""
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    f = nyse_calendar._nth_weekday(ny, nm, 4, 3)
    if not is_session(f):
        f = _prev(f, is_session)
    s = f - timedelta(days=30)
    return s if is_session(s) else _prev(s, is_session)


def session_test(sessions: list[date]):
    """Inside the data's range a session is a date with VX settlements; outside it, an NYSE weekday."""
    known, lo, hi = set(sessions), min(sessions), max(sessions)
    return lambda d: (d in known) if lo <= d <= hi else nyse_session(d)


# ---------------------------------------------------------------- CSV readers

def _header_skip(path: Path, needle: str) -> int:
    with open(path, errors="replace") as fh:
        for i, line in enumerate(fh):
            if needle in line.lower():
                return i
    raise ValueError(f"{path}: no header containing {needle!r}")


def _dates(col: pd.Series) -> pd.Series:
    return pd.to_datetime(col.astype(str).str.strip(), format="mixed").dt.date


def read_cboe_csv(path: Path) -> pd.Series:
    """One VX contract's daily settlements {trade date: settle}; zero or missing settles dropped."""
    df = pd.read_csv(path, skiprows=_header_skip(path, "trade date"))
    df.columns = [c.strip().lower() for c in df.columns]
    s = pd.Series(pd.to_numeric(df["settle"], errors="coerce").values, index=_dates(df["trade date"]))
    s = s[s > 0]
    s[s.index < OLD_X10_BEFORE] /= 10.0
    return s[~s.index.duplicated(keep="last")].sort_index()


def read_index_csv(path: Path) -> pd.Series:
    """CBOE index history (VIX_History.csv, VIX3M_History.csv): {date: close}."""
    df = pd.read_csv(path, skiprows=_header_skip(path, "date"))
    df.columns = [c.strip().lower() for c in df.columns]
    close = next(c for c in df.columns if "close" in c)
    s = pd.Series(pd.to_numeric(df[close], errors="coerce").values, index=_dates(df[df.columns[0]]))
    return s.dropna()[lambda x: x > 0].sort_index()


def load_vx(folder: Path) -> tuple[list[date], dict[date, pd.Series]]:
    """Files VX_YYYY-MM.csv -> (sessions, {settlement date: settles}). Settlement dates use the data's sessions."""
    raw = {}
    for p in sorted(folder.glob("VX_*.csv")):
        y, m = (int(x) for x in p.stem.split("_")[1].split("-"))
        s = read_cboe_csv(p)
        if len(s):
            raw[(y, m)] = s
    sessions = sorted(set().union(*(set(s.index) for s in raw.values())))
    is_session = session_test(sessions)
    return sessions, {vx_settlement(y, m, is_session): s for (y, m), s in raw.items()}


# ---------------------------------------------------------------- roll schedule and index

def roll_schedule(sessions: list[date], settles: list[date]) -> pd.DataFrame:
    """For each session t in roll period [S_k, S_k+1): first month S_k+1 with weight dr/dt, second month S_k+2."""
    settles, sessions = sorted(settles), sorted(sessions)
    rows = {}
    for t in sessions:
        k = bisect.bisect_right(settles, t) - 1
        if k < 0 or k + 2 >= len(settles):
            continue
        lo, hi = settles[k], settles[k + 1]
        if lo < sessions[0]:
            continue                                     # roll period starts before the data: dt unknown
        end = bisect.bisect_left(sessions, hi)
        dt = end - bisect.bisect_left(sessions, lo)
        dr = end - bisect.bisect_right(sessions, t)
        rows[t] = {"front": hi, "second": settles[k + 2], "w1": dr / dt}
    return pd.DataFrame.from_dict(rows, orient="index")


def _filled(s: pd.Series, sessions: list[date]) -> pd.Series:
    if s.empty:
        return s
    life = [d for d in sessions if s.index[0] <= d <= s.index[-1]]
    return s.reindex(life).ffill(limit=MAX_FILL)


def index_returns(sessions: list[date], settles: list[date], prices: dict[date, pd.Series]):
    """Daily SPVXSP-style excess return R(t) from the weights and contracts set at the close of t-1."""
    sch = roll_schedule(sessions, settles)
    px = {k: _filled(v, sessions) for k, v in prices.items()}
    out, missing = {}, 0

    def price(c, d):
        s = px.get(c)
        v = s.get(d, np.nan) if s is not None and len(s) else np.nan
        return float(v)

    for prev, t in zip(sessions, sessions[1:]):
        if prev not in sch.index:
            continue
        row = sch.loc[prev]
        legs = [(row.front, row.w1), (row.second, 1.0 - row.w1)]
        num = den = 0.0
        bad = False
        for c, w in legs:
            if w == 0:
                continue
            a, b = price(c, prev), price(c, t)
            if math.isnan(a) or math.isnan(b):
                bad = True
                break
            num += w * b
            den += w * a
        if bad or den <= 0:
            out[t] = np.nan
            missing += 1
        else:
            out[t] = num / den - 1.0
    return pd.Series(out, dtype=float), {"sessions": len(out), "missing": missing,
                                         "first": min(out) if out else None, "last": max(out) if out else None}


def front_prices(sessions: list[date], settles: list[date], prices: dict[date, pd.Series]) -> pd.Series:
    sch = roll_schedule(sessions, settles)
    px = {k: _filled(v, sessions) for k, v in prices.items()}
    return pd.Series({t: float(px[r.front].get(t, np.nan)) if r.front in px else np.nan for t, r in sch.iterrows()})


# ---------------------------------------------------------------- positions and returns

def positions(c: pd.Series, sessions: list[date], thresh: float = THRESH, lag: int = 1) -> pd.Series:
    """p(t) = 1 if c(t-lag) < thresh; a missing c keeps the previous position; flat before the first signal."""
    c = c.reindex(sessions)
    sig = pd.Series(np.where(c.isna(), np.nan, (c < thresh).astype(float)), index=c.index)
    return sig.shift(lag).ffill().fillna(0.0)


def strategy(R: pd.Series, p: pd.Series, model: str, lev: float = LEV, fee_yr: float = FEE_YR) -> pd.Series:
    """s(t) = p(t-1) x (lev x R(t) - fee) - cost x |p(t-1) - p(t-2)| - sale fee on exits. A missing R(t) earns 0."""
    p = p.reindex(R.index).fillna(0.0)
    held = p.shift(1).fillna(0.0)
    ret = (lev * R - fee_yr / 252).where(R.notna(), 0.0)
    s = held * ret
    prev = p.shift(1).fillna(0.0)
    change = (p - prev).abs().shift(1).fillna(0.0)
    exits = ((prev == 1) & (p == 0)).astype(float).shift(1).fillna(0.0)
    cost = COSTS[model] / 1e4
    sale = 0.0 if model == "zero" else SALE_BP / 1e4
    return s - cost * change - sale * exits


# ---------------------------------------------------------------- statistics

def nw_t(x: np.ndarray, lags: int = NW_LAGS) -> float:
    n = len(x)
    e = x - x.mean()
    v = e @ e / n
    for lag in range(1, lags + 1):
        v += 2 * (1 - lag / (lags + 1)) * (e[lag:] @ e[:-lag]) / n
    return float(x.mean() / math.sqrt(v / n)) if v > 0 else float("nan")


def stats(x: pd.Series, p: pd.Series | None = None) -> dict:
    a = x.dropna().to_numpy(dtype=float)
    n = len(a)
    if n < 2:
        return {"n": n}
    sd = a.std(ddof=1)
    t_iid = float(a.mean() / sd * math.sqrt(n)) if sd > 0 else float("nan")
    t_nw = nw_t(a)
    eq = np.cumprod(1 + a)
    dd = eq / np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:] - 1
    out = {"n": n, "mean": float(a.mean()), "mean_bp": float(a.mean() * 1e4), "t_iid": t_iid, "t_nw": t_nw,
           "t": float(min(t_iid, t_nw)), "ann_ret": float(a.mean() * 252), "ann_vol": float(sd * math.sqrt(252)),
           "sharpe": float(a.mean() / sd * math.sqrt(252)) if sd > 0 else float("nan"),
           "max_dd": float(dd.min()), "worst": float(a.min()), "total": float(eq[-1] - 1)}
    if p is not None:
        q = p.reindex(x.index).fillna(0.0)
        out["held"] = float(q.mean())
        out["switches_per_year"] = float(q.diff().abs().sum() / (n / 252))
    return out


def window(x: pd.Series, lo: date, hi: date) -> pd.Series:
    return x[(x.index >= lo) & (x.index <= hi)]


def passes(r: dict) -> bool:
    return r["is"]["mid1"]["mean"] > 0 and r["oos"]["mid1"]["t"] >= T_BAR and r["oos"]["taker"]["mean"] > 0


def by_year(x: pd.Series) -> dict:
    out = {}
    for y in sorted({d.year for d in x.index}):
        s = x[[d.year == y for d in x.index]]
        st = stats(s)
        if st["n"] >= 2:
            out[y] = {k: st[k] for k in ("n", "mean_bp", "t", "total", "worst", "max_dd")}
    return out


def validate(R: pd.Series, svxy: pd.Series | None) -> dict:
    """Rebuilt -0.5 x index less the fee vs SVXY's actual daily return from 2018-03-01."""
    if svxy is None or len(svxy) < 2:
        return {"valid": None, "why": "no SVXY closes"}
    actual = svxy.sort_index().pct_change()
    model = LEV * R - FEE_YR / 252
    j = pd.concat({"model": model, "actual": actual}, axis=1).dropna()
    j = j[j.index >= VALID_FROM]
    if len(j) < VALID_MIN:
        return {"valid": None, "why": f"only {len(j)} common sessions", "n": len(j)}
    corr = float(j["model"].corr(j["actual"]))
    gap = (j["model"] - j["actual"])
    yearly = {int(y): float(g.mean() * 252) for y, g in gap.groupby([d.year for d in gap.index])}
    return {"valid": corr >= VALID_CORR, "corr": corr, "n": len(j),
            "gap_ann": float(gap.mean() * 252), "gap_by_year": yearly}


# ---------------------------------------------------------------- run

def series_for(R, c, sessions, model, thresh=THRESH, lag=1, lev=LEV, always=False, sig_override=None):
    if always:
        p = pd.Series(1.0, index=sessions)
    elif sig_override is not None:
        p = sig_override
    else:
        p = positions(c, sessions, thresh, lag)
    return strategy(R, p, model, lev=lev), p


def summarize(R, c, sessions, lo_is, **kw) -> dict:
    out = {"is": {}, "oos": {}}
    for model in ("mid1", "taker"):
        s, p = series_for(R, c, sessions, model, **kw)
        out["is"][model] = stats(window(s, lo_is, IS[1]), p)
        out["oos"][model] = stats(window(s, *OOS), p)
    return out


def run(data: Path, out: Path) -> dict:
    sessions, prices = load_vx(data / "vx")
    settles = sorted(prices)
    R, info = index_returns(sessions, settles, prices)
    vix = read_index_csv(data / "VIX_History.csv")
    vix3m = read_index_csv(data / "VIX3M_History.csv")
    c = (vix / vix3m).dropna()
    rs = list(R.index)
    both = [d for d in rs if d in c.index]
    lo_is = max(IS[0], both[0] if both else rs[0])
    svxy_path = data / "svxy_daily.csv"
    svxy = pd.read_csv(svxy_path, parse_dates=["date"]).assign(date=lambda d: d.date.dt.date).set_index("date")[
        "close"] if svxy_path.exists() else None

    res = {"rules": "research/vix_carry_prereg.md", "index": info, "is_start": lo_is.isoformat(),
           "contracts": len(settles), "V1": summarize(R, c, rs, lo_is)}
    res["V1"]["pass"] = passes(res["V1"])
    s_main, p_main = series_for(R, c, rs, "mid1")
    res["by_year_mid1"] = by_year(window(s_main, lo_is, OOS[1]))
    worst = window(s_main, lo_is, OOS[1]).nsmallest(10)
    res["worst_days_mid1"] = [{"date": d.isoformat(), "ret": float(v), "vix_prev": float(vix.get(d, np.nan))}
                              for d, v in worst.items()]
    front = front_prices(sessions, settles, prices)
    basis_sig = pd.Series(np.where((front.reindex(rs).isna()) | vix.reindex(rs).isna(), np.nan,
                                   (front.reindex(rs) > vix.reindex(rs)).astype(float)), index=rs)
    basis_p = basis_sig.shift(1).ffill().fillna(0.0)
    sens = {
        "threshold 0.95": summarize(R, c, rs, lo_is, thresh=0.95),
        "threshold 0.90": summarize(R, c, rs, lo_is, thresh=0.90),
        "no filter (always short)": summarize(R, c, rs, lo_is, always=True),
        "-1x leverage": summarize(R, c, rs, lo_is, lev=-1.0),
        "no lag (look-ahead upper bound)": summarize(R, c, rs, lo_is, lag=0),
        "front-month basis filter": summarize(R, c, rs, lo_is, sig_override=basis_p),
    }
    zero = {}
    for name, w in (("is", (lo_is, IS[1])), ("oos", OOS)):
        s, p = series_for(R, c, rs, "zero")
        zero[name] = {"zero": stats(window(s, *w), p)}
    sens["zero costs"] = zero
    res["sensitivities"] = sens
    res["validation"] = validate(R, svxy)
    out.mkdir(parents=True, exist_ok=True)
    (out / "vix_carry_results.json").write_text(json.dumps(res, indent=1, default=str))
    (out / "vix_carry.md").write_text(report(res))
    return res


# ---------------------------------------------------------------- report

COLS = "| Row | Fills | sessions | mean bp/day | t (min of iid, NW) | ann. return | ann. vol | Sharpe | max DD | " \
       "worst day | held | switches/yr |\n|---|---|---|---|---|---|---|---|---|---|---|---|"


def _row(name: str, model: str, s: dict) -> str:
    if s.get("n", 0) < 2:
        return f"| {name} | {model} | {s.get('n', 0)} | | | | | | | | | |"
    return (f"| {name} | {model} | {s['n']} | {s['mean_bp']:+.2f} | {s['t']:+.2f} | {s['ann_ret']:+.1%} | "
            f"{s['ann_vol']:.1%} | {s['sharpe']:+.2f} | {s['max_dd']:.1%} | {s['worst']:+.1%} | "
            f"{s.get('held', float('nan')):.0%} | {s.get('switches_per_year', float('nan')):.1f} |")


def report(res: dict) -> str:
    v = res["V1"]
    L = ["# V1 short VIX futures carry (SVXY, -0.5x) while VIX/VIX3M < 1: replay", "",
         "Rules frozen in `research/vix_carry_prereg.md` (commit 862de63) before any data was downloaded.",
         "Bar: in-sample mean > 0 at mid1; out-of-sample (2015-01-02 to 2026-09-30) t >= 2.33 at mid1 (smaller of "
         "plain and Newey-West(5) t); out-of-sample mean > 0 at taker.", "",
         f"**Result: {'PASS' if v['pass'] else 'FAIL'}**", "",
         f"In-sample starts {res['is_start']}. Index sessions {res['index']['sessions']}, missing "
         f"{res['index']['missing']}, contracts {res['contracts']}.", ""]
    val = res["validation"]
    if val.get("valid") is None:
        L.append(f"SVXY validation: not run ({val.get('why')}).")
    else:
        L.append(f"SVXY validation (2018-03-01 on, {val['n']} sessions): correlation {val['corr']:.3f} "
                 f"({'valid' if val['valid'] else 'INVALID: below 0.95, fix the index or data and rerun'}); "
                 f"modeled minus actual {val['gap_ann']:+.1%} a year.")
    L += ["", COLS]
    for per in ("is", "oos"):
        for model in ("mid1", "taker"):
            L.append(_row(f"V1 {per}", model, v[per][model]))
    L += ["", "## Sensitivities (reported, never selected from)", "", COLS]
    for name, r in res["sensitivities"].items():
        for per in ("is", "oos"):
            for model, s in r[per].items():
                L.append(_row(f"{name} {per}", model, s))
    L += ["", "## By year (mid1)", "", "| Year | sessions | mean bp/day | t | total | worst day | max DD |",
          "|---|---|---|---|---|---|---|"]
    for y, s in res["by_year_mid1"].items():
        L.append(f"| {y} | {s['n']} | {s['mean_bp']:+.2f} | {s['t']:+.2f} | {s['total']:+.1%} | {s['worst']:+.1%} | "
                 f"{s['max_dd']:.1%} |")
    L += ["", "## Ten worst days (mid1)", "", "| Date | return |", "|---|---|"]
    L += [f"| {w['date']} | {w['ret']:+.1%} |" for w in res["worst_days_mid1"]]
    if val.get("gap_by_year"):
        L += ["", "## Modeled minus actual SVXY, by year (annualized)", "", "| Year | gap |", "|---|---|"]
        L += [f"| {y} | {g:+.1%} |" for y, g in val["gap_by_year"].items()]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=Path("data/cboe"))
    ap.add_argument("--out", type=Path, default=Path("research"))
    a = ap.parse_args()
    res = run(a.data, a.out)
    v = res["V1"]
    print(f"V1: {'PASS' if v['pass'] else 'FAIL'}  IS mid1 mean {v['is']['mid1'].get('mean_bp', float('nan')):+.2f} bp"
          f"  OOS mid1 t {v['oos']['mid1'].get('t', float('nan')):+.2f}"
          f"  OOS taker mean {v['oos']['taker'].get('mean_bp', float('nan')):+.2f} bp"
          f"  validation {res['validation'].get('corr', 'n/a')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
