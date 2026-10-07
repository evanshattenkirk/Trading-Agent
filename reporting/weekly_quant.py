"""Weekly per-book Quant report (HANDOFF section 10, phase 6).

Reads the trade journal (read-only) and writes, per book: trades, net P&L as filled and after taker costs, win
rate, profit factor, t, worst day, max drawdown, fill quality vs mid, the L2 split, and the section 10.8
promotion gates (book E: the section 7E gate). Also the B/D go/no-go number from the recorded 0DTE quotes:
the opening ATM straddle vs the move that followed.

Statistics follow the data plugin's statistical-analysis method: mean and median together, bootstrap CI for
the mean (P&L is skewed), Wilson CI for win rate, IQR outliers counted but never removed, and a Bonferroni
note because five books are tested at once.

Taker costs: combo fills (books B-E) log the natural price at fill time, so taker P&L re-prices every fill
there. Single-leg fills (book A) log only the price, so the natural (ask to buy, bid to sell) comes from the
nearest recorded quote in journal.option_quotes within 15 s; fills with no nearby quote stay as filled and are
counted as unpriced.

Book A trades on the free IEX feed, which carries about 4% of SPY prints, so its paper results are shown as
"not evidence" until a SIP replay (research/iex_vs_sip.py output, --sip-dir) agrees with them: entry-signal
Jaccard >= 0.80 and same-sign net over at least 5 sessions. The SIP replay P&L sits next to the IEX paper P&L.

Journal schema: trades as written by the multi-book framework (PR #6: book, legs, max_loss columns). An older
journal with no book column is read as all book A. Only paper and shadow trades count; sim never does.

    python -m reporting.weekly_quant --journal ~/.agentdesk/journal.db --out reports [--sip-dir DIR]
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

BOOKS = {
    "A": "Evan's MACD 0DTE calls",
    "B": "Iron fly",
    "C": "ORB bull-put spread",
    "D": "10:00 ET iron condor",
    "E": "Pre-earnings IV run-up",
    "F1": "Stocks in play, long shares",
    "F2": "Single-name call/put debit spreads",
    "G": "SPY 0DTE/1DTE call calendar",
}
LEGACY_BOOKS = {"F": "F1"}     # book F became F1 on 2026-09-29

# Modeled per-trade return on risk, lowest and highest cell across periods, cost levels and IV 0.60.
# A and E have no option-level backtest to compare against.
BACKTEST_RANGE = {
    "B": {"range": (-0.073, 0.317), "src": "HANDOFF 7B table (2005-26, 1c/taker/IV 0.60)"},
    "C": {"range": (-0.071, -0.030), "src": "HANDOFF 7C $2 bull-put, 1c and 2c taker"},
    "D": {"range": (-0.037, 0.120), "src": "entry-time quiet filter, research/d_quiet_check.py (spec Q1)"},
}

QUOTE_MATCH_S = 15.0
BOOK_COUNT = len(BOOKS)                 # for the Bonferroni note
ALPHA = 0.05
ORDER_STATE_WORDS = ("halt", "mismatch", "order state", "orderstate", "watchdog", "stale", "ambiguous", "kill")


# ---------------------------------------------------------------- statistics

def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > 1e-30 else 1e-30)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-30 else 1e-30
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-30 else 1e-30
        de = d * c
        h *= de
        if abs(de - 1) < 1e-12:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1 - front * _betacf(b, a, 1 - x) / b


def t_pvalue(t: float, df: float) -> float:
    """Two-sided p-value of Student's t."""
    return _betainc(df / 2, 0.5, df / (df + t * t))


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    if n == 0:
        return (None, None)
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (mid - half, mid + half)


def _quantile(xs: list, q: float) -> float:
    s = sorted(xs)
    pos = (len(s) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def bootstrap_mean_ci(xs: list, reps: int = 2000, seed: int = 7) -> tuple:
    if len(xs) < 2:
        return (None, None)
    rng, n = random.Random(seed), len(xs)
    means = sorted(sum(rng.choice(xs) for _ in range(n)) / n for _ in range(reps))
    return (_quantile(means, 0.025), _quantile(means, 0.975))


def trade_stats(xs: list) -> dict:
    xs = [float(x) for x in xs]
    n = len(xs)
    if n == 0:
        return {"n": 0, "net": 0.0}
    wins = [x for x in xs if x > 0]
    losses = [x for x in xs if x < 0]
    mean = sum(xs) / n
    sd = statistics.stdev(xs) if n > 1 else None
    t = mean / (sd / math.sqrt(n)) if sd else None
    q1, q3 = _quantile(xs, 0.25), _quantile(xs, 0.75)
    iqr = q3 - q1
    return {
        "n": n, "net": sum(xs), "mean": mean, "median": statistics.median(xs), "sd": sd,
        "t": t, "p": t_pvalue(t, n - 1) if t is not None else None,
        "win_rate": len(wins) / n, "win_ci": wilson(len(wins), n),
        "pf": (sum(wins) / -sum(losses)) if losses else None,
        "p5": _quantile(xs, 0.05), "p95": _quantile(xs, 0.95),
        "mean_ci": bootstrap_mean_ci(xs),
        "outliers": sum(1 for x in xs if x < q1 - 1.5 * iqr or x > q3 + 1.5 * iqr),
        "best": max(xs), "worst": min(xs),
    }


def daily_stats(by_day: dict) -> dict:
    days = sorted(by_day)
    if not days:
        return {"sessions": 0, "worst_day": None, "max_drawdown": 0.0, "day_t": None}
    peak = cum = dd = 0.0
    for d in days:
        cum += by_day[d]
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    vals = [by_day[d] for d in days]
    sd = statistics.stdev(vals) if len(vals) > 1 else None
    worst = min(days, key=lambda d: by_day[d])
    return {"sessions": len(days), "worst_day": (worst, by_day[worst]), "max_drawdown": dd,
            "day_t": (sum(vals) / len(vals)) / (sd / math.sqrt(len(vals))) if sd else None}


def overlaps(ci: tuple, rng: tuple):
    if ci is None or ci[0] is None or rng is None:
        return None
    return ci[0] <= rng[1] and ci[1] >= rng[0]


def max_month_share(trades: list):
    total = sum(t["pnl_taker"] for t in trades)
    if total <= 0:
        return None
    months: dict = {}
    for t in trades:
        months[t["session"][:7]] = months.get(t["session"][:7], 0.0) + t["pnl_taker"]
    return max(months.values()) / total


# ---------------------------------------------------------------- journal access

def _connect(path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{Path(path).expanduser()}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    _with_archives(db, path)
    return db


def _with_archives(db, path) -> None:
    """Quotes tools/prune_journal.py moved to <journal dir>/archive/quotes-YYYY.db stay in the report: a temp view
    named option_quotes (SQLite looks in temp before main) adds them to the journal's own rows."""
    files = sorted((Path(path).expanduser().parent / "archive").glob("quotes-*.db"))[-9:]   # SQLite attaches <= 10
    cols = [r[1] for r in db.execute("PRAGMA main.table_info(option_quotes)")] if files else []
    if not cols:
        return
    names = ", ".join(cols)
    parts = [f"SELECT {names} FROM main.option_quotes"]
    for i, f in enumerate(files):
        try:
            db.execute(f"ATTACH DATABASE ? AS qa{i}", (f"file:{f}?mode=ro",))
        except sqlite3.Error:
            continue
        if set(cols) <= {r[1] for r in db.execute(f"PRAGMA qa{i}.table_info(option_quotes)")}:
            parts.append(f"SELECT {names} FROM qa{i}.option_quotes")
    if len(parts) > 1:
        db.execute("CREATE TEMP VIEW option_quotes AS " + " UNION ALL ".join(parts))


def _loads(s):
    if s in (None, ""):
        return None
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return None


def load_trades(path, modes=("paper", "shadow")) -> list[dict]:
    db = _connect(path)
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(trades)")}
        if not cols:
            return []
        marks = ",".join("?" * len(modes))
        rows = db.execute(f"SELECT * FROM trades WHERE mode IN ({marks}) ORDER BY closed_ts, id"
                          if "closed_ts" in cols else f"SELECT * FROM trades WHERE mode IN ({marks}) ORDER BY id",
                          tuple(modes)).fetchall()
    finally:
        db.close()
    out = []
    for r in rows:
        d = dict(r)
        b = (d.get("book") or "A").upper()
        d["book"] = LEGACY_BOOKS.get(b, b)
        d["fills"] = _loads(d.get("fills")) or []
        d["l2"] = _loads(d.get("l2"))
        d["crew"] = _loads(d.get("crew"))
        d["pnl"] = float(d.get("pnl") or 0.0)
        out.append(d)
    return out


def parse_occ(occ: str | None):
    """SPY261001C00765500 -> ("2026-10-01", 765.5, "call"). None for combos or anything else."""
    if not occ or "," in occ or len(occ) < 16:
        return None
    tail = occ[-15:]
    try:
        yy, mm, dd, cp, k = tail[0:2], tail[2:4], tail[4:6], tail[6], int(tail[7:])
    except ValueError:
        return None
    if cp not in "CP":
        return None
    return (f"20{yy}-{mm}-{dd}", k / 1000, "call" if cp == "C" else "put")


class QuoteBook:
    """Nearest recorded quote for a contract, from journal.option_quotes."""

    def __init__(self, path):
        self.path = path
        self._db = None

    def _conn(self):
        if self._db is None:
            self._db = _connect(self.path)
            if not self._db.execute("SELECT name FROM sqlite_master WHERE name='option_quotes'").fetchone():
                self._db = False
        return self._db

    def near(self, expiry: str, strike: float, right: str, ts: float, within: float = QUOTE_MATCH_S):
        db = self._conn()
        if not db:
            return None
        r = db.execute("SELECT bid, ask, ts FROM option_quotes WHERE expiry=? AND abs(strike-?)<1e-6 AND right=? "
                       "AND ts BETWEEN ? AND ? ORDER BY abs(ts-?) LIMIT 1",
                       (expiry, strike, right, ts - within, ts + within, ts)).fetchone()
        return (r["bid"], r["ask"]) if r else None


def fill_quality(trade: dict, quotes: QuoteBook | None) -> dict:
    """P&L after taker costs, and cents per share given up vs mid, summed over the trade's fills."""
    penalty, vs_mid, priced, unpriced = 0.0, 0.0, 0, 0
    leg = parse_occ(trade.get("occ"))
    for f in trade.get("fills") or []:
        px, qty = f.get("px"), f.get("qty") or 0
        if px is None:
            continue
        nat, mid = f.get("natural"), f.get("mid")
        if nat is None and leg and quotes is not None:
            q = quotes.near(*leg, f.get("ts") or 0)
            if q:
                bid, ask = q
                nat = ask if f.get("side") == "buy" else bid
                mid = (bid + ask) / 2
                # a single leg can't fill better than the touch in paper: only count what the fill gave up
                if f.get("side") == "buy":
                    penalty += max(0.0, nat - px) * 100 * qty
                else:
                    penalty += max(0.0, px - nat) * 100 * qty
                vs_mid += max(0.0, (px - mid) if f.get("side") == "buy" else (mid - px)) * 100
                priced += 1
                continue
        if nat is None:
            unpriced += 1
            continue
        penalty += abs(px - nat) * 100 * qty
        if mid is not None:
            vs_mid += abs(px - mid) * 100
        priced += 1
    return {"pnl_taker": trade["pnl"] - penalty, "vs_mid_c": vs_mid, "priced": priced, "unpriced": unpriced}


def _basis(t: dict):
    if t.get("max_loss"):
        return float(t["max_loss"])
    if t.get("pnl_pct"):
        return t["pnl"] / float(t["pnl_pct"])
    return None


def enrich(trades: list, quotes: QuoteBook | None) -> list:
    for t in trades:
        t.update(fill_quality(t, quotes))
        b = _basis(t)
        t["pct"] = t["pnl"] / b if b else None
        t["pct_taker"] = t["pnl_taker"] / b if b else None
    return trades


# ---------------------------------------------------------------- L2 and gates

def l2_split(trades: list) -> dict:
    groups = {"pass": [], "would_block": [], "no_l2": []}
    for t in trades:
        l2 = t.get("l2")
        key = "no_l2" if not isinstance(l2, dict) or "would_block" not in l2 else (
            "would_block" if l2["would_block"] else "pass")
        groups[key].append(t["pnl"])
    out = {k: {"n": len(v), "mean": (sum(v) / len(v)) if v else None,
               "win_rate": (sum(1 for x in v if x > 0) / len(v)) if v else None} for k, v in groups.items()}
    a, b = groups["pass"], groups["would_block"]
    if len(a) > 1 and len(b) > 1:
        va, vb = statistics.variance(a), statistics.variance(b)
        se = math.sqrt(va / len(a) + vb / len(b))
        out["welch_t"] = (statistics.mean(a) - statistics.mean(b)) / se if se else None
    else:
        out["welch_t"] = None
    return out


def _order_state_flags(trades: list) -> list:
    return [t for t in trades if any(w in (t.get("exit_reason") or "").lower() for w in ORDER_STATE_WORDS)]


def promotion_gates(book: str, trades: list) -> list[dict]:
    """HANDOFF 10.8 minimums (E: 7E gate). ok is True/False, or None when it can't be judged yet."""
    taker = [t["pnl_taker"] for t in trades]
    st = trade_stats(taker)
    sessions = len({t["session"] for t in trades})
    if book == "E":
        share = max_month_share(trades)
        return [
            {"name": "events", "value": len(trades), "need": ">= 100", "ok": len(trades) >= 100},
            {"name": "mean_taker", "value": st.get("mean"), "need": "> 0", "ok": (st.get("mean") or 0) > 0},
            {"name": "t_taker", "value": st.get("t"), "need": "> 2",
             "ok": None if st.get("t") is None else st["t"] > 2},
            {"name": "month_share", "value": share, "need": "<= 40% of P&L",
             "ok": None if share is None else share <= 0.40},
            {"name": "order_state", "value": len(_order_state_flags(trades)), "need": "0 unresolved",
             "ok": not _order_state_flags(trades)},
        ]
    pcts = [t["pct_taker"] for t in trades if t.get("pct_taker") is not None]
    ref = BACKTEST_RANGE.get(book)
    ci = bootstrap_mean_ci(pcts) if len(pcts) > 1 else (None, None)
    return [
        {"name": "sessions", "value": sessions, "need": ">= 20 with a trade", "ok": sessions >= 20},
        {"name": "trades", "value": len(trades), "need": ">= 100", "ok": len(trades) >= 100},
        {"name": "pf_taker", "value": st.get("pf"), "need": ">= 1.1",
         "ok": None if st.get("pf") is None and st["n"] == 0 else (st.get("pf") is None or st["pf"] >= 1.1)},
        {"name": "in_backtest_range", "value": ci, "need": ref["range"] if ref else "no reference",
         "ok": overlaps(ci, ref["range"]) if ref else None},
        {"name": "order_state", "value": len(_order_state_flags(trades)), "need": "0 unresolved",
         "ok": not _order_state_flags(trades)},
    ]


# ---------------------------------------------------------------- B/D go/no-go from recorded quotes

def _ct(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, CT)


def straddle_vs_realized(path, start: str, end: str, entry_ct: str = "08:45", exit_ct: str = "14:30") -> dict:
    """Per session: the 0DTE ATM straddle mid at the first snapshot at/after entry_ct vs |spot move| to exit_ct."""
    db = _connect(path)
    try:
        if not db.execute("SELECT name FROM sqlite_master WHERE name='option_quotes'").fetchone():
            return {"days": 0}
        lo = datetime.fromisoformat(f"{start}T00:00").replace(tzinfo=CT).timestamp()
        hi = datetime.fromisoformat(f"{end}T23:59").replace(tzinfo=CT).timestamp()
        rows = db.execute("SELECT ts, expiry, strike, right, bid, ask, spot FROM option_quotes WHERE ts BETWEEN ? AND ? "
                          "ORDER BY ts", (lo, hi)).fetchall()
    finally:
        db.close()
    by_day: dict = {}
    for r in rows:
        day = _ct(r["ts"]).date().isoformat()
        if r["expiry"] == day:
            by_day.setdefault(day, {}).setdefault(r["ts"], []).append(r)
    out = []
    for day, snaps in sorted(by_day.items()):
        e_at = datetime.fromisoformat(f"{day}T{entry_ct}").replace(tzinfo=CT).timestamp()
        x_at = datetime.fromisoformat(f"{day}T{exit_ct}").replace(tzinfo=CT).timestamp()
        entry = None
        for ts in sorted(s for s in snaps if e_at <= s <= e_at + 300):
            rs = snaps[ts]
            spot = rs[0]["spot"]
            k = min({r["strike"] for r in rs}, key=lambda s: abs(s - spot))
            legs = {r["right"]: r for r in rs if r["strike"] == k and r["bid"] > 0 and r["ask"] >= r["bid"]}
            if "call" in legs and "put" in legs:
                entry = (spot, sum((l["bid"] + l["ask"]) / 2 for l in legs.values()))
                break
        after = [s for s in snaps if s >= x_at]
        if entry is None or not after:
            continue
        move = abs(snaps[min(after)][0]["spot"] - entry[0])
        out.append({"day": day, "straddle": entry[1], "move": move, "edge": entry[1] - move})
    if not out:
        return {"days": 0}
    edges = [d["edge"] for d in out]
    st = trade_stats(edges)
    ms = sum(d["straddle"] for d in out) / len(out)
    mm = sum(d["move"] for d in out) / len(out)
    return {"days": len(out), "mean_straddle": ms, "mean_move": mm, "ratio": mm / ms if ms else None,
            "mean_edge": st["mean"], "t": st["t"], "edge_ci": st["mean_ci"], "per_day": out,
            "entry_ct": entry_ct, "exit_ct": exit_ct}


# ---------------------------------------------------------------- book A: SIP replay vs IEX paper

SIP_MIN_DAYS = 5            # agreement needs at least a week of replayed sessions
SIP_MIN_JACCARD = 0.80      # entry signals IEX and SIP both produce, over all signals either produces


def load_sip_replays(root) -> dict:
    """{day: {"per_day": ..., "sip": [nets], "iex": [nets]}} from research/iex_vs_sip.py output dirs under root
    (per_day.json + trades.csv). A day replayed by more than one run takes the most recent run."""
    root = Path(root).expanduser()
    runs = sorted(root.rglob("per_day.json"), key=lambda f: f.stat().st_mtime) if root.exists() else []
    days: dict = {}
    for f in runs:
        try:
            per_day = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        nets: dict = {}
        tf = f.parent / "trades.csv"
        if tf.exists():
            with open(tf, newline="") as fh:
                for row in csv.DictReader(fh):
                    if row.get("variant") in ("sip144", "iex8"):
                        nets.setdefault((row["day"], row["variant"]), []).append(float(row["net"]))
        for d in per_day:
            day = d["day"]
            days[day] = {"per_day": d, "sip": nets.get((day, "sip144"), []), "iex": nets.get((day, "iex8"), [])}
    return days


def _pool(parts: list) -> dict:
    ref, alt, m = (sum(p[k] for p in parts) for k in ("ref", "alt", "matched"))
    union = ref + alt - m
    return {"ref": ref, "alt": alt, "matched": m, "recall": m / ref if ref else None,
            "precision": m / alt if alt else None, "jaccard": m / union if union else None}


def feeds_agree(blk: dict):
    """True once IEX paper can count as evidence for book A: entry-signal Jaccard >= 0.80 vs the SIP tape and
    the two replays' nets have the same sign, over at least 5 sessions. False on any disagreement; None when
    there is too little to judge."""
    if not blk or not blk.get("days"):
        return None
    j = (blk.get("signals") or {}).get("jaccard")
    s, i = blk["sip"].get("net", 0.0), blk["iex"].get("net", 0.0)
    if (j is not None and j < SIP_MIN_JACCARD) or (s * i < 0):
        return False
    if blk["days"] < SIP_MIN_DAYS or j is None:
        return None
    return True


def sip_replay_block(days: dict, start: str, end: str) -> dict:
    sel = [days[d] for d in sorted(days) if start <= d <= end]
    if not sel:
        return {"days": 0}
    iex8 = [x["per_day"]["variants"]["iex8"] for x in sel]
    blk = {
        "days": len(sel), "first": min(d for d in days if start <= d <= end), "last": max(d for d in days if start <= d <= end),
        "sip": trade_stats([n for x in sel for n in x["sip"]]),
        "iex": trade_stats([n for x in sel for n in x["iex"]]),
        "signals": _pool([v["signals_vs_sip"] for v in iex8]),
        "taken": _pool([v["taken_vs_sip"] for v in iex8]),
        "x144": _pool([v["cross_up"]["144t"] for v in iex8]),
    }
    blk["agree"] = feeds_agree(blk)
    return blk


def feed_gate(blk: dict | None) -> dict:
    return {"name": "feed_fidelity", "value": blk, "need": f"SIP replay agrees (signal Jaccard >= {SIP_MIN_JACCARD:.2f}, "
            f"same-sign net, >= {SIP_MIN_DAYS} days)", "ok": feeds_agree(blk) if blk else None}


# ---------------------------------------------------------------- report

def last_friday(today: date) -> date:
    return today - timedelta(days=(today.weekday() - 4) % 7)


def week_bounds(week_ending: date) -> tuple:
    return ((week_ending - timedelta(days=4)).isoformat(), week_ending.isoformat())


def _book_block(trades: list) -> dict:
    by_day: dict = {}
    for t in trades:
        by_day[t["session"]] = by_day.get(t["session"], 0.0) + t["pnl_taker"]
    fills = [t for t in trades if t["priced"]]
    return {
        "stats": trade_stats([t["pnl"] for t in trades]),
        "taker": trade_stats([t["pnl_taker"] for t in trades]),
        "pct_taker": trade_stats([t["pct_taker"] for t in trades if t.get("pct_taker") is not None]),
        "daily": daily_stats(by_day),
        "vs_mid_c": (sum(t["vs_mid_c"] for t in fills) / len(fills)) if fills else None,
        "fills_priced": sum(t["priced"] for t in trades),
        "fills_unpriced": sum(t["unpriced"] for t in trades),
        "l2": l2_split(trades),
        "exit_reasons": _counts(t.get("exit_reason") or "?" for t in trades),
    }


def _counts(it) -> dict:
    d: dict = {}
    for x in it:
        d[x] = d.get(x, 0) + 1
    return dict(sorted(d.items(), key=lambda kv: -kv[1]))


def build_report(journal, week_ending: date, modes=("paper", "shadow"), sip_dir=None) -> dict:
    ws, we = week_bounds(week_ending)
    sip_days = load_sip_replays(sip_dir) if sip_dir else {}
    trades = enrich([t for t in load_trades(journal, modes) if t["session"] and t["session"] <= we], QuoteBook(journal))
    books = {}
    for b in sorted({t["book"] for t in trades} | ({"A"} if sip_days else set())):
        cum = [t for t in trades if t["book"] == b]
        week = [t for t in cum if t["session"] >= ws]
        wb, cb = _book_block(week), _book_block(cum)
        books[b] = {
            "name": BOOKS.get(b, b),
            "week": wb["stats"], "week_taker": wb["taker"], "week_detail": wb,
            "cumulative": cb["stats"], "cumulative_taker": cb["taker"], "cumulative_detail": cb,
            "gates": promotion_gates(b, cum),
            "backtest": BACKTEST_RANGE.get(b),
        }
        if b == "A":
            sw, sc = sip_replay_block(sip_days, ws, we), sip_replay_block(sip_days, "2000-01-01", we)
            books[b].update(sip_week=sw, sip_cumulative=sc)
            books[b]["gates"].append(feed_gate(sc if sc["days"] else None))
    return {
        "week_start": ws, "week_ending": we, "modes": list(modes),
        "generated": datetime.now(CT).isoformat(timespec="minutes"),
        "books": books,
        "straddle_week": straddle_vs_realized(journal, ws, we),
        "straddle_all": straddle_vs_realized(journal, "2000-01-01", we),
        "alpha": ALPHA, "bonferroni_alpha": ALPHA / BOOK_COUNT,
        "crew": crew_scorecard(journal, trades, ws, we),
    }


# ----------------------------------------------------------------------------- crew scorecard
def load_crew_log(path) -> list[dict]:
    db = _connect(path)
    try:
        if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='crew_log'").fetchone():
            return []
        rows = db.execute("SELECT session,ts,kind,book,detail FROM crew_log ORDER BY id").fetchall()
    finally:
        db.close()
    return [{"session": r[0], "ts": r[1], "kind": r[2], "book": r[3], "detail": _loads(r[4]) or {}} for r in rows]


def _crew_block(trades: list, log: list) -> dict:
    """What the crew did in one window: P&L its size votes added or saved (actual minus the same trade at 1.0x,
    scaled by quantity), how tweaked trades did, what it blocked, and what it cost."""
    sizing: dict = {}
    tw = {"with": [], "without": []}
    for t in trades:
        c = t.get("crew")
        if not isinstance(c, dict):
            continue
        tw["with" if c.get("tweaks") else "without"].append(t["pnl"])
        q, q1 = c.get("qty") or 0, c.get("qty_1x")
        if q and q1 is not None and q != q1:
            who = ("size-up gate" if q > q1 else "+".join(c.get("cut_by") or []) or "crew cut")
            r = sizing.setdefault(who, {"trades": 0, "delta": 0.0})
            r["trades"] += 1
            r["delta"] += t["pnl"] - t["pnl"] * q1 / q
    blocks: dict = {}
    for r in log:
        if r["kind"] == "block":
            k = f"{r['detail'].get('desk', '?')} -> {r['book']}"
            blocks[k] = blocks.get(k, 0) + 1
    usage = [r["detail"] for r in log if r["kind"] == "usage"]
    return {
        "sizing": sizing,
        "tweaks": {k: {"trades": len(v), "net": sum(v)} for k, v in tw.items()},
        "blocks": dict(sorted(blocks.items(), key=lambda kv: -kv[1])),
        "calendar_disagreements": sum(1 for r in log if r["kind"] == "calendar_check"),
        "sessions_logged": len({r["session"] for r in log}),
        "est_cost_usd": sum(float(u.get("est_cost_usd") or 0) for u in usage),
        "web_searches": sum(int(u.get("web_searches") or 0) for u in usage),
    }


def crew_scorecard(journal, trades: list, ws: str, we: str) -> dict:
    log = [r for r in load_crew_log(journal) if r["session"] and r["session"] <= we]
    return {"week": _crew_block([t for t in trades if t["session"] >= ws], [r for r in log if r["session"] >= ws]),
            "cumulative": _crew_block(trades, log)}


def _crew_section(rep: dict) -> list:
    sc = rep.get("crew")
    L = ["## Crew scorecard", ""]
    if not sc or not (sc["cumulative"]["sessions_logged"] or any(t["trades"] for t in sc["cumulative"]["tweaks"].values())):
        return L + ["No crew effects logged yet (the crew log starts with the 2026-09-29 crew changes).", ""]
    w, c = sc["week"], sc["cumulative"]
    L += ["Sizing: P&L the crew's size votes added (+) or cost (-), vs the same trades at 1.0x. Blocked entries are "
          "counted, not priced; pricing them needs a replay on recorded quotes.", "",
          "| Vote from | Trades (week) | P&L effect (week) | Trades (since start) | P&L effect (since start) |",
          "|---|---|---|---|---|"]
    for who in sorted(set(w["sizing"]) | set(c["sizing"])):
        a, b = w["sizing"].get(who, {"trades": 0, "delta": 0.0}), c["sizing"].get(who, {"trades": 0, "delta": 0.0})
        L.append(f"| {who} | {a['trades']} | {_usd(a['delta'])} | {b['trades']} | {_usd(b['delta'])} |")
    if not c["sizing"]:
        L.append("| none yet | 0 | | 0 | |")
    L += ["", "| | This week | Since start |", "|---|---|---|",
          f"| Trades with a crew tweak active (net) | {w['tweaks']['with']['trades']} ({_usd(w['tweaks']['with']['net'])}) | "
          f"{c['tweaks']['with']['trades']} ({_usd(c['tweaks']['with']['net'])}) |",
          f"| Trades without one (net) | {w['tweaks']['without']['trades']} ({_usd(w['tweaks']['without']['net'])}) | "
          f"{c['tweaks']['without']['trades']} ({_usd(c['tweaks']['without']['net'])}) |",
          f"| Entries blocked | {sum(w['blocks'].values())} | {sum(c['blocks'].values())} |",
          f"| Calendar vs Macro disagreements | {w['calendar_disagreements']} | {c['calendar_disagreements']} |",
          f"| Crew API cost, estimated | ${w['est_cost_usd']:.2f} | ${c['est_cost_usd']:.2f} |",
          f"| Web searches | {w['web_searches']} | {c['web_searches']} |"]
    if c["blocks"]:
        L += ["", "Blocked entries by source: " + ", ".join(f"{k} {v}" for k, v in c["blocks"].items()) + "."]
    return L + [""]


def _m(x, nd=0, pct=False, sign=True):
    if x is None:
        return "n/a"
    if pct:
        return f"{x * 100:+.1f}%" if sign else f"{x * 100:.0f}%"
    return f"{x:+,.{nd}f}" if sign else f"{x:,.{nd}f}"


def _p(p) -> str:
    return "<0.001" if p < 0.001 else f"{p:.3f}"


def _usd(x):
    return "n/a" if x is None else (f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}")


def _gate_value(g) -> str:
    v = g["value"]
    if g["name"] == "in_backtest_range":
        return "n/a" if not v or v[0] is None else f"{v[0] * 100:+.1f}% to {v[1] * 100:+.1f}%"
    if g["name"] == "feed_fidelity":
        return "no replay" if not v else f"{v['days']} days, Jaccard {_m(v['signals']['jaccard'], pct=True, sign=False)}"
    if g["name"] == "month_share":
        return _m(v, pct=True, sign=False)
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def _need(g) -> str:
    n = g["need"]
    if isinstance(n, tuple):
        return f"overlaps {n[0] * 100:+.1f}% to {n[1] * 100:+.1f}%"
    return n


def render_markdown(rep: dict) -> str:
    L = [f"# Weekly Quant report, week of {rep['week_start']} to {rep['week_ending']}", ""]
    books = rep["books"]
    if not books:
        L += ["No paper trades in the journal through this week yet (sim trades never count).", ""]
    else:
        L += ["All P&L is per book in dollars. \"Taker\" re-prices every fill at the natural (the touch), the cost "
              "HANDOFF's gates use. Paper balance $10,000; nothing here is live.", "",
              "## This week", "",
              "| Book | Trades | Net | Net at taker | Win | PF taker | Worst day | Max DD |",
              "|---|---|---|---|---|---|---|---|"]
        for b, r in books.items():
            w, wt, d = r["week"], r["week_taker"], r["week_detail"]["daily"]
            if not w["n"]:
                L.append(f"| {b} {r['name']} | 0 | | | | | | |")
                continue
            wd = d["worst_day"]
            L.append(f"| {b} {r['name']}{_iex_tag(b, r)} | {w['n']} | {_usd(w['net'])} | {_usd(wt['net'])} | "
                     f"{_m(w['win_rate'], pct=True, sign=False)} | {_m(wt['pf'], 2, sign=False)} | "
                     f"{_usd(wd[1]) if wd else 'n/a'} | {_usd(d['max_drawdown'])} |")
        L += ["", "## Since the start (promotion basis)", "",
              "| Book | Sessions | Trades | Net at taker | Mean / median per trade | t (p) | Mean on risk, 95% CI | Max DD |",
              "|---|---|---|---|---|---|---|---|"]
        for b, r in books.items():
            c, ct_, cd = r["cumulative"], r["cumulative_taker"], r["cumulative_detail"]
            pt = cd["pct_taker"]
            ci = pt.get("mean_ci") if pt["n"] else None
            ci_s = "n/a" if not ci or ci[0] is None else f"{_m(pt['mean'], pct=True)} ({ci[0] * 100:+.1f}% to {ci[1] * 100:+.1f}%)"
            tp = "n/a" if ct_.get("t") is None else f"{ct_['t']:+.2f} ({_p(ct_['p'])})"
            L.append(f"| {b}{_iex_tag(b, r)} | {cd['daily']['sessions']} | {c['n']} | {_usd(ct_['net'])} | "
                     f"{_usd(ct_.get('mean'))} / {_usd(ct_.get('median'))} | {tp} | {ci_s} | {_usd(cd['daily']['max_drawdown'])} |")
        L += ["", f"Five books are tested at once, so a result counts as significant only at p < "
                  f"{rep['bonferroni_alpha']:.2f} (Bonferroni, {ALPHA:.2f} / {BOOK_COUNT}), not 0.05. "
                  "Trades inside one session are not independent; the per-session t is shown per book as a check.", ""]
        for b, r in books.items():
            L += _book_section(b, r)
    L += _straddle_section(rep)
    L += _crew_section(rep)
    L += ["## Notes", "",
          "- Journal schema: the multi-book framework's trades table (PR #6: `book`, `legs`, `max_loss`). "
          "A journal without `book` is read as all book A.",
          "- Book A fills log no quote, so its taker cost uses the nearest recorded quote within 15 s; "
          "fills with none are left as filled and counted as unpriced.",
          "- Backtest ranges are Black-Scholes modeled (HANDOFF section 7), so \"in range\" is a sanity check, "
          "not validation. D uses the entry-time quiet filter re-run from the approved spec.",
          "- Order-state flags are exits whose reason mentions a halt, mismatch, stale quote or kill; "
          "each needs Evan to confirm it is resolved before a promotion.", ""]
    return "\n".join(L)


def _iex_tag(b: str, r: dict) -> str:
    if b != "A" or any(g["name"] == "feed_fidelity" and g["ok"] is True for g in r["gates"]):
        return ""
    return " (IEX, not evidence)"


def _book_section(b: str, r: dict) -> list:
    cd, c, ct_ = r["cumulative_detail"], r["cumulative"], r["cumulative_taker"]
    L = [f"## Book {b}: {r['name']}", ""]
    if not c["n"]:
        return L + ["No paper trades yet."] + (_sip_section(r) if b == "A" else [""])
    wl, wh = c["win_ci"]
    L += [f"- Win rate {_m(c['win_rate'], pct=True, sign=False)} (95% CI {_m(wl, pct=True, sign=False)} to "
          f"{_m(wh, pct=True, sign=False)}); PF {_m(c['pf'], 2, sign=False)} as filled, "
          f"{_m(ct_['pf'], 2, sign=False)} at taker.",
          f"- Per trade at taker: mean {_usd(ct_['mean'])}, median {_usd(ct_['median'])}, 5th to 95th percentile "
          f"{_usd(ct_['p5'])} to {_usd(ct_['p95'])}, worst {_usd(ct_['worst'])}, best {_usd(ct_['best'])}; "
          f"{ct_['outliers']} IQR outlier(s), kept.",
          f"- Per-session t at taker: {_m(cd['daily']['day_t'], 2)} over {cd['daily']['sessions']} session(s); "
          f"worst day {_usd(cd['daily']['worst_day'][1])} ({cd['daily']['worst_day'][0]}).",
          f"- Fills: {cd['fills_priced']} priced, {cd['fills_unpriced']} without a quote; given up vs mid "
          f"{_m(cd['vs_mid_c'], 1, sign=False)}¢ per share per trade."]
    l2 = cd["l2"]
    if l2["pass"]["n"] or l2["would_block"]["n"]:
        L.append(f"- L2 split (observe mode): pass {l2['pass']['n']} trades, mean {_usd(l2['pass']['mean'])}; "
                 f"would block {l2['would_block']['n']}, mean {_usd(l2['would_block']['mean'])}; "
                 f"Welch t {_m(l2['welch_t'], 2)}. Enforce only if this separates winners from losers after ~50 trades.")
    L.append("- Exits: " + ", ".join(f"{k} {v}" for k, v in list(cd["exit_reasons"].items())[:6]) + ".")
    evidence = True
    if b == "A":
        L += _sip_section(r)
        evidence = any(g["name"] == "feed_fidelity" and g["ok"] is True for g in r["gates"])
    L += ["", "Promotion gates:", "", "| Gate | Now | Needs | |", "|---|---|---|---|"]
    for g in r["gates"]:
        mark = {True: "pass", False: "not yet", None: "n/a"}[g["ok"]]
        if g["name"] == "order_state" and g["ok"] is False:
            mark = "review"
        if not evidence and g["name"] in ("pf_taker", "in_backtest_range"):
            mark = "not evidence (IEX)"
        if g["name"] == "feed_fidelity" and g["ok"] is not True:
            mark = "not evidence (IEX)"
        L.append(f"| {g['name'].replace('_', ' ')} | {_gate_value(g)} | {_need(g)} | {mark} |")
    if r.get("backtest"):
        L.append(f"\nBacktest reference: {r['backtest']['src']}.")
    return L + [""]


def _sip_section(r: dict) -> list:
    L = ["", "SIP replay vs IEX paper (research/iex_vs_sip.py, after-close replay on the full tape):", ""]
    sc = r.get("sip_cumulative") or {}
    if not sc.get("days"):
        return L + ["No SIP replay yet, so book A's IEX paper results are not evidence. IEX sees about 4% of SPY "
                    "prints and its 8-print \"144t\" bars miss about half the real 144t crosses.", ""]
    L += ["| Window | Days | SIP replay net (PF) | IEX replay net (PF) | IEX paper net | Signal agreement | 144t crosses caught |",
          "|---|---|---|---|---|---|---|"]
    for name, blk, paper in (("This week", r.get("sip_week") or {}, r["week"]), ("Since start", sc, r["cumulative"])):
        if not blk.get("days"):
            L.append(f"| {name} | 0 | | | {_usd(paper.get('net'))} | | |")
            continue
        L.append(f"| {name} | {blk['days']} | {_usd(blk['sip'].get('net'))} ({_m(blk['sip'].get('pf'), 2, sign=False)}) | "
                 f"{_usd(blk['iex'].get('net'))} ({_m(blk['iex'].get('pf'), 2, sign=False)}) | {_usd(paper.get('net'))} | "
                 f"{_m(blk['signals']['jaccard'], pct=True, sign=False)} | {_m(blk['x144']['recall'], pct=True, sign=False)} |")
    verdict = {True: "The feeds agree, so IEX paper counts as evidence.",
               False: "The feeds disagree, so book A's IEX paper results are not evidence; judge A on the SIP replay.",
               None: "Too few replayed sessions to judge; book A's IEX paper results are not evidence yet."}[sc.get("agree")]
    return L + ["", f"Signal agreement is the Jaccard overlap of entry signals (matched within 2 min). {verdict}", ""]


def _straddle_section(rep: dict) -> list:
    L = ["## B/D go/no-go: 0DTE ATM straddle vs realized move", ""]
    a, w = rep["straddle_all"], rep["straddle_week"]
    if not a.get("days"):
        return L + ["No recorded 0DTE quote sessions yet (journal.option_quotes).", ""]
    L += [f"Straddle mid at the first snapshot from {a['entry_ct']} CT vs the SPY move to {a['exit_ct']} CT. "
          "A positive edge is the variance premium B and D sell.", "",
          "| Window | Days | Straddle | Realized move | Move / straddle | Edge per day (95% CI) | t |",
          "|---|---|---|---|---|---|---|"]
    for name, s in (("This week", w), ("Since start", a)):
        if not s.get("days"):
            L.append(f"| {name} | 0 | | | | | |")
            continue
        ci = s["edge_ci"]
        ci_s = "" if ci[0] is None else f" ({ci[0]:+.2f} to {ci[1]:+.2f})"
        L.append(f"| {name} | {s['days']} | ${s['mean_straddle']:.2f} | ${s['mean_move']:.2f} | "
                 f"{_m(s['ratio'], pct=True, sign=False)} | {s['mean_edge']:+.2f}{ci_s} | {_m(s['t'], 2)} |")
    return L + [""]


def _json_default(o):
    if isinstance(o, float) and (math.isinf(o) or math.isnan(o)):
        return None
    raise TypeError(type(o))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reporting.weekly_quant", description=__doc__.split("\n")[0])
    ap.add_argument("--journal", default="~/.agentdesk/journal.db")
    ap.add_argument("--week-ending", help="Friday YYYY-MM-DD (default: the latest Friday in Central time)")
    ap.add_argument("--out", default="reports", help="directory for quant-<date>.md and .json")
    ap.add_argument("--modes", default="paper,shadow", help="journal modes to include (sim never belongs here)")
    ap.add_argument("--sip-dir", help="directory holding research/iex_vs_sip.py output runs (book A SIP replay)")
    a = ap.parse_args(argv)
    path = Path(a.journal).expanduser()
    if not path.exists():
        print(f"journal not found: {path}", file=sys.stderr)
        return 2
    we = date.fromisoformat(a.week_ending) if a.week_ending else last_friday(datetime.now(CT).date())
    rep = build_report(path, we, tuple(m.strip() for m in a.modes.split(",") if m.strip()), sip_dir=a.sip_dir)
    md = render_markdown(rep)
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (out / f"quant-{we.isoformat()}.md").write_text(md)
    (out / f"quant-{we.isoformat()}.json").write_text(json.dumps(rep, indent=1, default=_json_default))
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
