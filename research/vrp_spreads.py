import math, sys, glob
import numpy as np, pandas as pd
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent)); sys.path.insert(0, ".")
from research import matrices
from agentdesk.indicators import MACD, RSI

vix = pd.read_csv("data/vix.csv", parse_dates=["DATE"]).set_index("DATE")
vix.index = vix.index.date

def load():
    spx = pd.read_pickle("data/spx_rth_1m.pkl")
    d1, M1, _ = matrices(spx, 1)
    f = "data/spy_5m_2025_2026.csv"
    spy = pd.read_csv(f)
    spy["et"] = pd.to_datetime(spy["Datetime"], utc=True).dt.tz_convert("America/New_York")
    spy["day"] = spy["et"].dt.date; spy["hm"] = spy["et"].dt.hour * 60 + spy["et"].dt.minute
    spy = spy.rename(columns=str.lower)
    d2, M2, _ = matrices(spy, 5)
    return (d1, M1, 1, 10.0), (d2, M2, 5, 1.0)     # scale: SPX/10 ~ SPY so strikes are $1 apart

# ---------------------------------------------------------------- 1. variance risk premium, open -> close
def vrp(days, M):
    O, C = M["O"][:, 0], M["C"][:, -1]
    rows = []
    vdates = sorted(vix.index)
    for i, d in enumerate(days):
        prev = [x for x in vdates if x < d]
        if not prev: continue
        v = vix.loc[prev[-1], "CLOSE"]
        sd = v / 100 / math.sqrt(252)
        rows.append((d, abs(C[i] / O[i] - 1), sd, v))
    df = pd.DataFrame(rows, columns=["day", "absmove", "sd", "vix"])
    df["ratio"] = df.absmove / df.sd
    df["short_straddle_bp"] = (0.798 * df.sd - df.absmove) * 1e4     # premium at E|Z| of the VIX-implied day
    return df

# ---------------------------------------------------------------- 2. structures on MACD-proxy signals
def bs_call(S, K, sd_left):
    if sd_left <= 1e-9: return max(S - K, 0.0)
    d1 = (math.log(S / K) + 0.5 * sd_left ** 2) / sd_left
    N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    return S * N(d1) - K * N(d1 - sd_left)

def signals(days, M, res):
    C = M["C"]; nd, nb = C.shape
    step5, step15 = max(1, 5 // res), 15 // res
    m5, r5, m15, r15 = MACD(), RSI(), MACD(), RSI()
    out = []
    for i in range(nd):
        prev5, open_sig = None, None
        for k in range(step5 - 1, nb, step5):
            c = C[i, k]
            v5, rv5 = m5.update(c), r5.update(c)
            if (k + 1) % step15 == 0:
                m15.update(c); r15.update(c); p15, pr15 = m15.value, r15.value
            else:
                p15, pr15 = m15.preview(c), r15.preview(c)
            mins = (k + 1) * res
            if open_sig is not None:
                if (prev5 and v5 and prev5.bull and not v5.bull) or mins >= 380:
                    out.append((i, open_sig, k)); open_sig = None
            elif v5 and prev5 and p15 and rv5 and pr15 and not prev5.bull and v5.bull and p15.bull \
                    and 30 < rv5 < 70 and 30 < pr15 < 70 and mins <= 330:
                open_sig = k
            prev5 = v5
    return out

def structures(days, M, res, scale, sigs):
    C = M["C"]; nb = C.shape[1]
    vdates = sorted(vix.index)
    rows = []
    for i, k0, k1 in sigs:
        prev = [x for x in vdates if x < days[i]]
        if not prev: continue
        sd_day = vix.loc[prev[-1], "CLOSE"] / 100 / math.sqrt(252)
        S0, S1 = C[i, k0] / scale, C[i, k1] / scale
        f0 = max(0.0, (nb - 1 - k0) / nb) + 15 / 390        # SPY 0DTE trades to 16:15
        f1 = max(0.0, (nb - 1 - k1) / nb) + 15 / 390
        sd0, sd1 = sd_day * math.sqrt(f0), sd_day * math.sqrt(f1)
        otm1 = math.floor(S0) + 1
        legs = {"Naked 1-OTM call": [(otm1, 1)],
                "Debit spread 1-OTM / 3-OTM ($2 wide)": [(otm1, 1), (otm1 + 2, -1)],
                "Debit spread ATM / 2-OTM ($2 wide)": [(round(S0), 1), (round(S0) + 2, -1)],
                "Debit spread 1-OTM / 2-OTM ($1 wide)": [(otm1, 1), (otm1 + 1, -1)]}
        for name, lg in legs.items():
            v0 = sum(q * bs_call(S0, K, sd0) for K, q in lg)
            v1 = sum(q * bs_call(S1, K, sd1) for K, q in lg)
            cost = 0.01 * 2 * len(lg) + 0.03 * 2 * len(lg) / 100      # 1c half-spread per leg per side + fees
            pnl = (v1 - v0 - cost) * 100                               # $ per 1 structure
            rows.append((name, days[i], (S1 / S0 - 1) * 1e4, (k1 - k0) * res, v0 * 100, pnl))
    return pd.DataFrame(rows, columns=["structure", "day", "spx_move_bp", "hold_min", "debit", "pnl"])

def tstat(x): return x.mean() / (x.std(ddof=1) / math.sqrt(len(x))) if len(x) > 2 else float("nan")

if __name__ == "__main__":
    (d1, M1, r1, s1), (d2, M2, r2, s2) = load()
    print("=== 1. Variance risk premium: short open->close ATM straddle priced at VIX-implied day (0.80 x sd) ===")
    for label, days, M in (("2005-2014", d1, M1), ("2015-2020", d1, M1), ("2025-26 SPY", d2, M2)):
        df = vrp(days, M)
        yrs = np.array([d.year for d in df.day])
        if label == "2005-2014": df = df[(yrs <= 2014)]
        elif label == "2015-2020": df = df[(yrs >= 2015)]
        x = df.short_straddle_bp
        print(f"{label:12s} days {len(df):4d}  realized/implied |move| ratio {df.ratio.mean():.2f} (break-even 0.80)  "
              f"short straddle {x.mean():+6.1f} bp/day  win {100*(x>0).mean():4.1f}%  t {tstat(x):5.2f}  worst day {x.min():7.0f} bp  "
              f"5% worst avg {x.nsmallest(max(1,len(x)//20)).mean():6.0f} bp")
    print("\n=== 2. Naked call vs debit spreads on your MACD entries (5m proxy), exit on 5m cross-back ===")
    for label, days, M, res, scale, sel in (("2015-2020", d1, M1, r1, s1, lambda d: d.year >= 2015),
                                            ("2005-2014", d1, M1, r1, s1, lambda d: d.year <= 2014),
                                            ("2025-26 SPY", d2, M2, r2, s2, lambda d: True)):
        sig = [s for s in signals(days, M, res) if sel(days[s[0]])]
        df = structures(days, M, res, scale, sig)
        print(f"\n{label}: {len(sig)} trades, median hold {df.hold_min.median():.0f} min")
        print(f"{'structure':38s} {'debit':>7s} {'avg $':>7s} {'% debit':>8s} {'win':>6s} {'t':>6s} | {'sideways':>9s} {'up>10bp':>8s} {'down>10bp':>9s}")
        for name, g in df.groupby("structure", sort=False):
            side = g[g.spx_move_bp.abs() <= 10].pnl.mean(); up = g[g.spx_move_bp > 10].pnl.mean(); dn = g[g.spx_move_bp < -10].pnl.mean()
            print(f"{name:38s} {g.debit.mean():7.0f} {g.pnl.mean():+7.1f} {100*g.pnl.sum()/g.debit.sum():+7.1f}% {100*(g.pnl>0).mean():5.1f}% {tstat(g.pnl):6.2f} | {side:+9.1f} {up:+8.1f} {dn:+9.1f}")
        if label == "2015-2020":
            b = df[df.structure == "Naked 1-OTM call"]
            print(f"  move buckets (share of trades): sideways {100*(b.spx_move_bp.abs()<=10).mean():.0f}%  up {100*(b.spx_move_bp>10).mean():.0f}%  down {100*(b.spx_move_bp<-10).mean():.0f}%")
