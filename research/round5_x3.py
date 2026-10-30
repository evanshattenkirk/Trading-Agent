"""X3 (research/round5_prereg.md section 4): does book F2-P's premise, that a stock closing up >= 3% on >= 2x its
20-day volume falls the next day, hold on US stocks the S&P daily study never saw? Shares, daily bars, research only.

    .venv/bin/python research/round5_x3.py                 # reads research/data/round5/x3/, writes x3_results.json

Inputs (gitignored, in research/data/round5/x3/ unless flags say otherwise):
- DoltHub `post-no-preference/stocks` (free CSV exports, downloaded 2026-10-30): `ohlcv_2015.parquet` (the `ohlcv`
  table from 2015-06-01, unadjusted), `symbol.csv`, `split.csv`, `dividend.csv`.
- `constituents.csv`: today's S&P 500 list (datasets/s-and-p-500-companies).
- `names505.txt`: the daily study's 505 symbols, one per line (the file names in CNuge/kaggle-code
  `stock_data/individual_stocks_5yr.zip`, the data `research/strategy_f_daily.py` ran on; its results JSON keeps only
  the count).

Rules, all frozen in the pre-registration: universe on day t = common, non-test, non-ETF symbols minus the four lists,
with prior close >= $10, 14-day average volume >= 1M, ATR14 >= $0.50 (Wilder, as `strategy_f_intraday._features`) and
20-day average dollar volume >= $25M, all through t-1, and no split with ex-date in [t-20, t+1] sessions. Signal:
close_t >= 1.03 x close_(t-1) and volume_t >= 2 x the mean volume of t-20..t-1. Short at close_t, cover at the next
session's close (a name with no bar that session can't be valued and is counted, not traded); the short pays a
dividend that goes ex that session. Excess = short return - 2 bp a side - the equal-weight universe's short return
over the same night. t is over signal-day means.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "research" / "data" / "round5" / "x3"

NAME_RE = re.compile(r"\b(warrants?|rights?|units?|preferred|depositary|notes?)\b", re.I)
F2_NAMES = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "JPM", "V", "MA", "UNH", "XOM", "LLY",
            "COST", "NFLX", "AMD", "ORCL", "CRM", "ADBE", "BAC", "WMT", "HD", "DIS", "INTC", "QCOM", "MU", "PLTR",
            "SMCI", "ARM", "MRVL", "COIN", "UBER", "SHOP"]
E_NAMES = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "JPM", "V", "MA", "UNH", "XOM", "LLY",
           "COST", "NFLX", "AMD", "ORCL", "CRM", "ADBE", "BAC", "WMT", "HD", "PG", "JNJ", "KO", "PEP", "DIS", "INTC",
           "QCOM"]
FLOORS = {"min_close": 10.0, "min_avg_vol14": 1_000_000, "min_atr": 0.50, "min_dv20": 25_000_000}
PERIOD = (date(2016, 1, 1), date(2026, 10, 29))
HALVES = {"2016-2020": (date(2016, 1, 1), date(2020, 12, 31)), "2021-2026": (date(2021, 1, 1), date(2026, 10, 29))}
UP_MIN, RVOL_MIN, CONTROL_MAX, F2P_RVOL = 0.03, 2.0, 1.5, 1.8
SIDE_BP, STRESS_BP = 2.0, 10.0
T_BAR, T_SINGLE, PF_BAR = 2.58, 2.33, 1.1
LISTED_SINCE = date(2026, 1, 1)          # last_seen before this = no longer listed


# ---------------------------------------------------------------- universe membership

def bad_security_name(name) -> bool:
    return bool(NAME_RE.search(name if isinstance(name, str) else ""))


def norm(sym) -> str:
    return re.sub(r"[-/ ]", ".", str(sym).strip().upper())


def eligible_symbols(sym: pd.DataFrame) -> set[str]:
    ok = ((sym["is_etf"].astype(str).str.strip() == "0") & (sym["is_test_issue"].astype(str).str.strip() == "0")
          & ~sym["security_name"].map(bad_security_name))
    return set(sym.loc[ok, "act_symbol"].map(norm))


def excluded_symbols(*lists) -> set[str]:
    return {norm(s) for names in lists for s in names}


def calendar(df: pd.DataFrame, min_names: int = 1000) -> np.ndarray:
    """Sessions: dates with at least `min_names` bars (drops stray weekend or holiday prints)."""
    n = df.groupby("date").size()
    return np.array(sorted(n.index[n >= min_names]), dtype=object)


# ---------------------------------------------------------------- point-in-time features

def add_features(df: pd.DataFrame, presorted: bool = False) -> pd.DataFrame:
    """Per name, from prior bars only: prev_close, avg_vol14, vol20, dv20, atr14 (Wilder, seeded with the mean of the
    first 14 true ranges, as `strategy_f_intraday._features`), n_prior; plus today's ret."""
    if not presorted:
        df = df.sort_values(["act_symbol", "date"], kind="stable").reset_index(drop=True)
    g = df.groupby("act_symbol", sort=False)
    n = g.cumcount().to_numpy()
    df["n_prior"] = n
    df["prev_close"] = g["close"].shift(1)
    df["ret"] = df["close"] / df["prev_close"] - 1

    def prior_mean(s: pd.Series, w: int) -> pd.Series:
        out = s.shift(1).rolling(w, min_periods=w).mean()
        return out.where(n >= w)                     # windows that reach into the previous name are void

    df["avg_vol14"] = prior_mean(df["volume"], 14)
    df["vol20"] = prior_mean(df["volume"], 20)
    df["dv20"] = prior_mean(df["close"] * df["volume"], 20)
    pc = df["prev_close"]
    tr = np.maximum.reduce([(df["high"] - df["low"]).to_numpy(), (df["high"] - pc).abs().to_numpy(),
                            (df["low"] - pc).abs().to_numpy()])
    tr = pd.Series(np.where(n >= 1, tr, np.nan), index=df.index)
    seed = tr.rolling(14, min_periods=14).mean()
    z = pd.Series(np.where(n == 14, seed, np.where(n > 14, tr, np.nan)), index=df.index)
    atr_after = z.groupby(df["act_symbol"], sort=False).transform(lambda s: s.ewm(alpha=1 / 14, adjust=False).mean())
    df["atr14"] = atr_after.groupby(df["act_symbol"], sort=False).shift(1)
    return df


def _session_index(dates, cal: np.ndarray) -> np.ndarray:
    return pd.Index(cal).get_indexer(pd.Index(dates))


def _to_dates(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s).dt.date if len(s) else s


def split_blocked(panel: pd.DataFrame, splits: pd.DataFrame, cal: np.ndarray) -> np.ndarray:
    """True where the name has a split with ex-date in [t-20, t+1] sessions. A non-session ex-date counts as the next
    session."""
    di = panel["di"].to_numpy() if "di" in panel else _session_index(panel["date"], cal)
    if not len(splits):
        return np.zeros(len(panel), dtype=bool)
    ex = np.searchsorted(cal, _to_dates(splits["ex_date"]).to_numpy(), side="left")
    off = np.arange(-1, 21)
    blk = pd.DataFrame({"act_symbol": np.repeat(splits["act_symbol"].map(norm).to_numpy(), len(off)),
                        "di": (ex[:, None] + off[None, :]).ravel()}).drop_duplicates()
    blk["blk"] = True
    key = pd.DataFrame({"act_symbol": panel["act_symbol"].to_numpy(), "di": di})
    return key.merge(blk, on=["act_symbol", "di"], how="left")["blk"].fillna(False).to_numpy(dtype=bool)


# ---------------------------------------------------------------- panel, signal, trade

def build_panel(ohlcv: pd.DataFrame, symbols: pd.DataFrame, splits: pd.DataFrame, dividends: pd.DataFrame,
                exclude: set[str], cal: np.ndarray | None = None, min_names: int = 1000) -> pd.DataFrame:
    df = ohlcv.copy()
    df["act_symbol"] = df["act_symbol"].map(norm)
    keep = eligible_symbols(symbols) - set(exclude)
    df = df[df["act_symbol"].isin(keep)]
    if cal is None:
        cal = calendar(df, min_names)
    df = df.assign(di=_session_index(df["date"], cal))
    df = df[df["di"] >= 0].sort_values(["act_symbol", "di"], kind="stable").reset_index(drop=True)
    df = add_features(df, presorted=True)
    g = df.groupby("act_symbol", sort=False)
    df["next_close"] = g["close"].shift(-1)
    df["next_ok"] = (g["di"].shift(-1) == df["di"] + 1).to_numpy()
    if len(dividends):
        dv = dividends.assign(act_symbol=dividends["act_symbol"].map(norm),
                              di=np.searchsorted(cal, _to_dates(dividends["ex_date"]).to_numpy(), side="left") - 1)
        dv = dv.groupby(["act_symbol", "di"], as_index=False)["amount"].sum()
        df = df.merge(dv.rename(columns={"amount": "div_next"}), on=["act_symbol", "di"], how="left")
    else:
        df["div_next"] = np.nan
    df["div_next"] = df["div_next"].fillna(0.0)
    df["short_ret"] = np.where(df["next_ok"], (df["close"] - df["next_close"] - df["div_next"]) / df["close"], np.nan)
    df["rvol"] = df["volume"] / df["vol20"]
    df["floors"] = ((df["n_prior"] >= 20) & (df["prev_close"] >= FLOORS["min_close"])
                    & (df["avg_vol14"] >= FLOORS["min_avg_vol14"]) & (df["atr14"] >= FLOORS["min_atr"])
                    & (df["dv20"] >= FLOORS["min_dv20"]))
    df["split_block"] = split_blocked(df, splits, cal)
    df["universe"] = df["floors"] & ~df["split_block"]
    bench = df[df["universe"] & df["next_ok"]].groupby("di")["short_ret"].agg(["mean", "size"])
    df = df.merge(bench.rename(columns={"mean": "univ_short", "size": "univ_n"}), left_on="di", right_index=True,
                  how="left")
    last = dict(zip(symbols["act_symbol"].map(norm), symbols.get("last_seen", pd.Series(dtype=object))))
    df["last_seen"] = df["act_symbol"].map(last)
    return df


def _signal_mask(p: pd.DataFrame, rvol_min=None, rvol_max=None, up_min=UP_MIN, period=PERIOD) -> pd.Series:
    m = (p["close"] >= (1 + up_min) * p["prev_close"]) & (p["date"] >= period[0]) & (p["date"] <= period[1])
    if rvol_min is not None:
        m &= p["volume"] >= rvol_min * p["vol20"]
    if rvol_max is not None:
        m &= p["volume"] < rvol_max * p["vol20"]
    return m


def signals(p: pd.DataFrame, **kw) -> pd.DataFrame:
    m = _signal_mask(p, **kw) & p["universe"] & p["next_ok"]
    return p.loc[m].copy()


def counts(p: pd.DataFrame, **kw) -> dict:
    m = _signal_mask(p, **kw) & p["floors"]
    return {"signals_before_checks": int(m.sum()), "split_window": int((m & p["split_block"]).sum()),
            "no_next_session_bar": int((m & ~p["split_block"] & ~p["next_ok"]).sum()),
            "traded": int((m & p["universe"] & p["next_ok"]).sum())}


def net_returns(tr: pd.DataFrame, side_bp: float = SIDE_BP) -> pd.DataFrame:
    tr = tr.copy()
    tr["raw"] = tr["short_ret"] - 2 * side_bp / 1e4
    tr["excess"] = tr["raw"] - tr["univ_short"]
    return tr


# ---------------------------------------------------------------- statistics

def stats(df: pd.DataFrame, col: str) -> dict:
    x = df[col].astype(float)
    if not len(x):
        return {"trades": 0, "days": 0}
    day = x.groupby(df["date"]).mean()
    nd = len(day)
    sd = day.std(ddof=1) if nd > 1 else float("nan")
    pos, neg = x[x > 0].sum(), -x[x < 0].sum()
    return {"trades": int(len(x)), "days": int(nd), "mean_bp": float(x.mean() * 1e4),
            "day_mean_bp": float(day.mean() * 1e4),
            "t": float(day.mean() / (sd / np.sqrt(nd))) if nd > 1 and sd > 0 else None,
            "pf": float(pos / neg) if neg > 0 else None, "win": float((x > 0).mean())}


def halves(df: pd.DataFrame, col: str) -> dict:
    return {k: stats(df[(df["date"] >= a) & (df["date"] <= b)], col) for k, (a, b) in HALVES.items()}


def by_year(df: pd.DataFrame, col: str) -> dict:
    yrs = df["date"].map(lambda d: d.year)
    return {int(y): stats(df[yrs == y], col) for y in sorted(yrs.unique())}


def verdict(s: dict) -> dict:
    h = s["halves"]
    checks = {"profit factor >= 1.1": s["pf"] is not None and s["pf"] >= PF_BAR,
              "mean > 0 in both halves": all((h[k].get("day_mean_bp") or 0) > 0 for k in HALVES),
              "day-clustered t >= 2.58": s["t"] is not None and s["t"] >= T_BAR,
              "mean > 0 at 10 bp per side": (s["stress_day_mean_bp"] or 0) > 0}
    return {"pass": all(checks.values()), "checks": checks,
            "t_clears_single_test_2.33": s["t"] is not None and s["t"] >= T_SINGLE}


# ---------------------------------------------------------------- report

def row(tr: pd.DataFrame, col: str = "excess") -> dict:
    return {**stats(tr, col), "halves": halves(tr, col)}


def run(p: pd.DataFrame) -> dict:
    main = net_returns(signals(p, rvol_min=RVOL_MIN))
    stress = net_returns(signals(p, rvol_min=RVOL_MIN), STRESS_BP)
    head = row(main)
    head["stress_day_mean_bp"] = stats(stress, "excess").get("day_mean_bp")
    head["stress_mean_bp"] = stats(stress, "excess").get("mean_bp")
    gone = main["last_seen"].map(lambda s: isinstance(s, str) and s < LISTED_SINCE.isoformat())
    in_period = p[(p["date"] >= PERIOD[0]) & (p["date"] <= PERIOD[1])]
    big = main.reindex(main["excess"].abs().sort_values(ascending=False).index[:10])
    return {
        "verdict": verdict(head),
        "main_rvol2_excess": head,
        "reported_only": {
            "control_rvol_below_1.5": row(net_returns(signals(p, rvol_max=CONTROL_MAX))),
            "f2p_rvol_1.8": row(net_returns(signals(p, rvol_min=F2P_RVOL))),
            "main_raw_not_excess": row(main, "raw"),
            "main_by_year": by_year(main, "excess"),
            "main_no_longer_listed": stats(main[gone], "excess"),
            "main_still_listed": stats(main[~gone], "excess"),
            "diagnostic_abs_short_ret_le_50pct": stats(main[main["short_ret"].abs() <= 0.5], "excess"),
            "diagnostic_largest_10_by_abs_excess": [
                {"symbol": r.act_symbol, "date": r.date.isoformat(), "close": r.close, "next_close": r.next_close,
                 "ret": r.ret, "rvol": r.rvol, "excess": r.excess} for r in big.itertuples()],
        },
        "counts": {
            "symbols_in_panel": int(p["act_symbol"].nunique()),
            "universe_name_days": int(in_period["universe"].sum()),
            "universe_names": int(in_period.loc[in_period["universe"], "act_symbol"].nunique()),
            "universe_names_no_longer_listed": int(in_period.loc[in_period["universe"]]
                                                   .drop_duplicates("act_symbol")["last_seen"]
                                                   .map(lambda s: isinstance(s, str) and s < LISTED_SINCE.isoformat())
                                                   .sum()),
            "mean_universe_size_per_day": float(in_period[in_period["universe"]].groupby("di").size().mean()),
            "main": counts(p, rvol_min=RVOL_MIN),
            "control": counts(p, rvol_max=CONTROL_MAX),
        },
    }


def load(args) -> tuple:
    import pyarrow.parquet as pq
    ohlcv = pq.read_table(args.ohlcv).to_pandas()
    symbols = pd.read_csv(args.symbols, dtype=str, keep_default_na=False, na_values=[""])
    splits = pd.read_csv(args.splits, dtype={"act_symbol": str})
    dividends = pd.read_csv(args.dividends, dtype={"act_symbol": str})
    sp500 = pd.read_csv(args.sp500, dtype=str)["Symbol"].tolist()
    names505 = [s for s in Path(args.names505).read_text().split() if s]
    return ohlcv, symbols, splits, dividends, excluded_symbols(sp500, names505, F2_NAMES, E_NAMES), len(names505)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ohlcv", default=DATA / "ohlcv_2015.parquet")
    ap.add_argument("--symbols", default=DATA / "symbol.csv")
    ap.add_argument("--splits", default=DATA / "split.csv")
    ap.add_argument("--dividends", default=DATA / "dividend.csv")
    ap.add_argument("--sp500", default=DATA / "constituents.csv")
    ap.add_argument("--names505", default=DATA / "names505.txt")
    ap.add_argument("--out", default=DATA / "x3_results.json")
    a = ap.parse_args()
    ohlcv, symbols, splits, dividends, exclude, n505 = load(a)
    raw_syms = set(ohlcv["act_symbol"].map(norm))
    p = build_panel(ohlcv, symbols, splits, dividends, exclude)
    res = run(p)
    res["counts"].update({"symbols_in_ohlcv": len(raw_syms),
                          "symbols_not_in_symbol_table": len(raw_syms - set(symbols["act_symbol"].map(norm))),
                          "excluded_list_size": len(exclude), "names505": n505})
    res["inputs"] = {k: str(v) for k, v in vars(a).items()}
    Path(a.out).write_text(json.dumps(res, indent=1, default=str))
    h, v = res["main_rvol2_excess"], res["verdict"]
    print(f"X3 {'PASS' if v['pass'] else 'FAIL'}: {h['trades']} trades on {h['days']} days, day mean "
          f"{h['day_mean_bp']:+.1f} bp, t {h['t']:+.2f}, PF {h['pf']:.2f}, 10bp/side {h['stress_day_mean_bp']:+.1f} bp")
    for k, ok in v["checks"].items():
        print(f"  {'ok ' if ok else 'NO '} {k}")
    print(f"-> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
