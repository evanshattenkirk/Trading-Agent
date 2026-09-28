"""Backtests for Strategy C (ChatGPT's bullish 30-min ORB + VWAP, three option expressions), the bearish
put mirror, Strategy B (iron fly with its exit rules) and Strategy D (10:00 ET iron condor).
Data: S&P 500 1m CFD 2005-2020 (scaled /10 to SPY-like $1 strikes) -> 5m bars; SPY 5m 2025-26.
Options: Black-Scholes, flat IV from the prior VIX close (sd_rth = 0.80 x VIX-implied day), per-leg cost
= half-spread + Robinhood fees ($0.04/contract/leg, from review_option_order on 2026-09-27).
Rules are frozen as given; nothing here is fitted."""
import math, sys, json
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
from research import matrices
from agentdesk.indicators import EMA, MACD, RSI

N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
def call(S, K, sd):
    if sd < 1e-9: return max(S - K, 0.0)
    d1 = (math.log(S / K) + 0.5 * sd * sd) / sd
    return S * N(d1) - K * N(d1 - sd)
def put(S, K, sd): return call(S, K, sd) - S + K

FEE = 0.04 / 100          # $ per share per leg per side
M_RTH = 0.80

def to5(M1):
    O, H, L, C, V = (M1[k] for k in "OHLCV")
    nd = C.shape[0]
    r = lambda a: a[:, :390].reshape(nd, 78, 5)
    return {"O": r(O)[:, :, 0], "H": r(H).max(2), "L": r(L).min(2), "C": r(C)[:, :, 4], "V": r(V).sum(2)}

def load(data):
    spx = pd.read_pickle(data / "spx_rth_1m.pkl")
    d1, M1, _ = matrices(spx, 1)
    M5 = to5(M1)
    for k in "OHLC": M5[k] = M5[k] / 10.0
    spy = pd.read_csv(data / "spy_5m_2025_2026.csv")
    spy["et"] = pd.to_datetime(spy["Datetime"], utc=True).dt.tz_convert("America/New_York")
    spy["day"] = spy["et"].dt.date; spy["hm"] = spy["et"].dt.hour * 60 + spy["et"].dt.minute
    d2, M2, _ = matrices(spy.rename(columns=str.lower), 5)
    vix = pd.read_csv(data / "vix.csv", parse_dates=["DATE"]).set_index("DATE")["CLOSE"]
    vix.index = vix.index.date
    for M in (M5, M2):                      # re-base every session to SPY ~765 so $1 strikes / $2 wings mean what they mean today
        f = 765.0 / M["O"][:, :1]
        for k in "OHLC": M[k] = M[k] * f
    return (d1, M5), (d2, M2), vix

def prev_vix(vix, days):
    vd = sorted(vix.index); out = []; j = 0
    for d in days:
        while j + 1 < len(vd) and vd[j + 1] < d: j += 1
        out.append(vix[vd[j]] if vd[j] < d else np.nan)
    return np.array(out)

def sd_left(sd_day, bar_close_idx):          # remaining RTH variance share (+15 min SPY 0DTE tail)
    f = max(0.0, (78 - 1 - bar_close_idx) / 78) + 15 / 390
    return M_RTH * sd_day * math.sqrt(f)

def indicators(M):
    C = M["C"]; nd, nb = C.shape
    ema = np.full(C.shape, np.nan); rsi = np.full(C.shape, np.nan); mac = np.full(C.shape, np.nan); sig = np.full(C.shape, np.nan)
    e, r, m = EMA(20), RSI(14), MACD()
    for i in range(nd):
        for k in range(nb):
            c = C[i, k]; ema[i, k] = e.update(c) or np.nan; rsi[i, k] = r.update(c) or np.nan
            v = m.update(c)
            if v: mac[i, k], sig[i, k] = v.macd, v.signal
    V = M["V"]; flat = V.reshape(-1)
    med = pd.Series(flat).rolling(20).median().shift(1).values.reshape(V.shape)
    tp = (M["H"] + M["L"] + C) / 3
    vwap = np.cumsum(tp * V, 1) / np.cumsum(V, 1)
    return ema, rsi, mac, sig, med, vwap

def orb_trades(M, ind, bull=True):
    O, H, L, C, V = (M[k] for k in "OHLCV")
    ema, rsi, mac, sig, med, vwap = ind
    nd, nb = C.shape
    out = []
    for i in range(nd):
        orh, orl = H[i, :6].max(), L[i, :6].min()
        n, k = 0, 6
        while k <= 59 and n < 2:              # bar k closes at 9:35 + 5k -> k=59 closes 14:30 ET
            if k < 3 or np.isnan(ema[i, k - 3]) or np.isnan(rsi[i, k]):
                k += 1; continue
            if bull:
                ok = (C[i, k] > orh and C[i, k - 1] <= orh and C[i, k] > vwap[i, k] and ema[i, k] > ema[i, k - 3]
                      and 55 <= rsi[i, k] <= 72 and C[i, k] > O[i, k] and V[i, k] >= 0.8 * med[i, k])
            else:
                ok = (C[i, k] < orl and C[i, k - 1] >= orl and C[i, k] < vwap[i, k] and ema[i, k] < ema[i, k - 3]
                      and 28 <= rsi[i, k] <= 45 and C[i, k] < O[i, k] and V[i, k] >= 0.8 * med[i, k])
            if not ok:
                k += 1; continue
            e = C[i, k]; stop = e * (1 - 0.0018) if bull else e * (1 + 0.0018); tgt = e * (1 + 0.0045) if bull else e * (1 - 0.0045)
            j, ex, why = k + 1, None, None
            while j < nb:
                if bull:
                    if L[i, j] <= stop: ex, why = stop, "stop"; break
                    if H[i, j] >= tgt: ex, why = tgt, "target"; break
                    if mac[i, j] < sig[i, j] and mac[i, j - 1] >= sig[i, j - 1]: ex, why = C[i, j], "macd"; break
                else:
                    if H[i, j] >= stop: ex, why = stop, "stop"; break
                    if L[i, j] <= tgt: ex, why = tgt, "target"; break
                    if mac[i, j] > sig[i, j] and mac[i, j - 1] <= sig[i, j - 1]: ex, why = C[i, j], "macd"; break
                if (j - k) * 5 >= 45 or j >= 70: ex, why = C[i, j], "time"; break
                j += 1
            if ex is None: ex, why, j = C[i, nb - 1], "eod", nb - 1
            out.append((i, k, j, e, ex, why)); n += 1; k = j + 1
    return out

def price_structs(trades, vixp, bull, cost_leg):
    rows = []
    for i, k, j, e, x, why in trades:
        if np.isnan(vixp[i]): continue
        sdd = vixp[i] / 100 / math.sqrt(252)
        s0, s1 = sd_left(sdd, k) * e, sd_left(sdd, j) * x
        sg0, sg1 = s0 / e, s1 / x
        und = (x / e - 1) * (1 if bull else -1) * 1e4 - 2.0     # bp after 2bp underlying cost
        r = {"i": i, "und_bp": und, "why": why}
        if bull:
            kp = math.floor(e) if e != math.floor(e) else e - 1
            cr0 = put(e, kp, sg0) - put(e, kp - 2, sg0); cr1 = put(x, kp, sg1) - put(x, kp - 2, sg1)
            pnl = (cr0 - cr1 - 4 * cost_leg); risk = 2 - cr0 + 2 * cost_leg
            r["C: $2 bull-put credit"] = pnl / risk
            ka = round(e); p0, p1 = call(e, ka, sg0), call(x, ka, sg1)
            r["ATM long call"] = (p1 - p0 - 2 * cost_leg) / (p0 + cost_leg)
            ko = math.floor(e) + 1; p0, p1 = call(e, ko, sg0), call(x, ko, sg1)
            r["1-OTM long call"] = (p1 - p0 - 2 * cost_leg) / (p0 + cost_leg)
            d0 = call(e, ka, sg0) - call(e, ka + 2, sg0); d1 = call(x, ka, sg1) - call(x, ka + 2, sg1)
            r["$2 call debit spread"] = (d1 - d0 - 4 * cost_leg) / (d0 + 2 * cost_leg)
        else:
            ka = round(e); p0, p1 = put(e, ka, sg0), put(x, ka, sg1)
            r["ATM long put"] = (p1 - p0 - 2 * cost_leg) / (p0 + cost_leg)
            ko = math.ceil(e) - 1; p0, p1 = put(e, ko, sg0), put(x, ko, sg1)
            r["1-OTM long put"] = (p1 - p0 - 2 * cost_leg) / (p0 + cost_leg)
            d0 = put(e, ka, sg0) - put(e, ka - 2, sg0); d1 = put(x, ka, sg1) - put(x, ka - 2, sg1)
            r["$2 put debit spread"] = (d1 - d0 - 4 * cost_leg) / (d0 + 2 * cost_leg)
            kc = math.ceil(e) if e != math.ceil(e) else e + 1
            cr0 = call(e, kc, sg0) - call(e, kc + 2, sg0); cr1 = call(x, kc, sg1) - call(x, kc + 2, sg1)
            r["$2 bear-call credit"] = (cr0 - cr1 - 4 * cost_leg) / (2 - cr0 + 2 * cost_leg)
        rows.append(r)
    return pd.DataFrame(rows)

def condor_like(M, vixp, kind, cost_leg, quiet=False, ind=None):
    """kind='fly' = Strategy B (iron fly 09:45 ET entry, wings +-$5, TP 50% credit, stop 1x credit, close 15:30 ET)
       kind='condor' = Strategy D (iron condor 10:00 ET, shorts at +-0.9 x remaining expected move, $2 wings,
       TP 50%, stop when debit = 2x credit, close 15:25 ET)."""
    O, H, L, C = M["O"], M["H"], M["L"], M["C"]
    nd, nb = C.shape
    out = []
    for i in range(nd):
        if np.isnan(vixp[i]): continue
        sdd = vixp[i] / 100 / math.sqrt(252)
        k0 = 2 if kind == "fly" else 5         # bar closing 9:45 / 10:00 ET
        S = C[i, k0]; sg = sd_left(sdd, k0)
        if quiet and ind is not None:
            rng = (H[i, :12].max() - L[i, :12].min()) / S
            if i < 15: continue
            past = [(H[t, :12].max() - L[t, :12].min()) / C[t, 11] for t in range(i - 14, i)]
            if rng >= np.median(past) or abs(S / ind[5][i, k0] - 1) > 0.0012: continue
        if kind == "fly":
            kc = kp = round(S); wc, wp = kc + 5, kp - 5
        else:
            em = sg * S * 0.9
            kc, kp = math.ceil(S + em), math.floor(S - em); wc, wp = kc + 2, kp - 2
        def val(s, sgv):
            return (call(s, kc, sgv) - call(s, wc, sgv)) + (put(s, kp, sgv) - put(s, wp, sgv))
        cr = val(S, sg) - 4 * cost_leg
        width = (wc - kc) if kind == "condor" else 5
        risk = width - cr
        end = 71 if kind == "fly" else 70        # bar closing 15:30 / 15:25 ET
        pnl, why = None, "time"
        for j in range(k0 + 1, end + 1):
            s = C[i, j]; v = val(s, sd_left(sdd, j)) + 4 * cost_leg
            if v <= cr * 0.5: pnl, why = cr - v, "take 50%"; break
            if (kind == "fly" and v - cr >= cr) or (kind == "condor" and v >= 2 * cr): pnl, why = cr - v, "stop"; break
        if pnl is None:
            v = val(C[i, end], sd_left(sdd, end)) + 4 * cost_leg; pnl = cr - v
        out.append({"i": i, "ret": pnl / risk, "usd": pnl * 100, "why": why, "credit": cr * 100, "risk": risk * 100})
    return pd.DataFrame(out)

def summ(x):
    x = pd.Series(x).dropna()
    if len(x) < 3: return {"n": len(x)}
    w, l = x[x > 0].sum(), -x[x < 0].sum()
    return {"n": len(x), "avg": x.mean() * 100, "win": (x > 0).mean() * 100, "pf": w / l if l > 0 else float("inf"),
            "t": x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))}

def fmt(s): return f"n={s['n']:4d} avg {s['avg']:+6.2f}% win {s['win']:4.1f}% PF {s['pf']:4.2f} t {s['t']:+5.2f}" if s.get("n", 0) >= 3 else f"n={s.get('n',0)}"

if __name__ == "__main__":
    data = HERE / "data"
    (d1, M1), (d2, M2), vix = load(data)
    sets = [("2005-14", d1, M1, lambda d: d.year <= 2014), ("2015-20", d1, M1, lambda d: d.year >= 2015), ("2025-26 SPY", d2, M2, lambda d: True)]
    cache = {}
    res = {}
    for cost_name, cl, mr in (("1c half-spread, IV 0.80", 0.01 + FEE, 0.80), ("2c taker, IV 0.80", 0.02 + FEE, 0.80),
                              ("1c half-spread, IV 0.60 (little VRP)", 0.01 + FEE, 0.60)):
        M_RTH = mr
        print(f"\n######## option cost per leg per side: {cost_name}")
        for bull in (True, False):
            print(f"\n=== Strategy C {'BULLISH ORB (calls / bull-put)' if bull else 'BEARISH mirror (puts)'} ===")
            for name, days, M, sel in sets:
                key = (name, bull)
                if key not in cache:
                    ind = cache.get((name, "ind")) or indicators(M); cache[(name, "ind")] = ind
                    cache[key] = orb_trades(M, ind, bull)
                tr = [t for t in cache[key] if sel(days[t[0]])]
                df = price_structs(tr, prev_vix(vix, days), bull, cl)
                if df.empty: print(f"  {name}: no trades"); continue
                print(f"  {name}: underlying {fmt(summ(df.und_bp / 1e4))}  (avg {df.und_bp.mean():+.2f} bp)")
                for col in [c for c in df.columns if c not in ("i", "und_bp", "why")]:
                    s = summ(df[col]); print(f"     {col:26s} {fmt(s)}"); res[f"{cost_name}|{'bull' if bull else 'bear'}|{name}|{col}"] = s
        print("\n=== Strategy B iron fly (09:45 ET, wings ±$5, TP 50%, stop 1x credit, 15:30 ET) and D iron condor (10:00 ET, ±0.9 EM, $2 wings, TP 50%, stop 2x, 15:25 ET) ===")
        for name, days, M, sel in sets:
            vp = prev_vix(vix, days)
            for kind, quiet in (("fly", False), ("condor", False), ("condor", True)):
                df = condor_like(M, vp, kind, cl, quiet, cache.get((name, "ind")) or indicators(M))
                df = df[[sel(days[i]) for i in df.i]]
                s = summ(df.ret); lab = {"fly": "B iron fly", "condor": "D iron condor"}[kind] + (" (quiet filter)" if quiet else "")
                print(f"  {name:11s} {lab:28s} {fmt(s)}  avg ${df.usd.mean():+6.1f}/1-lot  worst ${df.usd.min():+6.0f}  avg credit ${df.credit.mean():.0f} risk ${df.risk.mean():.0f}")
                res[f"{cost_name}|{lab}|{name}"] = {**s, "avg_usd": float(df.usd.mean()), "worst_usd": float(df.usd.min())}
    (HERE / "strategies_bcd_results.json").write_text(json.dumps(res, indent=1, default=float))
