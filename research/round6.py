"""Round 6 replays (research/round6_prereg.md): PM1-PM5 on Kalshi history pulled by research/round6_fetch.py.

    .venv/bin/python research/round6.py btc-wanted   # PM3/PM4 markets to pull candles for (needs coinbase + markets)
    .venv/bin/python research/round6.py report       # research/round6_results.json + printed tables

Rules are frozen in the pre-registration; this file only implements them. Kalshi prices are in dollars (0-1).
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from round5_k1 import _read_json_gz, norm_candles  # noqa: E402
from round6_fetch import DATA, SAMPLE_END, btc_close, btc_range_event, prev_weekday, ts  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# round 5's XSP 0DTE pull: data/thetadata/xsp_0dte on the Mac, the project's shared folder in a cloud session
XSP = Path(os.environ.get("ROUND6_XSP") or next(
    (p for p in (ROOT / "data" / "thetadata" / "xsp_0dte", Path("/mnt/project-files/data/round5/xsp_0dte")) if p.exists()),
    ROOT / "data" / "thetadata" / "xsp_0dte"))
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
T_BAR = 2.58
FEE_RATE = 0.07
EDGE = 0.03


# ------------------------------------------------------------------ common
def fee(p: float, rate: float = FEE_RATE) -> float:
    """Taker fee per contract on a 100-contract order, rounded up to the whole cent per order."""
    return math.ceil(round(100 * rate * p * (1 - p) * 100, 9)) / 10_000


def ret(payout: float, p: float, f: float) -> float:
    return (payout - p - f) / (p + f)


def buyable(p) -> bool:
    return p is not None and 0.02 <= p <= 0.98


def quote_at(candles: list[dict], e: int) -> tuple[float, float] | None:
    """(yes bid, yes ask) from the candle ending at e, else the latest ending in (e - 300, e] with both."""
    best = None
    for c in candles:
        if e - 300 < c["end"] <= e and c["bid"] is not None and c["ask"] is not None:
            if best is None or c["end"] > best["end"]:
                best = c
    return None if best is None else (best["bid"], best["ask"])


def side_trade(fair: float, bid: float, ask: float, payout_yes: int, edge: float = EDGE, rate: float = FEE_RATE):
    """PM2/PM3/PM5 rule: buy the side with the larger edge if it is >= edge and buyable. Returns (side, p, ret) or None."""
    cy, cn = ask, 1 - bid
    ey = fair - cy - fee(cy, rate) if buyable(cy) else -9
    en = (1 - fair) - cn - fee(cn, rate) if buyable(cn) else -9
    if max(ey, en) < edge:
        return None
    if ey >= en:
        return ("yes", cy, ret(payout_yes, cy, fee(cy, rate)))
    return ("no", cn, ret(1 - payout_yes, cn, fee(cn, rate)))


def maker_ret(side: str, bid: float, ask: float, payout_yes: int) -> float | None:
    """Reported only: the same side bought at its bid (yes bid, or 1 - yes ask for NO), no fee."""
    p = bid if side == "yes" else 1 - ask
    if not buyable(p):
        return None
    pay = payout_yes if side == "yes" else 1 - payout_yes
    return (pay - p) / p


def tstat(x) -> float:
    x = np.asarray(x, float)
    if len(x) < 3 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x))))


def summarize(rows: list[dict], key: str = "ret") -> dict:
    """rows: one per trade with 'event', 'close' and key; averaged within an event, t over events, halves by time."""
    if not rows:
        return {"events": 0, "trades": 0}
    df = pd.DataFrame(rows).dropna(subset=[key])
    if df.empty:
        return {"events": 0, "trades": 0}
    ev = df.groupby("event").agg(r=(key, "mean"), close=("close", "first")).sort_values("close")
    n = len(ev)
    h1, h2 = ev["r"].iloc[: n // 2], ev["r"].iloc[n // 2:]
    return {"events": n, "trades": int(len(df)), "mean": float(ev["r"].mean()), "t": tstat(ev["r"]),
            "half1_mean": float(h1.mean()) if len(h1) else None, "half2_mean": float(h2.mean()) if len(h2) else None,
            "half1_t": tstat(h1), "half2_t": tstat(h2),
            "first": datetime.fromtimestamp(ev["close"].iloc[0], ET).strftime("%Y-%m-%d"),
            "last": datetime.fromtimestamp(ev["close"].iloc[-1], ET).strftime("%Y-%m-%d"),
            "win_rate": float((df[key] > 0).mean()), "mean_price": float(df["p"].mean()) if "p" in df else None}


def verdict(s: dict, extra: bool = True) -> str:
    ok = (s.get("events", 0) > 0 and s["t"] >= T_BAR and (s["half1_mean"] or -1) > 0 and (s["half2_mean"] or -1) > 0
          and extra)
    return "PASS" if ok else "FAIL"


def candles_of(series: str, ticker: str) -> list[dict] | None:
    f = DATA / "candles" / series / f"{ticker}.json.gz"
    return norm_candles(_read_json_gz(f)) if f.exists() else None


def markets_of(series: str, event: str) -> list[dict]:
    f = DATA / "markets" / series / f"{event}.json.gz"
    return _read_json_gz(f) if f.exists() else []


def settled(m: dict) -> bool:
    return m.get("result") in ("yes", "no") and m.get("close_time") and ts(m["close_time"]) <= SAMPLE_END


def num(x):
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def kind(m: dict) -> tuple[str, float | None, float | None] | None:
    """(strike_type, floor, cap) from the market's fields; None when they can't be read."""
    st, lo, hi = m.get("strike_type"), num(m.get("floor_strike")), num(m.get("cap_strike"))
    if st == "between" and lo is not None and hi is not None:
        return ("between", lo, hi)
    if st == "greater" and lo is not None:
        return ("greater", lo, None)
    if st == "less" and hi is not None:
        return ("less", None, hi)
    return None


# ------------------------------------------------------------------ PM1 / PM4: modal bracket
def modal_trades(series, events, entry_fn, pick_fn=None, neighbours=False, rate=FEE_RATE, counts=None):
    rows = []
    for ev in events:
        ms = [m for m in markets_of(series, ev) if settled(m) and (k := kind(m)) and k[0] == "between"]
        if pick_fn:
            ms = pick_fn(ev, ms)
        if len(ms) < 5 and not pick_fn:
            counts["few_between"] += 1
            continue
        if not ms:
            counts["no_markets"] += 1
            continue
        e = entry_fn(ms[0])
        if e is None:
            counts["no_entry"] += 1
            continue
        quoted = []
        for m in ms:
            c = candles_of(series, m["ticker"])
            q = quote_at(c, e) if c else None
            if q:
                quoted.append((m, q))
            elif c is None:
                counts["no_candle_file"] += 1
        if not quoted:
            counts["no_quotes"] += 1
            continue
        quoted.sort(key=lambda x: num(x[0]["floor_strike"]))
        mids = [(q[0] + q[1]) / 2 for _, q in quoted]
        best = max(range(len(quoted)), key=lambda i: (mids[i], -i))
        picks = [best] + ([best - 1, best + 1] if neighbours else [])
        close = ts(quoted[0][0]["close_time"])
        for i in picks:
            if not 0 <= i < len(quoted):
                continue
            m, (bid, ask) = quoted[i]
            if not buyable(ask):
                counts["not_buyable"] += 1
                continue
            pay = 1 if m["result"] == "yes" else 0
            rows.append({"event": ev, "close": close, "p": ask, "ret": ret(pay, ask, fee(ask, rate)),
                         "maker": maker_ret("yes", bid, ask, pay), "win": pay})
    return rows


def index_events(series: str) -> list[str]:
    return sorted(f.name[:-8] for f in (DATA / "markets" / series).glob("*.json.gz"))


def at_et(hh, mm):
    def f(m):
        d = datetime.fromtimestamp(ts(m["close_time"]), ET).date()
        return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).timestamp())
    return f


def is_daily_close_1600(m) -> bool:
    c = datetime.fromtimestamp(ts(m["close_time"]), ET)
    return (c.hour, c.minute) == (16, 0)


def pm1(series="KXINX", hh=12, mm=0, neighbours=False, rate=FEE_RATE):
    counts = defaultdict(int)
    evs = []
    for ev in index_events(series):
        ms = [m for m in markets_of(series, ev) if settled(m)]
        if ms and is_daily_close_1600(ms[0]):
            evs.append(ev)
        else:
            counts["not_1600_close"] += 1
    rows = modal_trades(series, evs, at_et(hh, mm), neighbours=neighbours, rate=rate, counts=counts)
    return rows, dict(counts)


# ------------------------------------------------------------------ PM2: XSP option-implied digitals
def xsp_quotes(day: date, hh: int, mm: int) -> pd.DataFrame | None:
    f = XSP / f"{day.isoformat()}.parquet"
    if not f.exists():
        return None
    d = pd.read_parquet(f, columns=["strike", "right", "timestamp", "bid", "ask"])
    t = pd.Timestamp(datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET))
    d = d[d["timestamp"] == t]
    return d if len(d) else None


def digital_curve(q: pd.DataFrame):
    """Points (x, P(S >= x)) from adjacent-strike verticals; calls above spot, puts at or below. None if no spot."""
    q = q[(q["bid"] > 0) & (q["ask"] > q["bid"])].copy()
    q["mid"] = (q["bid"] + q["ask"]) / 2
    c = q[q["right"] == "CALL"].set_index("strike")["mid"]
    p = q[q["right"] == "PUT"].set_index("strike")["mid"]
    both = c.index.intersection(p.index)
    if len(both) == 0:
        return None
    k_atm = min(both, key=lambda k: abs(c[k] - p[k]))
    spot = k_atm + c[k_atm] - p[k_atm]
    listed = sorted(set(q["strike"]))
    pts = []
    for k1, k2 in zip(listed[:-1], listed[1:]):
        m = (k1 + k2) / 2
        if m > spot:
            if k1 in c.index and k2 in c.index:
                pts.append((m, (c[k1] - c[k2]) / (k2 - k1)))
        else:
            if k1 in p.index and k2 in p.index:
                pts.append((m, 1 - (p[k2] - p[k1]) / (k2 - k1)))
    if len(pts) < 3:
        return None
    pts.sort()
    xs, ys, run = [], [], 1.0
    for x, y in pts:
        y = min(max(y, 0.0), 1.0)
        run = min(run, y)
        xs.append(x)
        ys.append(run)
    return np.array(xs), np.array(ys), spot


def D(curve, x):
    xs, ys, _ = curve
    if x < xs[0] or x > xs[-1]:
        return None
    return float(np.interp(x, xs, ys))


def fair_index(curve, k, scale=10.0):
    st, lo, hi = k
    if st == "between":
        a, b = D(curve, lo / scale), D(curve, hi / scale)
        return None if a is None or b is None else max(0.0, a - b)
    if st == "greater":
        return D(curve, lo / scale)
    a = D(curve, hi / scale)
    return None if a is None else 1 - a


def pm2(hh=12, mm=0, edge=EDGE, rate=FEE_RATE):
    counts = defaultdict(int)
    rows = []
    for ev in index_events("KXINX"):
        ms = [m for m in markets_of("KXINX", ev) if settled(m)]
        if not ms or not is_daily_close_1600(ms[0]):
            continue
        if sum(1 for m in ms if (k := kind(m)) and k[0] == "between") < 5:
            counts["few_between"] += 1
            continue
        day = datetime.fromtimestamp(ts(ms[0]["close_time"]), ET).date()
        tq = datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET) - timedelta(minutes=1)
        q = xsp_quotes(day, tq.hour, tq.minute)
        if q is None:
            counts["no_xsp"] += 1
            continue
        curve = digital_curve(q)
        if curve is None:
            counts["no_curve"] += 1
            continue
        e = int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET).timestamp())
        for m in ms:
            k = kind(m)
            if k is None:
                counts["unreadable_strike"] += 1
                continue
            fair = fair_index(curve, k)
            if fair is None:
                counts["outside_curve"] += 1
                continue
            c = candles_of("KXINX", m["ticker"])
            qq = quote_at(c, e) if c else None
            if not qq:
                counts["no_quote"] += 1
                continue
            bid, ask = qq
            pay = 1 if m["result"] == "yes" else 0
            t = side_trade(fair, bid, ask, pay, edge, rate)
            if t:
                rows.append({"event": ev, "close": ts(m["close_time"]), "p": t[1], "ret": t[2], "side": t[0],
                             "fair": fair, "maker": maker_ret(t[0], bid, ask, pay)})
    return rows, dict(counts)


# ------------------------------------------------------------------ Bitcoin: Coinbase minutes
_CB = None


def coinbase() -> pd.Series:
    """BTC-USD 1-minute closes indexed by the minute's END (epoch seconds)."""
    global _CB
    if _CB is None:
        recs = {}
        for f in (DATA / "coinbase").glob("*.json.gz"):
            for t, lo, hi, op, cl, v in _read_json_gz(f):
                recs[int(t) + 60] = float(cl)
        _CB = pd.Series(recs).sort_index()
    return _CB


def spot_sigma(e: int, n: int = 360) -> tuple[float, float] | None:
    cb = coinbase()
    if e not in cb.index:
        return None
    w = cb.loc[e - n * 60: e]
    w = w[w.index % 60 == 0]
    if len(w) < 0.8 * (n + 1):
        return None
    r = np.diff(np.log(w.values))
    # a gap of missing minutes makes one longer return; scale each by its length to keep a per-minute variance
    dt = np.diff(w.index.values) / 60
    var = float(np.mean(r ** 2 / dt) - (np.mean(r / np.sqrt(dt))) ** 2)
    return float(cb[e]), math.sqrt(max(var, 1e-12))


def btc_events() -> list[str]:
    return json.loads((DATA / "btc_sample.json").read_text())["events"]


def nearest(ms: list[dict], s: float, key, n: int = 6) -> list[dict]:
    return sorted(ms, key=lambda m: abs(key(m) - s))[:n]


def btc_wanted() -> dict:
    """PM3/PM4 markets to fetch: six nearest at each of the three entry offsets (union)."""
    want = {"KXBTCD": [], "KXBTC": []}
    for ev in btc_events():
        for series, evt, typ, key in (("KXBTCD", ev, "greater", lambda m: num(m["floor_strike"])),
                                       ("KXBTC", btc_range_event(ev), "between",
                                        lambda m: (num(m["floor_strike"]) + num(m["cap_strike"])) / 2)):
            ms = [m for m in markets_of(series, evt) if settled(m) and (k := kind(m)) and k[0] == typ]
            if not ms:
                continue
            close = ts(ms[0]["close_time"])
            sel = set()
            for off in (45, 15, 5):
                ss = spot_sigma(close - off * 60, 60)
                if ss:
                    sel |= {m["ticker"] for m in nearest(ms, ss[0], key)}
            want[series] += sorted(sel)
    return want


def pm3(off=15, n_sigma=360, edge=EDGE, rate=FEE_RATE):
    counts = defaultdict(int)
    rows = []
    for ev in btc_events():
        ms = [m for m in markets_of("KXBTCD", ev) if settled(m) and (k := kind(m)) and k[0] == "greater"]
        if not ms:
            counts["no_markets"] += 1
            continue
        close = ts(ms[0]["close_time"])
        e = close - off * 60
        ss = spot_sigma(e, n_sigma)
        if not ss:
            counts["no_spot"] += 1
            continue
        s, sig = ss
        for m in nearest(ms, s, lambda m: num(m["floor_strike"])):
            K = num(m["floor_strike"])
            fair = 0.5 * math.erfc(-(math.log(s / K) / (sig * math.sqrt(off))) / math.sqrt(2))
            c = candles_of("KXBTCD", m["ticker"])
            q = quote_at(c, e) if c else None
            if not q:
                counts["no_quote"] += 1
                continue
            bid, ask = q
            pay = 1 if m["result"] == "yes" else 0
            t = side_trade(fair, bid, ask, pay, edge, rate)
            if t:
                rows.append({"event": ev, "close": close, "p": t[1], "ret": t[2], "side": t[0], "fair": fair,
                             "maker": maker_ret(t[0], bid, ask, pay)})
    return rows, dict(counts)


def pm4(off=15, neighbours=False, rate=FEE_RATE):
    counts = defaultdict(int)

    def pick(ev, ms):
        if not ms:
            return []
        ss = spot_sigma(ts(ms[0]["close_time"]) - off * 60, 60)
        if not ss:
            counts["no_spot"] += 1
            return []
        return nearest(ms, ss[0], lambda m: (num(m["floor_strike"]) + num(m["cap_strike"])) / 2)

    evs = [btc_range_event(e) for e in btc_events()]
    rows = modal_trades("KXBTC", evs, lambda m: ts(m["close_time"]) - off * 60, pick_fn=pick,
                        neighbours=neighbours, rate=rate, counts=counts)
    return rows, dict(counts)


# ------------------------------------------------------------------ PM5: CPI vs Cleveland Fed nowcast
def nowcast_table() -> tuple[dict, dict]:
    """(nowcasts, actuals): nowcasts[(y, m)] = list of (date, value) for headline CPI m/m; actuals[(y, m)] =
    (release date, value)."""
    charts = json.load(gzip.open(DATA / "nowcast_month.json.gz"))
    now, act = {}, {}
    for ch in charts:
        y, mth = map(int, ch["chart"]["subcaption"].split("-"))
        labels = [c["label"] for c in ch["categories"][0]["category"] if not c.get("vline")]
        dates = []
        for lab in labels:
            mm, dd = map(int, lab.split("/"))
            yy = y + (1 if mm < mth else 0)
            dates.append(date(yy, mm, dd))
        ds = {d["seriesname"]: [num(x.get("value")) for x in d["data"]] for d in ch["dataset"]}
        cpi, a = ds.get("CPI Inflation", []), ds.get("Actual CPI Inflation", [])
        now[(y, mth)] = [(dates[i], v) for i, v in enumerate(cpi) if v is not None and i < len(dates)]
        rel = [(dates[i], v) for i, v in enumerate(a) if v is not None and i < len(dates)]
        if rel:
            act[(y, mth)] = rel[0]
    return now, act


def nowcast_on(series_pts, d: date):
    pts = [v for dd, v in series_pts if dd <= d]
    return pts[-1] if pts else None


MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def cpi_month(event: str) -> tuple[int, int] | None:
    try:
        suf = event.split("-", 1)[1]
        return 2000 + int(suf[:2]), MON[suf[2:5]]
    except (IndexError, KeyError, ValueError):
        return None


def cpi_kind(m: dict):
    """PM5 strike semantics: Kalshi's CPI markets are 'greater' (printed > floor) in the current format; legacy
    markets are read from their fields the same way, else skipped."""
    k = kind(m)
    if k:
        return k
    mt = re.fullmatch(r"Above (-?\d+(?:\.\d+)?)%", (m.get("yes_sub_title") or "").strip())
    if mt:      # legacy 2021-22 markets carry the strike only in the subtitle: "Above 0.4%"
        return ("greater", float(mt.group(1)), None)
    return None


def pm5(lead=1, edge=EDGE, rate=FEE_RATE):
    counts = defaultdict(int)
    now, act = nowcast_table()
    rows = []
    for ev in index_events("KXCPI"):
        ym = cpi_month(ev)
        ms = [m for m in markets_of("KXCPI", ev) if settled(m)]
        if not ym or not ms or ym not in now:
            counts["no_nowcast_or_markets"] += 1
            continue
        close = ts(ms[0]["close_time"])
        cday = datetime.fromtimestamp(close, ET).date()
        eday = prev_weekday(cday, lead)
        e = int(datetime(eday.year, eday.month, eday.day, 15, 0, tzinfo=ET).timestamp())
        n = nowcast_on(now[ym], eday)
        if n is None:
            counts["no_nowcast_value"] += 1
            continue
        errs = []
        for k2, (rday, a) in act.items():
            if rday >= eday or k2 not in now:
                continue
            nv = nowcast_on(now[k2], prev_weekday(rday, lead))
            if nv is not None:
                errs.append(a - nv)
        if len(errs) < 24:
            counts["short_history"] += 1
            continue
        sig = math.sqrt(float(np.mean(np.square(errs))))
        Phi = lambda z: 0.5 * math.erfc(-z / math.sqrt(2))  # noqa: E731
        for m in ms:
            k = cpi_kind(m)
            if k is None:
                counts["unreadable_strike"] += 1
                continue
            st, lo, hi = k
            if st == "greater":
                fair = 1 - Phi((lo + 0.05 - n) / sig)
            elif st == "less":
                fair = Phi((hi - 0.05 - n) / sig)
            else:
                fair = Phi((hi + 0.05 - n) / sig) - Phi((lo - 0.05 - n) / sig)
            c = candles_of("KXCPI", m["ticker"])
            q = quote_at(c, e) if c else None
            if not q:
                counts["no_quote"] += 1
                continue
            bid, ask = q
            pay = 1 if m["result"] == "yes" else 0
            t = side_trade(fair, bid, ask, pay, edge, rate)
            if t:
                rows.append({"event": ev, "close": close, "p": t[1], "ret": t[2], "side": t[0], "fair": fair,
                             "nowcast": n, "sigma": sig, "maker": maker_ret(t[0], bid, ask, pay)})
    return rows, dict(counts)


# ------------------------------------------------------------------ report
def block(rows, counts):
    return {"taker": summarize(rows), "maker": summarize(rows, "maker"), "skips": counts}


def report() -> dict:
    out = {}
    r, c = pm1("KXINX")
    nq, cq = pm1("KXNASDAQ100")
    main = block(r, c)
    nqs = summarize(nq)
    out["PM1"] = {"primary": main, "nasdaq": block(nq, cq),
                  "verdict": verdict(main["taker"], nqs.get("events", 0) > 0 and nqs.get("mean", -1) > 0),
                  "sens": {f"E{h}": summarize(pm1("KXINX", h, 0)[0]) for h in (10, 14, 15)}
                  | {"neighbours": summarize(pm1("KXINX", neighbours=True)[0]),
                     "fee_0035": summarize(pm1("KXINX", rate=0.035)[0])}}
    r, c = pm2()
    b = block(r, c)
    out["PM2"] = {"primary": b, "verdict": verdict(b["taker"]),
                  "sens": {"edge_0.01": summarize(pm2(edge=0.01)[0]), "edge_0.05": summarize(pm2(edge=0.05)[0]),
                           "E14": summarize(pm2(14, 0)[0]), "fee_0035": summarize(pm2(rate=0.035)[0])}}
    r, c = pm3()
    b = block(r, c)
    out["PM3"] = {"primary": b, "verdict": verdict(b["taker"]),
                  "sens": {"E-5": summarize(pm3(5)[0]), "E-45": summarize(pm3(45)[0]),
                           "sig60": summarize(pm3(n_sigma=60)[0]), "sig1440": summarize(pm3(n_sigma=1440)[0]),
                           "edge_0.01": summarize(pm3(edge=0.01)[0]), "edge_0.05": summarize(pm3(edge=0.05)[0])}}
    r, c = pm4()
    b = block(r, c)
    out["PM4"] = {"primary": b, "verdict": verdict(b["taker"]),
                  "sens": {"E-45": summarize(pm4(45)[0]), "neighbours": summarize(pm4(neighbours=True)[0])}}
    r, c = pm5()
    b = block(r, c)
    out["PM5"] = {"primary": b, "verdict": verdict(b["taker"]),
                  "sens": {"edge_0.01": summarize(pm5(edge=0.01)[0]), "edge_0.05": summarize(pm5(edge=0.05)[0]),
                           "lead2": summarize(pm5(lead=2)[0])}}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["btc-wanted", "report"])
    a = ap.parse_args(argv)
    if a.step == "btc-wanted":
        w = btc_wanted()
        (DATA / "btc_wanted.json").write_text(json.dumps(w))
        print({k: len(v) for k, v in w.items()})
    else:
        out = report()
        (ROOT / "research" / "round6_results.json").write_text(json.dumps(out, indent=1, default=str))
        for k, v in out.items():
            p = v["primary"]["taker"]
            print(k, v["verdict"], {x: p.get(x) for x in ("events", "trades", "mean", "t", "half1_mean", "half2_mean")})


if __name__ == "__main__":
    main()
