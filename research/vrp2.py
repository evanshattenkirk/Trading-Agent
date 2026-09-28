import math, numpy as np, pandas as pd, sys
sys.path.insert(0, ".")
from vrp_spreads import load, vrp, tstat, bs_call
(d1, M1, _, _), (d2, M2, _, _) = load()
def bs_put(S, K, sd): return bs_call(S, K, sd) - S + K
print("premium multiple m: straddle priced at m x VIX-implied 1-day sd.  m=0.80 prices the whole day's variance, m=0.69 only the ~75% that lands in RTH")
print(f"{'period':12s} {'m':>5s} {'straddle bp/day':>16s} {'t':>6s} {'win':>6s} | {'iron fly (wings ±1sd) bp/day':>29s} {'t':>6s} {'worst':>7s}")
for label, days, M in (("2005-2014", d1, M1), ("2015-2020", d1, M1), ("2025-26 SPY", d2, M2)):
    df = vrp(days, M); yrs = np.array([d.year for d in df.day])
    df = df[yrs <= 2014] if label == "2005-2014" else df[yrs >= 2015] if label == "2015-2020" else df
    for m in (0.80, 0.69):
        prem = m * df.sd
        sd_rth = m / 0.798 * df.sd           # BS sd consistent with that straddle price
        wing = np.array([bs_call(1, 1 + s, s) + bs_put(1, 1 - s, s) for s in sd_rth])   # wings at +/-1sd
        sd = df.sd.values
        straddle = (prem - df.absmove) * 1e4
        fly = (prem - wing - np.minimum(df.absmove, sd_rth)) * 1e4 - 4 * 0.5     # 0.5bp per leg friction
        print(f"{label:12s} {m:5.2f} {straddle.mean():+16.1f} {tstat(straddle):6.2f} {100*(straddle>0).mean():5.1f}% | {fly.mean():+29.1f} {tstat(fly):6.2f} {fly.min():7.0f}")
