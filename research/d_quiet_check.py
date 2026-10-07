"""D quiet filter: original (first-hour range, which ends 30 min after the 10:00 ET entry) vs an
implementable version (range known at entry: 9:30-10:00 ET, vs trailing 14-day median of the same window).

    python research/d_quiet_check.py      # needs research/data (fetch_data.sh + load_oanda.py), runs on the Mac

Prints the table and writes research/d_quiet_check_results.json ({cost case: {period: {filter: stats}}}; stats are
strategies_bcd.summ's n/avg %/win %/PF/t of the return on risk, plus avg_usd and worst_usd per 1 lot). CLAUDE.md's
book D numbers (+7.5% / +5.7% / +12.0% at 1c, ...) are the "quiet, 9:30-10:00 (known at entry)" rows."""
import sys, math, json
from pathlib import Path
R = Path(__file__).resolve().parent
sys.path.insert(0, str(R)); sys.path.insert(0, str(R.parent))
import numpy as np, pandas as pd
import strategies_bcd as sb

def condor(M, vixp, cost_leg, filt, vwap, nbars):
    O, H, L, C = M["O"], M["H"], M["L"], M["C"]
    nd, nb = C.shape; out = []
    for i in range(nd):
        if np.isnan(vixp[i]): continue
        sdd = vixp[i] / 100 / math.sqrt(252); k0 = 5
        S = C[i, k0]; sg = sb.sd_left(sdd, k0)
        if filt:
            if i < 15: continue
            rng = (H[i, :nbars].max() - L[i, :nbars].min()) / S
            past = [(H[t, :nbars].max() - L[t, :nbars].min()) / C[t, nbars - 1] for t in range(i - 14, i)]
            if rng >= np.median(past) or abs(S / vwap[i, k0] - 1) > 0.0012: continue
        em = sg * S * 0.9
        kc, kp = math.ceil(S + em), math.floor(S - em); wc, wp = kc + 2, kp - 2
        val = lambda s, g: (sb.call(s, kc, g) - sb.call(s, wc, g)) + (sb.put(s, kp, g) - sb.put(s, wp, g))
        cr = val(S, sg) - 4 * cost_leg; risk = 2 - cr; pnl = None
        for j in range(k0 + 1, 71):
            v = val(C[i, j], sb.sd_left(sdd, j)) + 4 * cost_leg
            if v <= cr * 0.5 or v >= 2 * cr: pnl = cr - v; break
        if pnl is None: pnl = cr - (val(C[i, 70], sb.sd_left(sdd, 70)) + 4 * cost_leg)
        out.append({"i": i, "ret": pnl / risk, "usd": pnl * 100})
    return pd.DataFrame(out)

(d1, M1), (d2, M2), vix = sb.load(R / "data")
sets = [("2005-14", d1, M1, lambda d: d.year <= 2014), ("2015-20", d1, M1, lambda d: d.year >= 2015), ("2025-26", d2, M2, lambda d: True)]
vw = {}
results = {}
for name, days, M, _ in sets:
    if id(M) not in vw: vw[id(M)] = sb.indicators(M)[5]
for cname, cl, mr in (("1c, IV 0.80", 0.01 + sb.FEE, 0.80), ("2c taker, IV 0.80", 0.02 + sb.FEE, 0.80), ("1c, IV 0.60", 0.01 + sb.FEE, 0.60)):
    sb.M_RTH = mr
    print(f"\n### {cname}")
    for name, days, M, sel in sets:
        vp = sb.prev_vix(vix, days)
        for lab, filt, nbars in (("no filter", False, 12), ("quiet, 1st hour (as backtested)", True, 12), ("quiet, 9:30-10:00 (known at entry)", True, 6)):
            df = condor(M, vp, cl, filt, vw[id(M)], nbars)
            df = df[[sel(days[i]) for i in df.i]]
            s = sb.summ(df.ret)
            print(f"  {name:8s} {lab:36s} {sb.fmt(s)}  avg ${df.usd.mean():+5.1f}  worst ${df.usd.min():+5.0f}")
            results.setdefault(cname, {}).setdefault(name, {})[lab] = {
                **{k: float(v) for k, v in s.items()}, "avg_usd": float(df.usd.mean()) if len(df) else None,
                "worst_usd": float(df.usd.min()) if len(df) else None}
(R / "d_quiet_check_results.json").write_text(json.dumps(results, indent=1))
print(f"-> {R / 'd_quiet_check_results.json'}")
