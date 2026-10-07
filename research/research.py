"""Pre-registered test of intraday SPX/SPY strategies. Parameters come from the published papers, not fitted here.
In-sample 2005-2014, out-of-sample 2015-2020 (1m S&P 500 CFD), recent check 2025-04..2026-03 (SPY 5m)."""
import math, sys, json
from datetime import date, timedelta
import numpy as np, pandas as pd
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from agentdesk.indicators import MACD, RSI

def matrices(df, res):
    """day x bar matrices of O,H,L,C,V for RTH; res = minutes per bar."""
    nb = 390 // res
    df = df.copy()
    df["b"] = (df["hm"] - 570) // res
    df = df[(df.b >= 0) & (df.b < nb)]
    days = sorted(df["day"].unique())
    idx = {d: i for i, d in enumerate(days)}
    M = {k: np.full((len(days), nb), np.nan) for k in "OHLCV"}
    di = df["day"].map(idx).values; bi = df["b"].values
    for k, col in zip("OHLCV", ["open", "high", "low", "close", "volume"]):
        M[k][di, bi] = df[col].values
    for k in "OHLC":                                  # forward-fill gaps within a day (never from a later bar)
        a = pd.DataFrame(M[k]).ffill(axis=1).values
        M[k] = a
    M["V"] = np.nan_to_num(M["V"], nan=0.0) + 1e-9
    keep = ~np.isnan(M["O"][:, 0])                    # a session without its 09:30 bar is dropped, not back-filled
    if not keep.all():
        days = [d for d, k in zip(days, keep) if k]
        M = {k: v[keep] for k, v in M.items()}
    return days, M, nb


# NYSE closures the rule-based calendar misses (same set as book_h_candidates.py)
EXTRA_CLOSURES = {date(2007, 1, 2), date(2012, 10, 29), date(2012, 10, 30)}


def prior_session_close(days, C):
    """prevC per session: the previous row's last close only when that row is the prior NYSE session; NaN when the
    data lacks that session (a dropped half day or gap), instead of an older close."""
    here = str(__import__("pathlib").Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.append(here)
    import nyse_calendar
    if not len(days):
        return np.array([])
    closed = nyse_calendar.holidays_between(days[0].year - 1, days[-1].year + 1) | EXTRA_CLOSURES
    out = np.full(len(days), np.nan)
    for i in range(1, len(days)):
        p = days[i] - timedelta(days=1)
        while p.weekday() >= 5 or p in closed:
            p -= timedelta(days=1)
        if days[i - 1] == p:
            out[i] = C[i - 1, -1]
    return out

def bp(x): return x * 1e4

def strategies(days, M, res):
    O, H, L, C, V = M["O"], M["H"], M["L"], M["C"], M["V"]
    nd, nb = C.shape
    b = lambda minute: minute // res                  # bar index that *ends* at minute offset
    last = nb - 1
    prevC = prior_session_close(days, C)
    op = O[:, 0]
    out = {}
    # 1 Gao-Han-Li-Zhou intraday momentum: (prev close -> 10:00) predicts 15:30 -> 16:00
    r1 = C[:, b(29)] / prevC - 1
    r13 = C[:, last] / C[:, b(359)] - 1
    out["IM last30 (both)"] = np.sign(r1) * r13
    out["IM last30 (long only)"] = np.where(r1 > 0, r13, 0.0)
    # 8 day's move (open -> 15:00) continues into last hour
    rd = C[:, b(329)] / op - 1
    rl = C[:, last] / C[:, b(329)] - 1
    out["Late-day momentum 15:00-16:00"] = np.sign(rd) * rl
    # 5 first hour continuation: sign(open->10:30) held 10:30->close
    rf = C[:, b(59)] / op - 1
    out["First-hour continuation"] = np.sign(rf) * (C[:, last] / C[:, b(59)] - 1)
    # 4 gap fade at 0.5%
    gap = op / prevC - 1
    oc = C[:, last] / op - 1
    out["Gap fade >0.5% (both)"] = np.where(gap < -0.005, oc, np.where(gap > 0.005, -oc, 0.0))
    out["Gap fade >0.5% (long only)"] = np.where(gap < -0.005, oc, 0.0)
    # 3 5-minute opening range breakout, stop at the other side of the range, exit at close
    orh = H[:, :b(5)].max(1) if res == 1 else H[:, 0]
    orl = L[:, :b(5)].min(1) if res == 1 else L[:, 0]
    oc5 = C[:, b(5) - 1]
    d = np.sign(oc5 - op)
    entry = oc5
    pnl_orb = np.zeros(nd)
    for i in range(nd):
        if d[i] == 0 or np.isnan(entry[i]):
            continue
        seg_l, seg_h = L[i, b(5):], H[i, b(5):]
        if d[i] > 0:
            hit = np.where(seg_l <= orl[i])[0]
            ex = orl[i] if len(hit) else C[i, last]
        else:
            hit = np.where(seg_h >= orh[i])[0]
            ex = orh[i] if len(hit) else C[i, last]
        pnl_orb[i] = d[i] * (ex / entry[i] - 1)
    out["ORB 5m (both)"] = pnl_orb
    out["ORB 5m (long only)"] = np.where(d > 0, pnl_orb, 0.0)
    # 2 Zarattini-Aziz-Barbon noise-boundary momentum (half-hour checks, VWAP trailing stop)
    move = np.abs(C / op[:, None] - 1)
    sig = pd.DataFrame(move).rolling(14, min_periods=14).mean().shift(1).values
    upper_ref = np.maximum(op, np.nan_to_num(prevC, nan=0))
    lower_ref = np.minimum(op, np.where(np.isnan(prevC), op, prevC))
    tp = (H + L + C) / 3
    vwap = np.cumsum(tp * V, 1) / np.cumsum(V, 1)
    checks = [b(m) for m in range(29, 390, 30)] + [last]
    zab_both, zab_long = np.zeros(nd), np.zeros(nd)
    for i in range(nd):
        if np.isnan(sig[i, 0]) or np.isnan(prevC[i]):
            continue
        for mode, arr in (("both", zab_both), ("long", zab_long)):
            pos, p = 0, 0.0
            for j, k in enumerate(checks[:-1]):
                ub = upper_ref[i] * (1 + sig[i, k]); lb = lower_ref[i] * (1 - sig[i, k])
                c = C[i, k]
                if pos == 1 and c < max(ub, vwap[i, k]): pos = 0
                if pos == -1 and c > min(lb, vwap[i, k]): pos = 0
                if pos == 0:
                    if c > ub: pos = 1
                    elif c < lb and mode == "both": pos = -1
                nxt = checks[j + 1]
                p += pos * (C[i, nxt] / c - 1)
            arr[i] = p
    out["Noise-boundary momentum (both)"] = zab_both
    out["Noise-boundary momentum (long only)"] = zab_long
    # 6 your MACD rules, proxied on 5m: 5m cross-up, 15m MACD>signal, RSI 30-70 on 5m & 15m; hold 30 min, flat by 15:50
    pnl_macd = np.zeros(nd); n_macd = 0
    step5 = max(1, 5 // res); step15 = 15 // res
    m5, r5, m15, r15 = MACD(), RSI(), MACD(), RSI()
    for i in range(nd):
        prev5 = None
        for k in range(step5 - 1, nb, step5):
            c = C[i, k]
            v5, rv5 = m5.update(c), r5.update(c)
            if (k + 1) % step15 == 0:
                m15.update(c); r15.update(c)
                p15, pr15 = m15.value, r15.value
            else:
                p15, pr15 = m15.preview(c), r15.preview(c)
            if v5 and prev5 and p15 and rv5 and pr15 and (not prev5.bull) and v5.bull and p15.bull \
                    and 30 < rv5 < 70 and 30 < pr15 < 70 and k + b(30) <= b(379):
                pnl_macd[i] += C[i, k + b(30)] / c - 1; n_macd += 1
            prev5 = v5
    out["Your MACD rules (5m proxy, 30m hold)"] = pnl_macd
    return out, days

def stats(x, days_mask):
    x = np.nan_to_num(x[days_mask])
    traded = x[x != 0]
    n = len(x)
    mu, sd = x.mean(), x.std(ddof=1)
    t = mu / (sd / math.sqrt(n)) if sd > 0 else 0
    return {"days": n, "trades": len(traded), "avg_bp_per_trade": bp(traded.mean()) if len(traded) else 0,
            "hit": (traded > 0).mean() if len(traded) else 0, "sharpe": mu / sd * math.sqrt(252) if sd > 0 else 0,
            "t": t, "total_bp": bp(x.sum())}

if __name__ == "__main__":
    spx = pd.read_pickle("data/spx_rth_1m.pkl")
    days, M, nb = matrices(spx, 1)
    out, days = strategies(days, M, 1)
    yrs = np.array([d.year for d in days])
    import glob
    f = "data/spy_5m_2025_2026.csv"
    spy = pd.read_csv(f)
    spy["et"] = pd.to_datetime(spy["Datetime"], utc=True).dt.tz_convert("America/New_York")
    spy["day"] = spy["et"].dt.date; spy["hm"] = spy["et"].dt.hour * 60 + spy["et"].dt.minute
    spy = spy.rename(columns=str.lower)
    d2, M2, _ = matrices(spy, 5)
    out2, d2 = strategies(d2, M2, 5)
    rows = []
    for k in out:
        a = stats(out[k], (yrs >= 2005) & (yrs <= 2014))
        o = stats(out[k], yrs >= 2015)
        r = stats(out2[k], np.ones(len(d2), bool))
        rows.append((k, a, o, r))
    K = len(rows)
    zcrit = 2.86   # two-sided Bonferroni for 12 tests at 5%
    print(f"S&P 500 1m: {days[0]}..{days[-1]} ({len(days)} sessions); SPY 5m recent: {d2[0]}..{d2[-1]} ({len(d2)} sessions); {K} strategies, Bonferroni |t|>{zcrit}")
    hdr = f"{'strategy':40s} | {'IS 2005-14: bp/trade  hit  Sharpe   t':38s} | {'OOS 2015-20: bp/trade hit Sharpe  t':37s} | {'2025-26: bp/trade Sharpe  t':28s}"
    print(hdr); print("-" * len(hdr))
    res = {}
    for k, a, o, r in rows:
        flag = "PASS" if abs(a["t"]) > zcrit and np.sign(o["t"]) == np.sign(a["t"]) and abs(o["t"]) > 1.96 else ""
        print(f"{k:40s} | {a['avg_bp_per_trade']:7.2f} {a['hit']*100:5.1f}% {a['sharpe']:6.2f} {a['t']:6.2f} {'':10s} | "
              f"{o['avg_bp_per_trade']:7.2f} {o['hit']*100:5.1f}% {o['sharpe']:5.2f} {o['t']:5.2f} | {r['avg_bp_per_trade']:7.2f} {r['sharpe']:6.2f} {r['t']:5.2f} {flag}")
        res[k] = {"is": a, "oos": o, "recent": r, "pass": bool(flag)}
    json.dump(res, open("research_results.json", "w"), indent=1, default=float)
    
