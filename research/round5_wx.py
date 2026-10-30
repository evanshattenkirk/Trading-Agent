"""WX (research/round5_wx_prereg.md): W's chosen cell (13:00 ET, wings 2.0 straddles) on XSP 0DTE, held to cash
settlement at the SPX close / 10, paying Robinhood's index option fee. Research only.

    .venv/bin/python research/round5_wx.py --xsp data/thetadata/xsp_0dte --fred research/data/sp500_fred.csv \\
        --w-trades research/data/round5/w/trades.parquet --spy data/thetadata/spy_0dte \\
        --vix research/data/vix_cboe.csv --out research/data/round5/wx

Strikes, skips and fills are W's (round5_w.pick / _open). Settlement value: FRED SP500 / 10, else the 15:59 ET parity
spot (counted). Fees $0.54 per leg at the open, nothing at settlement. The fresh-path bar reads W's cached settlement
rows for QQQ and IWM (run round5_w.py with them first); --spy only adds the 13:00 leg-spread comparison.
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bd_real_quotes as bd  # noqa: E402
import round5_w as w5  # noqa: E402

ENTRY, WING = 13 * 60, 2.0
FEE_RH, FEE_STRESS = 0.54 / 100, 0.75 / 100      # $ per share per leg (Robinhood index fee $0.50 + $0.04 regulatory)
CAP = 300.0                                     # the account's max loss per B/C/D position, $ per lot
LAST = bd.CLOSE - 1                             # 15:59 ET
T_BAR, T_58, T_SINGLE, T_FRESH = 2.64, 2.58, 2.33, 1.65
MODELS = w5.MODELS
tstats, hhmm = w5.tstats, w5.hhmm


def load_fred(path) -> dict[date, float]:
    """FRED's SP500 csv: observation_date, SP500; blanks and '.' are missing days."""
    f = pd.read_csv(path, dtype=str)
    f.columns = ["date", "close"]
    f["close"] = pd.to_numeric(f["close"], errors="coerce")
    f = f.dropna()
    return {date.fromisoformat(d): float(c) for d, c in zip(f["date"], f["close"])}


def settle_spot(p: bd.QuotePanel, day: date, fred: dict) -> tuple[float | None, str | None]:
    if day in fred:
        return fred[day] / 10, "fred"
    s = bd.spot(p, min(p.last_minute, LAST))
    return (s, "parity") if s is not None else (None, None)


def leg_spreads(p: bd.QuotePanel, minute: int, w: float) -> list[float] | None:
    s = w5.pick(p, minute, w)
    if "skip" in s:
        return None
    qs = [p.ba(r, k, minute) for r, k, _ in w5._legs(s)]
    return None if any(q is None for q in qs) else [q[1] - q[0] for q in qs]


# ---------------------------------------------------------------- one session file

def _skip_rows(base: dict, why: str) -> list[dict]:
    return [{**base, "variant": "settle", "entry": ENTRY, "w": WING, "model": m, "skip": why} for m in MODELS]


def session_rows(und: str, day: date, path: str, fred: dict) -> list[dict]:
    base = {"und": und, "day": day}
    why = w5.session_reason(day)
    if why:
        return _skip_rows(base, why)
    try:
        p = bd.QuotePanel(bd.normalize(pd.read_parquet(path)))
    except Exception as e:  # noqa: BLE001 - counted as a skip
        return _skip_rows(base, f"unreadable: {e!r}"[:80])
    if p.last_minute <= ENTRY:
        return _skip_rows(base, "no_pm_quotes")
    s_end, src = settle_spot(p, day, fred)
    if s_end is None:
        return _skip_rows(base, "no_settlement_value")
    sp = leg_spreads(p, ENTRY, WING)
    info = {"s_settle": s_end, "settle_src": src, "s_parity": bd.spot(p, min(p.last_minute, LAST)),
            "spread_med": float(np.median(sp)) if sp else None}
    out = []

    def add(variant, entry, w, model, r):
        keep = {k: r[k] for k in ("pnl", "max_loss", "ret", "credit", "M", "wings_sold", "exit_minute") if k in r}
        out.append({**base, **info, "variant": variant, "entry": entry, "w": w, "model": model,
                    "skip": r.get("skip"), **keep})

    for m in MODELS:
        add("settle", ENTRY, WING, m, w5.settlement(p, ENTRY, WING, m, fee=FEE_RH, s_end=s_end))
        add("settle_fee075", ENTRY, WING, m, w5.settlement(p, ENTRY, WING, m, fee=FEE_STRESS, s_end=s_end))
        add("x1540", ENTRY, WING, m, w5.trade(p, ENTRY, WING, m, fee=FEE_RH))
        for e in w5.ENTRIES:
            for w in w5.WINGS:
                add("settle_cell", e, w, m, w5.settlement(p, e, w, m, fee=FEE_RH, s_end=s_end))
    return out


def spread_row(und: str, day: date, path: str) -> dict:
    try:
        p = bd.QuotePanel(bd.normalize(pd.read_parquet(path)))
    except Exception:  # noqa: BLE001
        return {"und": und, "day": day, "spread_med": None}
    sp = leg_spreads(p, ENTRY, WING)
    return {"und": und, "day": day, "spread_med": float(np.median(sp)) if sp else None}


def _job(args):
    return session_rows(*args)


def _spread_job(args):
    return spread_row(*args)


def _files(folder) -> list[tuple[date, str]]:
    out = []
    for f in sorted(Path(folder).glob("*.parquet")):
        try:
            out.append((date.fromisoformat(f.stem), str(f)))
        except ValueError:
            continue
    return out


def collect(folder, fred: dict, workers: int = 4) -> pd.DataFrame:
    jobs = [("XSP", d, f, fred) for d, f in _files(folder)]
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, r in enumerate(ex.map(_job, jobs, chunksize=4), 1):
            rows += r
            if i % 200 == 0:
                print(f"  {i}/{len(jobs)} sessions", flush=True)
    return pd.DataFrame(rows)


def collect_spreads(folder, workers: int = 4) -> pd.DataFrame:
    jobs = [("SPY", d, f) for d, f in _files(folder) if not w5.session_reason(d)]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return pd.DataFrame(list(ex.map(_spread_job, jobs, chunksize=8)))


# ---------------------------------------------------------------- bars

def halves(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split at the median trade date: the first half takes the median date itself."""
    days = sorted(df["day"])
    if not days:
        return df, df
    cut = days[(len(days) - 1) // 2]
    return df[df["day"] <= cut], df[df["day"] > cut]


def verdict(b: dict) -> dict:
    def pos(x):
        return x is not None and x > 0
    t = b["main_t"]
    b1 = pos(b["main_mean"]) and t is not None and t >= T_BAR
    b2 = pos(b["taker_mean"])
    b3 = len(b["half_means"]) == 2 and all(pos(x) for x in b["half_means"])
    b4 = pos(b["cap_mean"])
    out = {"bar_1": bool(b1), "bar_2": bool(b2), "bar_3": bool(b3), "bar_4": bool(b4),
           "bars_1_4": bool(b1 and b2 and b3 and b4),
           "t_clears_2.58": t is not None and t >= T_58, "t_clears_2.33": t is not None and t >= T_SINGLE}
    if b.get("fresh_mean") is None:
        return {"pass": False, "provisional": True, **out}
    b5 = b["fresh_mean"] > 0 and b["fresh_t"] is not None and b["fresh_t"] >= T_FRESH
    return {"pass": bool(out["bars_1_4"] and b5), "provisional": False, "bar_5": bool(b5), **out}


# ---------------------------------------------------------------- report

def _sel(df, variant="settle", model="patient", entry=ENTRY, w=WING, unds=None):
    m = (df["variant"] == variant) & (df["model"] == model) & df["skip"].isna()
    if entry is not None:
        m &= df["entry"] == entry
    if w is not None:
        m &= df["w"] == w
    if unds is not None:
        m &= df["und"].isin(unds)
    return df[m]


def _ml(x: pd.Series) -> dict:
    return {"median": float(x.median()), "p90": float(x.quantile(0.9)), "share_above_300": float((x > CAP).mean())} \
        if len(x) else {}


TRADE_COLS = ("pnl", "max_loss", "ret", "credit", "M", "wings_sold", "exit_minute",
              "s_settle", "settle_src", "s_parity", "spread_med")


def report(df: pd.DataFrame, w: pd.DataFrame | None, vix: pd.Series | None, spy_spreads: pd.DataFrame | None) -> dict:
    df = df.reindex(columns=list(dict.fromkeys([*df.columns, *TRADE_COLS])))   # a run where every session skipped
    main = _sel(df).sort_values("day")
    taker = _sel(df, model="taker")
    h1, h2 = halves(main)
    cap = main[main["max_loss"] <= CAP]
    fresh = pd.Series(dtype=float)
    fresh_unds = []
    if w is not None and len(w):
        fresh_unds = [u for u in ("QQQ", "IWM") if u in set(w["und"])]
        if fresh_unds:
            fresh = w5.daily(_sel(w, unds=fresh_unds))
    hs = [tstats(h1["ret"]), tstats(h2["ret"])]
    b = {"main_mean": tstats(main["ret"])["mean"], "main_t": tstats(main["ret"])["t"],
         "taker_mean": tstats(taker["ret"])["mean"], "half_means": [h["mean"] for h in hs],
         "cap_mean": tstats(cap["ret"])["mean"],
         "fresh_mean": tstats(fresh)["mean"] if len(fresh) else None,
         "fresh_t": tstats(fresh)["t"] if len(fresh) else None}
    out = {"bars": b, "verdict": verdict(b), "fresh_underlyings": fresh_unds,
           "xsp_patient": tstats(main["ret"]), "xsp_taker": tstats(taker["ret"]),
           "xsp_mid": tstats(_sel(df, model="mid")["ret"]),
           "halves_patient": {f"to {h1['day'].max()}" if len(h1) else "first": hs[0],
                              f"after {h1['day'].max()}" if len(h1) else "second": hs[1]},
           "cap_300_patient": tstats(cap["ret"]),
           "fresh_qqq_iwm_settlement_patient": tstats(fresh) if len(fresh) else None}
    rep = {"fee_0.75_patient": tstats(_sel(df, variant="settle_fee075")["ret"]),
           "exit_1540_rh_fees": {m: tstats(_sel(df, variant="x1540", model=m)["ret"]) for m in MODELS}}
    cells = {}
    for e in w5.ENTRIES:
        for wing in w5.WINGS:
            for m in MODELS:
                cells[f"XSP {m} {hhmm(e)} w{wing}"] = tstats(_sel(df, variant="settle_cell", model=m, entry=e,
                                                                 w=wing)["ret"])
                if w is not None and len(w):
                    cells[f"SPY {m} {hhmm(e)} w{wing}"] = tstats(_sel(w, model=m, entry=e, w=wing,
                                                                     unds=["SPY"])["ret"])
    rep["nine_cells_to_settlement"] = cells
    yr = main["day"].map(lambda d: d.year)
    rep["by_year_patient"] = {int(y): tstats(g["ret"]) for y, g in main.groupby(yr)}
    rep["sessions_by_weekday"] = {k: int(v) for k, v in
                                  main["day"].map(lambda d: d.strftime("%a")).value_counts().items()}
    rep["daily_era_patient"] = {"before 2022-11-14": tstats(main.loc[main["day"] < bd.DAILY_ERA, "ret"]),
                                "from 2022-11-14": tstats(main.loc[main["day"] >= bd.DAILY_ERA, "ret"])}
    if vix is not None:
        pv = main["day"].map(lambda x: bd.prev_close(vix, x))
        rep["prior_vix_patient"] = {lab: tstats(main.loc[(pv >= lo) & (pv < hi), "ret"])
                                    for lo, hi, lab in w5.VIX_BUCKETS}
    if w is not None and len(w):
        spy = _sel(w, unds=["SPY"])[["day", "ret"]].rename(columns={"ret": "spy"})
        both = main[["day", "ret"]].merge(spy, on="day")
        rep_same = {"n": int(len(both))}
        if len(both) > 2:
            sd_ok = both["ret"].std() > 0 and both["spy"].std() > 0
            rep_same.update({"corr": float(both["ret"].corr(both["spy"])) if sd_ok else None,
                             "mean_xsp": float(both["ret"].mean()), "mean_spy": float(both["spy"].mean()),
                             "mean_gap_xsp_minus_spy": tstats(both["ret"] - both["spy"])})
        out["xsp_vs_spy_same_day"] = rep_same
    sx = df[(df["variant"] == "settle") & (df["model"] == "patient")].dropna(subset=["spread_med"])
    rep["leg_spread_1300_median"] = {"XSP": float(sx["spread_med"].median()) if len(sx) else None}
    if spy_spreads is not None and len(spy_spreads):
        rep["leg_spread_1300_median"]["SPY"] = float(spy_spreads["spread_med"].dropna().median())
    allp = df[(df["variant"] == "settle") & (df["model"] == "patient")]
    rep["skips_patient"] = {"sessions": int(len(allp)),
                            **{str(k): int(v) for k, v in allp["skip"].fillna("traded").value_counts().items()}}
    rep["max_loss_per_lot"] = _ml(main["max_loss"])
    rep["worst_5_patient"] = main.nsmallest(5, "pnl")[["day", "pnl", "max_loss", "ret"]] \
        .assign(day=lambda x: x["day"].astype(str)).to_dict("records")
    out["settlement_source"] = {str(k): int(v) for k, v in main["settle_src"].value_counts().items()}
    chk = main[main["settle_src"] == "fred"].dropna(subset=["s_parity"])
    gap = (chk["s_settle"] - chk["s_parity"]).abs()
    out["fred_vs_parity"] = {"n": int(len(chk)), **({"median_abs": float(gap.median()),
                                                    "p90_abs": float(gap.quantile(0.9)),
                                                    "max_abs": float(gap.max())} if len(chk) else {})}
    out["date_range"] = [str(main["day"].min()), str(main["day"].max())] if len(main) else None
    out["reported_only"] = rep
    return out


def _f(v, fmt):
    return "n/a" if v is None else format(v, fmt)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xsp", type=Path, required=True, help="folder of XSP YYYY-MM-DD.parquet files")
    ap.add_argument("--fred", type=Path, default=Path("research/data/sp500_fred.csv"))
    ap.add_argument("--w-trades", type=Path, default=Path("research/data/round5/w/trades.parquet"))
    ap.add_argument("--spy", type=Path, default=None, help="SPY 0DTE folder, for the leg-spread comparison only")
    ap.add_argument("--vix", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=Path("research/data/round5/wx"))
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    df = collect(a.xsp, load_fred(a.fred), a.workers)
    df.to_parquet(a.out / "trades.parquet", index=False)
    w = None
    if a.w_trades.exists():
        w = pd.read_parquet(a.w_trades)
        w["day"] = pd.to_datetime(w["day"]).dt.date
    spy_spreads = collect_spreads(a.spy, a.workers) if a.spy else None
    vix = bd.load_vix(a.vix) if a.vix else None
    res = report(df, w, vix, spy_spreads)
    res["inputs"] = {k: str(v) for k, v in vars(a).items()}
    (a.out / "wx_results.json").write_text(json.dumps(res, indent=1, default=str))
    v, b, x = res["verdict"], res["bars"], res["xsp_patient"]
    print(f"WX {'PASS' if v['pass'] else 'FAIL'}{' (provisional: no fresh QQQ/IWM rows)' if v['provisional'] else ''}"
          f": {x['n']} XSP trades {res['date_range']}, patient mean {_f(b['main_mean'], '+.4f')} t "
          f"{_f(b['main_t'], '+.2f')}, taker mean {_f(b['taker_mean'], '+.4f')}, halves "
          f"{[_f(h, '+.4f') for h in b['half_means']]}, cap mean {_f(b['cap_mean'], '+.4f')}, "
          f"fresh mean {_f(b['fresh_mean'], '+.4f')} t {_f(b['fresh_t'], '+.2f')}")
    print(f"-> {a.out / 'wx_results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
