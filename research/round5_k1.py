"""K1 (research/round5_prereg.md section 5): do Kalshi favourites (a side priced 70-97c a day before the close)
still earn a positive return after the taker fee on markets that closed after Bürgi, Deng & Whelan's sample?
Information only: the agent can't trade event contracts. Kalshi's public API, no account, no auth.

    .venv/bin/python research/round5_k1.py --fetch     # resumable pull into research/data/round5/k1/ (gitignored)
    .venv/bin/python research/round5_k1.py --report    # k1_results.json and k1_summary.md from the cache

--fetch runs these steps; each one skips what is already on disk, so a restart picks up where it stopped:
1. `series.json.gz` (GET /series: fee_type, fee_multiplier, category) and `cutoff.json` (GET /historical/cutoff).
2. `hist_markets*/` (GET /historical/markets?mve_filter=exclude, markets archived before the cutoff, read in
   created-time segments from constructed cursors; see hist_segments) and
   `live_markets/` (GET /markets?status=settled&mve_filter=exclude, settled markets still in the live set), raw
   pages as gzipped JSON plus a `state.json` holding the next cursor. /events?status=settled leaves out settled
   markets whose event still has open markets, so the live list is read by market status instead.
3. `eligible.parquet` (one row per eligible market, deduplicated by ticker) and `listing_summary.json` (every
   listed market counted once, by eligibility reason and listing).
4. `sample.json`: the eligible markets, k and the kept event tickers (SHA-1 of event_ticker mod k = 0).
5. `event_series.json`: event -> series for the kept events (GET /events?tickers=..., 100 a call).
6. `candles/{ticker}.json.gz`: daily candles (period_interval 1440) from the market's open to its close, from
   /historical/markets/{t}/candlesticks or /series/{s}/markets/{t}/candlesticks (the other one if the first has none);
   live markets may come from /markets/candlesticks in batches of 25, cut to each market's open..close (the same
   candles; that endpoint was throttled far less than the single-market one).

Rules (frozen): binary, non-multivariate markets settled yes or no, close_time 2021-07-01 to 2026-10-29 (UTC), open
at least 3 days. Entry on the last daily candle ending >= 24 h before close_time; skip if the market settled before
that candle's end, if yes ask - yes bid at the candle's close > $0.05, or if fewer than 100 contracts traded in the 7
daily candles up to it. Buy YES if the yes ask is in [0.70, 0.97], NO if 1 - yes bid is. Taker fee =
fee_multiplier x 0.07 x P x (1 - P) from the series' current fee_type (quadratic or quadratic_with_maker_fees; other
series dropped and counted). Return = (payout - P - fee) / (P + fee). In-sample: close before 2025-05-01. Pass: IS
mean > 0, OOS event-clustered t >= 2.58 (markets in one event averaged first), OOS mean > 0 with the fee rounded up
to a whole cent. Fee changes over time are not modelled.

Rate: one shared limiter keeps all threads together at or under 5 requests/s and slows down on each HTTP 429.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import http.client
import json
import logging
import math
import multiprocessing
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "research" / "data" / "round5" / "k1"
BASE = "https://api.elections.kalshi.com/trade-api/v2"

UTC = timezone.utc
WINDOW_START = int(datetime(2021, 7, 1, tzinfo=UTC).timestamp())
WINDOW_END = int(datetime(2026, 10, 30, tzinfo=UTC).timestamp())          # exclusive: closes through 2026-10-29
OOS_START = int(datetime(2025, 5, 1, tzinfo=UTC).timestamp())
MIN_OPEN_SEC = 3 * 86400
MAX_MARKETS = 60_000
MAX_SPREAD = 0.05
MIN_VOLUME = 100.0
VOLUME_CANDLES = 7
FEE_RATE = 0.07
FEE_TYPES = ("quadratic", "quadratic_with_maker_fees")
SETTLED = ("finalized", "settled")
T_BAR, T_SINGLE = 2.58, 2.33

# (lo, hi, lo_open, hi_closed): which ask prices a side may be bought at
FAVOURITE = (0.70, 0.97, False, True)
BUCKETS = {"0.50-0.70": (0.50, 0.70, False, False), "0.70-0.85": (0.70, 0.85, False, False),
           "0.85-0.97": (0.85, 0.97, False, True), "longshot_<0.30": (0.0, 0.30, True, False)}

log = logging.getLogger("round5_k1")


# ------------------------------------------------------------------ market eligibility
def parse_ts(s) -> int | None:
    if not s:
        return None
    return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())


def is_mve(m: dict) -> bool:
    return bool(m.get("mve_collection_ticker")) or str(m.get("ticker", "")).startswith("KXMVE") \
        or str(m.get("event_ticker", "")).startswith("KXMVE")


def eligibility_reason(m: dict) -> str | None:
    """None if the market qualifies under the prereg's Markets paragraph, else the first rule it fails."""
    if m.get("market_type") != "binary":
        return "not_binary"
    if is_mve(m):
        return "mve"
    if m.get("result") not in ("yes", "no") or m.get("status") not in SETTLED:
        return "not_settled_yes_no"
    close = parse_ts(m.get("close_time"))
    if close is None or not WINDOW_START <= close < WINDOW_END:
        return "close_outside_window"
    opened = parse_ts(m.get("open_time"))
    if opened is None or close - opened < MIN_OPEN_SEC:
        return "open_lt_3d"
    return None


def _volume(m: dict) -> float | None:
    v = m.get("volume_fp", m.get("volume"))
    return None if v is None else float(v)


def needs_candles(row: dict) -> bool:
    """A market whose lifetime volume is under 100 contracts can't have 100 in any 7 candles: no fetch needed."""
    v = row.get("volume")
    return v is None or pd.isna(v) or v + 1e-9 >= MIN_VOLUME


def market_row(m: dict, source: str) -> dict:
    return {"volume": _volume(m), "ticker": m.get("ticker"), "event_ticker": m.get("event_ticker"),
            "market_type": m.get("market_type"), "mve": is_mve(m), "status": m.get("status"), "result": m.get("result"),
            "open_ts": parse_ts(m.get("open_time")), "close_ts": parse_ts(m.get("close_time")),
            "settled_ts": parse_ts(m.get("settlement_ts")), "can_close_early": bool(m.get("can_close_early")),
            "source": source, "reason": eligibility_reason(m)}


def period(close_ts: int) -> str:
    return "IS" if close_ts < OOS_START else "OOS"


# ------------------------------------------------------------------ SHA-1 sampling
def event_hash(event_ticker: str) -> int:
    return int(hashlib.sha1(event_ticker.encode("utf-8")).hexdigest(), 16)


def choose_k(markets_per_event: dict, cap: int = MAX_MARKETS) -> int:
    """Smallest k >= 1 for which the events with SHA-1(event_ticker) mod k == 0 hold at most `cap` markets."""
    pairs = [(event_hash(e), n) for e, n in markets_per_event.items()]
    for k in range(1, 10_000_000):
        if sum(n for h, n in pairs if h % k == 0) <= cap:
            return k
    raise RuntimeError("no k keeps the sample under the cap")


def sample(df: pd.DataFrame, cap: int = MAX_MARKETS) -> tuple[int, pd.DataFrame]:
    k = choose_k(df.groupby("event_ticker").size().to_dict(), cap)
    keep = {e for e in df["event_ticker"].unique() if event_hash(e) % k == 0}
    return k, df[df["event_ticker"].isin(keep)]


# ------------------------------------------------------------------ candles and entry
def _px(d) -> float | None:
    if not d:
        return None
    v = d.get("close", d.get("close_dollars"))
    return None if v is None else float(v)


def norm_candles(raw: list) -> list[dict]:
    """Both API formats (historical: yes_bid.close / volume; live: yes_bid.close_dollars / volume_fp)."""
    out = []
    for c in raw or []:
        vol = c.get("volume")
        if vol is None:
            vol = c.get("volume_fp")
        out.append({"end": int(c["end_period_ts"]), "bid": _px(c.get("yes_bid")), "ask": _px(c.get("yes_ask")),
                    "vol": float(vol or 0)})
    return sorted(out, key=lambda c: c["end"])


def entry(candles: list[dict], close_ts: int, settled_ts: int | None, lead_hours: float = 24) -> dict:
    """The entry candle and its filters. `skip` is None when the market can be traded."""
    cut = close_ts - lead_hours * 3600
    idx = None
    for i, c in enumerate(candles):
        if c["end"] <= cut:
            idx = i
        else:
            break
    if idx is None:
        return {"skip": "no_entry_candle"}
    c = candles[idx]
    first = max(0, idx - (VOLUME_CANDLES - 1))
    out = {"skip": None, "end": c["end"], "bid": c["bid"], "ask": c["ask"],
           "vol7": sum(x["vol"] for x in candles[first:idx + 1]), "span_days": (c["end"] - candles[first]["end"]) / 86400}
    if settled_ts is not None and settled_ts < c["end"]:
        out["skip"] = "settled_before_entry"
    elif c["bid"] is None or c["ask"] is None:
        out["skip"] = "no_quote"
    elif round(c["ask"] - c["bid"], 6) > MAX_SPREAD:
        out["skip"] = "spread"
    elif out["vol7"] + 1e-9 < MIN_VOLUME:
        out["skip"] = "volume"
    return out


def sides_in(bid, ask, lo: float, hi: float, hi_closed: bool = True, lo_open: bool = False) -> list:
    """Sides whose ask lies in the range: YES at the yes ask, NO at 1 - yes bid."""
    if bid is None or ask is None:
        return []
    out = []
    for side, p in (("yes", ask), ("no", 1.0 - bid)):
        p = round(p, 6)
        if (p > lo if lo_open else p >= lo) and (p <= hi if hi_closed else p < hi):
            out.append((side, p))
    return out


def pick_side(bid, ask):
    s = sides_in(bid, ask, *FAVOURITE[:2], hi_closed=FAVOURITE[3], lo_open=FAVOURITE[2])
    return s[0] if s else None


# ------------------------------------------------------------------ fees, returns, statistics
def taker_fee(p: float, mult: float) -> float:
    return mult * FEE_RATE * p * (1 - p)


def round_up_cent(fee: float) -> float:
    return math.ceil(round(fee * 100, 6)) / 100


def contract_return(p: float, fee: float, won: bool) -> float:
    return ((1.0 if won else 0.0) - p - fee) / (p + fee)


def summarize(df: pd.DataFrame, col: str) -> dict:
    """Trade-level mean and win rate; t over event means (markets in one event averaged first)."""
    n = len(df)
    if not n:
        return {"trades": 0, "events": 0, "mean": None, "event_mean": None, "t": None, "win_rate": None}
    ev = df.groupby("event_ticker")[col].mean()
    g = len(ev)
    t = None
    if g >= 2:
        sd = float(ev.std(ddof=1))
        t = float(ev.mean() / (sd / math.sqrt(g))) if sd > 0 else None
    out = {"trades": int(n), "events": int(g), "mean": float(df[col].mean()), "event_mean": float(ev.mean()),
           "t": t, "win_rate": float(df["won"].mean())}
    if "price" in df:
        out["mean_price"] = float(df["price"].mean())
    return out


def verdict(is_s: dict, oos_s: dict, oos_up_s: dict) -> dict:
    t = oos_s.get("t")
    bars = [{"bar": "in-sample mean > 0", "value": is_s.get("mean"), "is_t": False,
             "ok": is_s.get("mean") is not None and is_s["mean"] > 0},
            {"bar": f"out-of-sample event-clustered t >= {T_BAR}", "value": t, "is_t": True,
             "ok": t is not None and t >= T_BAR},
            {"bar": "out-of-sample mean > 0, fee rounded up to a whole cent", "value": oos_up_s.get("mean"),
             "is_t": False, "ok": oos_up_s.get("mean") is not None and oos_up_s["mean"] > 0}]
    return {"pass": all(b["ok"] for b in bars), "bars": bars, "clears_2_33": t is not None and t >= T_SINGLE}


# ------------------------------------------------------------------ markets -> entries -> trades
def build_entries(df: pd.DataFrame, series: dict, candles, lead_hours: float = 24) -> tuple[pd.DataFrame, Counter]:
    """Apply the series fee-type rule and the entry-candle filters. `candles` maps ticker -> normalized candles."""
    counts, rows, dropped = Counter(), [], Counter()
    for r in df.itertuples(index=False):
        s = series.get(r.series_ticker)
        if s is None:
            counts["series_missing"] += 1
            continue
        if s.get("fee_type") not in FEE_TYPES:
            counts["fee_type_dropped"] += 1
            dropped[f"{r.series_ticker} ({s.get('fee_type')})"] += 1
            continue
        if not needs_candles(r._asdict()):
            counts["volume"] += 1
            counts["volume_market_total"] += 1
            continue
        c = candles.get(r.ticker)
        if c is None:
            counts["candles_missing"] += 1
            continue
        settled = None if pd.isna(r.settled_ts) else int(r.settled_ts)
        e = entry(c, int(r.close_ts), settled, lead_hours)
        if e["skip"]:
            counts[e["skip"]] += 1
            continue
        counts["entered"] += 1
        rows.append({"ticker": r.ticker, "event_ticker": r.event_ticker, "series_ticker": r.series_ticker,
                     "category": s.get("category") or "unknown", "fee_type": s.get("fee_type"),
                     "fee_multiplier": 1.0 if s.get("fee_multiplier") is None else float(s["fee_multiplier"]),
                     "result": r.result, "close_ts": int(r.close_ts), "period": period(int(r.close_ts)),
                     "can_close_early": bool(r.can_close_early), "entry_end": e["end"], "bid": e["bid"],
                     "ask": e["ask"], "vol7": e["vol7"], "span_days": e["span_days"],
                     "volume_total": getattr(r, "volume", None)})
    counts["fee_type_dropped_series"] = len(dropped)
    cols = ["ticker", "event_ticker", "series_ticker", "category", "fee_type", "fee_multiplier", "result", "close_ts",
            "period", "can_close_early", "entry_end", "bid", "ask", "vol7", "span_days", "volume_total"]
    out = pd.DataFrame(rows, columns=cols)
    out.attrs["dropped_series"] = dict(dropped)
    return out, counts


def trades(entries: pd.DataFrame, rule=FAVOURITE, maker: bool = False) -> pd.DataFrame:
    """One row per contract bought. Taker: the side's ask plus the quadratic fee. Maker: the side's bid, no fee."""
    lo, hi, lo_open, hi_closed = rule
    rows = []
    for r in entries.itertuples(index=False):
        for side, ask_px in sides_in(r.bid, r.ask, lo, hi, hi_closed=hi_closed, lo_open=lo_open):
            if maker:
                price = round(r.bid if side == "yes" else 1.0 - r.ask, 6)
                if price <= 0:
                    continue
                fee = 0.0
            else:
                price, fee = ask_px, taker_fee(ask_px, r.fee_multiplier)
            fee_up = round_up_cent(fee)
            won = side == r.result
            rows.append({**r._asdict(), "side": side, "price": price, "fee": fee, "fee_up": fee_up, "won": won,
                         "ret": contract_return(price, fee, won), "ret_up": contract_return(price, fee_up, won)})
    cols = list(entries.columns) + ["side", "price", "fee", "fee_up", "won", "ret", "ret_up"]
    return pd.DataFrame(rows, columns=cols)


def by_period(t: pd.DataFrame, with_up: bool = True) -> dict:
    out = {}
    for p in ("IS", "OOS", "ALL"):
        sub = t if p == "ALL" else t[t["period"] == p]
        s = summarize(sub, "ret")
        if with_up:
            u = summarize(sub, "ret_up")
            s.update({"mean_fee_up": u["mean"], "event_mean_fee_up": u["event_mean"], "t_fee_up": u["t"]})
        out[p] = s
    return out


def analyse(entries24: pd.DataFrame, entries72: pd.DataFrame, counts: Counter) -> dict:
    fav = trades(entries24, FAVOURITE)
    head = by_period(fav)
    res = {"headline": head,
           "verdict": verdict(head["IS"], head["OOS"], {"mean": head["OOS"]["mean_fee_up"]}),
           "verdict_event_mean": verdict({"mean": head["IS"]["event_mean"]}, head["OOS"],
                                         {"mean": head["OOS"]["event_mean_fee_up"]}),
           "buckets": {name: by_period(trades(entries24, rule)) for name, rule in BUCKETS.items()},
           "by_category": {cat: by_period(g) for cat, g in fav.groupby("category")},
           "maker": by_period(trades(entries24, FAVOURITE, maker=True), with_up=False),
           "entry_72h": by_period(trades(entries72, FAVOURITE)),
           "favourite_sides": {p: dict(Counter(fav[fav["period"] == p]["side"])) for p in ("IS", "OOS")},
           "no_side_in_range": int(len(entries24) - fav["ticker"].nunique()),
           "diagnostics": {
               "note": "not pre-registered; changes no verdict",
               "can_close_early": {str(flag): by_period(g) for flag, g in fav.groupby("can_close_early")},
               "volume_window_longer_than_7_days": int((fav["span_days"] > 6.5).sum()) if len(fav) else 0,
               "fee_multiplier_counts": {str(k): int(v) for k, v in Counter(fav["fee_multiplier"]).items()},
               "entries_with_7_candle_volume_above_lifetime_volume": int(
                   (entries24["vol7"] > entries24["volume_total"].astype(float) + 1e-6).sum()) if len(entries24) else 0,
           }}
    res["counts"] = dict(counts)
    return res


# ------------------------------------------------------------------ HTTP
class NotFound(Exception):
    pass


class BadRequest(NotFound):
    pass


class Limiter:
    """Shared pacing for every thread: at least `interval` seconds between requests, never under `lo` (5 requests/s
    in all, retries included). From this network Kalshi answers 20-25% of requests with 429 at any rate from 0.75
    to 2 requests/s, so single 429s don't slow the pace (the thread just retries after a pause). Every `window`
    outcomes the share of 429s is checked: above half, the pace slows x1.5; under 35%, it speeds up x1.25."""

    def __init__(self, interval: float = 0.2, lo: float = 0.2, hi: float = 3.0, window: int = 50):
        self.interval, self.lo, self.hi, self.window = interval, lo, hi, window
        self.next = 0.0
        self.lock = threading.Lock()
        self.throttles = 0
        self.seen = self.bad = 0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next)
            self.next = at + self.interval
        if at > now:
            time.sleep(at - now)

    def _record(self, bad: bool):
        self.seen += 1
        self.bad += bad
        if self.seen >= self.window:
            share = self.bad / self.seen
            if share > 0.5:
                self.interval = min(self.hi, self.interval * 1.5)
            elif share < 0.35:
                self.interval = max(self.lo, self.interval / 1.25)
            self.seen = self.bad = 0

    def ok(self):
        with self.lock:
            self._record(False)

    def throttled(self):
        with self.lock:
            self.throttles += 1
            self._record(True)


class Api:
    """GET with retries (429 and 5xx back off and retry; 404 raises NotFound, 400 BadRequest). At most `in_flight`
    requests are open at once."""

    def __init__(self, base: str = BASE, limiter: Limiter | None = None, timeout: float = 90, retries: int = 12,
                 in_flight: int = 4):
        self.base, self.limiter, self.timeout, self.retries = base, limiter or Limiter(), timeout, retries
        self.calls = 0
        self.slots = threading.BoundedSemaphore(in_flight)

    def __call__(self, path: str, params: dict | None = None) -> dict:
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(self.retries):
            self.limiter.wait()
            backoff = min(60.0, 2.0 * 1.6 ** attempt)
            try:
                req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "Accept": "application/json",
                                                           "User-Agent": "agentdesk-research-k1/1"})
                with self.slots, urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = r.read()
                    enc = r.headers.get("Content-Encoding")
                if enc == "gzip":
                    body = gzip.decompress(body)
                self.calls += 1
                self.limiter.ok()
                return json.loads(body)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise NotFound(path) from None
                if e.code == 400:
                    raise BadRequest(f"{path}: {e.read()[:200]!r}") from None
                if e.code == 429 or e.code >= 500:
                    self.limiter.throttled()
                    time.sleep(backoff if e.code >= 500 else min(20.0, 0.5 * 1.6 ** attempt))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException,
                    json.JSONDecodeError, OSError) as e:
                log.warning("retry %s after %r", path, e)
                time.sleep(backoff)
        raise RuntimeError(f"gave up on {url}")


# ------------------------------------------------------------------ resumable fetch
def _write_json_gz(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _read_json_gz(path: Path):
    with gzip.open(path, "rt") as f:
        return json.load(f)


def _write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, path)


def _varint(x: int) -> bytes:
    out = bytearray()
    while True:
        b, x = x & 0x7F, x >> 7
        out.append(b | 0x80 if x else b)
        if not x:
            return bytes(out)


def _ld(field: int, payload: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(payload)) + payload


def make_cursor(sec: int, nanos: int, ticker: str) -> str:
    """A listing cursor as Kalshi encodes it: {1: {1: created seconds, 2: nanos}, 2: ticker}, base64url, unpadded.
    /historical/markets lists by created_time, newest first, so a cursor built at time T starts the listing there."""
    ts = _varint(1 << 3) + _varint(sec) + _varint(2 << 3) + _varint(nanos)
    return base64.urlsafe_b64encode(_ld(1, ts) + _ld(2, ticker.encode())).decode().rstrip("=")


# The archive is tens of millions of markets, listed newest-created first. It is read in created-time segments that
# run in parallel: each segment starts at its boundary with a constructed cursor and the newer one runs an hour past
# it, so nothing falls between two segments (repeats are dropped by ticker).
HIST_SEGMENTS = [("hist_markets", None), ("hist_markets_x1", "2026-06-15"), ("hist_markets_s1", "2026-05-01"),
                 ("hist_markets_x2", "2026-03-15"), ("hist_markets_s2", "2026-02-01"),
                 ("hist_markets_s3", "2025-11-01"), ("hist_markets_s4", "2025-07-01"),
                 ("hist_markets_s5", "2025-01-01")]                  # (directory, created-time start), newest first
SEGMENT_OVERLAP_SEC = 3600


def hist_segments() -> list[dict]:
    starts = [None if b is None else int(datetime.fromisoformat(b).replace(tzinfo=UTC).timestamp())
              for _, b in HIST_SEGMENTS]
    segs = []
    for i, ((d, _), st) in enumerate(zip(HIST_SEGMENTS, starts)):
        nxt = starts[i + 1] if i + 1 < len(starts) else None
        segs.append({"dir": d, "start_cursor": None if st is None else make_cursor(st, 1, "~"), "start_ts": st,
                     "stop_before_ts": None if nxt is None else nxt - SEGMENT_OVERLAP_SEC})
    return segs


def paginate(get, path: str, params: dict, key: str, out_dir: Path, log_every: int = 50,
             start_cursor: str | None = None, stop_before_ts: int | None = None) -> dict:
    """Cursor-paged listing. Each raw page is saved before the state moves on, so a restart resumes from the cursor.
    With stop_before_ts, the listing ends after the first page holding a market created before that time."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sp = out_dir / "state.json"
    st = json.loads(sp.read_text()) if sp.exists() else {"cursor": start_cursor, "pages": 0, "items": 0,
                                                          "done": False}
    while not st["done"]:
        q = dict(params)
        if st["cursor"]:
            q["cursor"] = st["cursor"]
        d = get(path, q)
        items = d.get(key) or []
        n = st["pages"] + 1
        _write_json_gz(out_dir / f"p{n:06d}.json.gz", d)
        cur = d.get("cursor") or None
        below = stop_before_ts is not None and items and \
            min(parse_ts(m.get("created_time")) or 0 for m in items) < stop_before_ts
        st = {"cursor": cur, "pages": n, "items": st["items"] + len(items), "done": bool(not cur or not items or below)}
        _write_json(sp, st)
        if n % log_every == 0:
            lim = getattr(get, "limiter", None)
            log.info("%s: %d pages, %d items (limiter %s s/unit, %s throttles)", path, n, st["items"],
                     None if lim is None else round(lim.interval, 3), None if lim is None else lim.throttles)
    return st


def read_pages(out_dir: Path, key: str):
    for p in sorted(Path(out_dir).glob("p*.json.gz")):
        yield from _read_json_gz(p).get(key) or []


def _safe(ticker: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", ticker)


def candle_path(out_dir: Path, ticker: str) -> Path:
    return Path(out_dir) / f"{_safe(ticker)}.json.gz"


def fetch_candles_one(get, row: dict, out_dir: Path) -> str:
    path = candle_path(out_dir, row["ticker"])
    if path.exists():
        return "cached"
    params = {"start_ts": int(row["open_ts"]), "end_ts": int(row["close_ts"]), "period_interval": 1440}
    hist = f"/historical/markets/{row['ticker']}/candlesticks"
    live = f"/series/{row['series_ticker']}/markets/{row['ticker']}/candlesticks"
    order = [live, hist] if row["source"] == "live" else [hist, live]
    got = None
    for ep in order:
        try:
            d = get(ep, params)
        except NotFound:
            continue
        if d.get("candlesticks"):
            got = {**d, "endpoint": ep}
            break
        got = got or {**d, "endpoint": ep}
    if got is None:
        got = {"ticker": row["ticker"], "candlesticks": [], "not_found": True}
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_gz(path, got)
    return "fetched"


def fetch_candles_batch(get, rows: list[dict], out_dir: Path, max_tickers: int = 25,
                        max_candles: int = 2000) -> list[dict]:
    """Live markets through GET /markets/candlesticks (up to 100 tickers and 10,000 candles a call; it serves only the
    live set). Each market's candles are cut to its own open..close, which gives exactly what the single-market
    endpoint returns. Returns the rows the batch call did not cover, for fetch_candles_one."""
    rows = sorted(rows, key=lambda r: (r["close_ts"], r["ticker"]))
    groups, cur, n = [], [], 0
    for r in rows:
        need = int((r["close_ts"] - r["open_ts"]) // 86400) + 3
        if cur and (len(cur) >= max_tickers or n + need > max_candles):
            groups.append(cur)
            cur, n = [], 0
        cur.append(r)
        n += need
    if cur:
        groups.append(cur)
    left = []
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for g in groups:
        q = {"market_tickers": ",".join(r["ticker"] for r in g), "start_ts": int(min(r["open_ts"] for r in g)),
             "end_ts": int(max(r["close_ts"] for r in g)), "period_interval": 1440}
        try:
            got = {m.get("market_ticker"): m.get("candlesticks") or [] for m in get("/markets/candlesticks", q)
                   .get("markets") or []}
        except NotFound:
            got = {}
        for r in g:
            if r["ticker"] not in got:
                left.append(r)
                continue
            cs = [c for c in got[r["ticker"]] if r["open_ts"] <= c["end_period_ts"] <= r["close_ts"]]
            _write_json_gz(candle_path(out_dir, r["ticker"]), {"ticker": r["ticker"], "candlesticks": cs,
                                                               "endpoint": "/markets/candlesticks"})
    return left


def load_candles(out_dir: Path, ticker: str) -> list | None:
    p = candle_path(out_dir, ticker)
    if not p.exists():
        return None
    return norm_candles(_read_json_gz(p).get("candlesticks"))


class CandleStore:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir

    def get(self, ticker: str):
        return load_candles(self.out_dir, ticker)


# ------------------------------------------------------------------ fetch steps
REASONS = ["eligible", "not_binary", "mve", "not_settled_yes_no", "close_outside_window", "open_lt_3d"]
SOURCES = ["hist", "live"]


def _ticker_key(ticker: str) -> int:
    return int.from_bytes(hashlib.blake2b(ticker.encode("utf-8"), digest_size=8).digest(), "little")


def _page_summary(args) -> tuple:
    """One listing page -> (ticker keys, reason codes, eligible rows). Only eligible markets keep a full row."""
    path, source = args
    keys, codes, rows = [], [], []
    for m in _read_json_gz(path).get("markets") or []:
        r = market_row(m, source)
        keys.append(_ticker_key(r["ticker"]))
        codes.append(REASONS.index(r["reason"] or "eligible"))
        if r["reason"] is None:
            rows.append(r)
    return np.array(keys, dtype=np.uint64), np.array(codes, dtype=np.int8), SOURCES.index(source), rows


def build_market_table(data: Path = DATA) -> tuple[pd.DataFrame, dict]:
    """Eligible markets (first listing wins on a repeated ticker) and the listing counts, each ticker counted once.
    The listing holds tens of millions of markets, so only eligible ones keep a full row."""
    data = Path(data)
    hist_dirs = sorted(d for d in data.glob("hist_markets*") if d.is_dir())
    jobs = [(p, "hist") for d in hist_dirs for p in sorted(d.glob("p*.json.gz"))] + \
           [(p, "live") for p in sorted((data / "live_markets").glob("p*.json.gz"))]
    keys, codes, srcs, rows = [], [], [], []
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2)), mp_context=ctx) as ex:
        for i, (kk, cc, src, rr) in enumerate(ex.map(_page_summary, jobs, chunksize=4)):
            keys.append(kk)
            codes.append(cc)
            srcs.append(np.full(len(kk), src, dtype=np.int8))
            rows.extend(rr)
            if (i + 1) % 1000 == 0:
                log.info("market table: %d / %d pages", i + 1, len(jobs))
    k = np.concatenate(keys) if keys else np.array([], dtype=np.uint64)
    c = np.concatenate(codes) if codes else np.array([], dtype=np.int8)
    s = np.concatenate(srcs) if srcs else np.array([], dtype=np.int8)
    _, first = np.unique(k, return_index=True)            # first occurrence of each ticker in listing order
    cu, su = c[first], s[first]
    summ = {"rows_listed": int(len(k)), "markets_listed": int(len(first)),
            "by_source": {SOURCES[i]: int((su == i).sum()) for i in range(len(SOURCES)) if (su == i).any()},
            "reasons": {REASONS[i]: int((cu == i).sum()) for i in range(len(REASONS)) if (cu == i).any()}}
    cols = list(market_row({}, "hist"))
    eligible = pd.DataFrame(rows, columns=cols).drop_duplicates("ticker", keep="first").reset_index(drop=True)
    return eligible, summ


def load_series(data: Path = DATA) -> dict:
    return {s["ticker"]: s for s in _read_json_gz(data / "series.json.gz")["series"]}


def fetch_series_and_cutoff(api: Api, data: Path) -> None:
    if not (data / "series.json.gz").exists():
        d = api("/series")
        allser = list(d.get("series") or [])
        cur = d.get("cursor")
        while cur:
            d = api("/series", {"cursor": cur})
            allser += d.get("series") or []
            cur = d.get("cursor")
        _write_json_gz(data / "series.json.gz", {"series": allser, "fetched_at": datetime.now(UTC).isoformat()})
        log.info("series: %d", len(allser))
    if not (data / "cutoff.json").exists():
        _write_json(data / "cutoff.json", {**api("/historical/cutoff"), "fetched_at": datetime.now(UTC).isoformat()})


def fetch_listings(api: Api, data: Path) -> None:
    live = ("/markets", {"status": "settled", "mve_filter": "exclude", "limit": 1000}, data / "live_markets", None, None)
    jobs = [live] + [("/historical/markets", {"mve_filter": "exclude", "limit": 1000}, data / s["dir"],
                      s["start_cursor"], s["stop_before_ts"]) for s in hist_segments()]
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {ex.submit(paginate, api, p, q, "markets", d, 50, c, stop): d.name for p, q, d, c, stop in jobs}
        for f in as_completed(futs):
            log.info("listing done: %s %s", futs[f], f.result())


def fetch_event_series(api: Api, events: list[str], data: Path, batch: int = 100) -> dict:
    mp = data / "event_series.json"
    known = json.loads(mp.read_text()) if mp.exists() else {}
    todo = [e for e in events if e not in known]
    log.info("event lookups: %d to go (%d known)", len(todo), len(known))
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        q = {"tickers": ",".join(chunk), "limit": 200}
        while True:
            d = api("/events", q)
            for e in d.get("events") or []:
                known[e["event_ticker"]] = e.get("series_ticker")
            if not d.get("cursor") or not d.get("events"):
                break
            q = {**q, "cursor": d["cursor"]}
        for e in chunk:
            known.setdefault(e, None)
        if (i // batch) % 20 == 0:
            _write_json(mp, known)
            log.info("event lookups: %d / %d", i + len(chunk), len(todo))
    _write_json(mp, known)
    return known


def series_for(kept: pd.DataFrame, event_series: dict, series: dict) -> tuple[pd.Series, int]:
    """Series ticker per kept market: the /events lookup, else the event ticker's prefix if that is a series."""
    out, fallback = [], 0
    for e in kept["event_ticker"]:
        s = event_series.get(e)
        if not s:
            pre = e.split("-")[0]
            s = pre if pre in series else None
            fallback += 1
        out.append(s)
    return pd.Series(out, index=kept.index), fallback


def fetch(data: Path = DATA, workers: int = 4, api=None) -> None:
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    api = api or Api()
    fetch_series_and_cutoff(api, data)
    fetch_listings(api, data)
    et, ls = data / "eligible.parquet", data / "listing_summary.json"
    if not (et.exists() and ls.exists()):
        eligible, summ = build_market_table(data)
        eligible.to_parquet(et, index=False)
        _write_json(ls, summ)
        log.info("market table: %s", summ)
    elig = pd.read_parquet(et)
    k, kept = sample(elig, MAX_MARKETS)
    _write_json(data / "sample.json", {"eligible": len(elig), "k": k, "kept_markets": len(kept),
                                       "kept_events": int(kept["event_ticker"].nunique()),
                                       "events": sorted(kept["event_ticker"].unique())})
    log.info("eligible %d, k %d, kept %d markets", len(elig), k, len(kept))
    series = load_series(data)
    ev = fetch_event_series(api, sorted(kept["event_ticker"].unique()), data)
    kept = kept.copy()
    kept["series_ticker"], _ = series_for(kept, ev, series)
    ok = kept[kept["series_ticker"].map(lambda s: s in series and series[s].get("fee_type") in FEE_TYPES)
              & kept.apply(lambda r: needs_candles({"volume": r["volume"]}), axis=1)]
    cdir = data / "candles"
    cdir.mkdir(exist_ok=True)
    todo = [r for r in ok.to_dict("records") if not candle_path(cdir, r["ticker"]).exists()]
    log.info("candles: %d to fetch (%d kept with a known fee type and lifetime volume >= 100)", len(todo), len(ok))
    live = [r for r in todo if r["source"] == "live"]
    if live:
        left = fetch_candles_batch(api, live, cdir)
        log.info("candles: %d live markets in batches, %d left for one-by-one", len(live) - len(left), len(left))
        todo = [r for r in todo if r["source"] != "live"] + left
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_candles_one, api, r, cdir): r["ticker"] for r in todo}
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:                       # noqa: BLE001 - logged, retried on the next run
                log.warning("candles %s failed: %r", futs[f], e)
            done += 1
            if done % 1000 == 0:
                log.info("candles: %d / %d (calls %d, throttles %d, interval %.3f s)", done, len(todo), api.calls,
                         api.limiter.throttles, api.limiter.interval)
    log.info("fetch complete: %d API calls", api.calls)


# ------------------------------------------------------------------ report
def _fmt(x, pct=True, nd=2, sign=True):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return (f"{100 * x:+.{nd}f}%" if sign else f"{100 * x:.{nd}f}%") if pct else f"{x:.{nd}f}"


def _row(name: str, s: dict) -> str:
    return (f"| {name} | {s['trades']:,} | {s['events']:,} | {_fmt(s['mean'])} | {_fmt(s.get('mean_fee_up'))} | "
            f"{_fmt(s['event_mean'])} | {_fmt(s['t'], pct=False)} | {_fmt(s['win_rate'], nd=1, sign=False)} | "
            f"{_fmt(s.get('mean_price'), pct=False, nd=3)} |")


HEAD = ("| row | trades | events | mean (quadratic fee) | mean (fee rounded up) | event-clustered mean | t (event) | "
        "win rate | mean price |\n|---|---|---|---|---|---|---|---|---|")


def summary_md(res: dict) -> str:
    v, h, c = res["verdict"], res["headline"], res["counts"]
    lines = ["# K1: Kalshi favourite-longshot bias (round 5, section 5)", "",
             f"**Verdict: {'PASS' if v['pass'] else 'FAIL'}.** Out-of-sample event-clustered t = "
             f"{_fmt(h['OOS']['t'], pct=False)} (bar {T_BAR}; the single-test {T_SINGLE} is "
             f"{'cleared' if v['clears_2_33'] else 'not cleared'}).", ""]
    lines += [f"- {b['bar']}: {_fmt(b['value'], pct=not b['is_t'])} -> {'ok' if b['ok'] else 'FAIL'}"
              for b in v["bars"]]
    ve = res["verdict_event_mean"]
    lines += [f"- With event-clustered means in bars 1 and 3 instead (reported, not the rule): "
              f"{'PASS' if ve['pass'] else 'FAIL'}"]
    lines += ["", "## Headline (taker at the ask, quadratic fee, entry 24 h before close)", "", HEAD]
    lines += [_row(p, h[p]) for p in ("IS", "OOS", "ALL")]
    lines += ["", "## Reported only (never selected)", "", "### Price buckets (ask of the side bought)", "", HEAD]
    for name, b in res["buckets"].items():
        lines += [_row(f"{name} {p}", b[p]) for p in ("IS", "OOS")]
    lines += ["", "### Maker version (buy at the bid, no fee)", "", HEAD]
    lines += [_row(f"maker {p}", res["maker"][p]) for p in ("IS", "OOS")]
    lines += ["", "### Entry 72 h before close", "", HEAD]
    lines += [_row(f"72h {p}", res["entry_72h"][p]) for p in ("IS", "OOS")]
    lines += ["", "### By category (series category)", "", HEAD]
    for cat, b in sorted(res["by_category"].items()):
        lines += [_row(f"{cat} {p}", b[p]) for p in ("IS", "OOS") if b[p]["trades"]]
    lines += ["", "## Counts", ""]
    lines += [f"- {k}: {v:,}" if isinstance(v, int) else f"- {k}: {v}" for k, v in c.items()]
    lines += ["", "## Diagnostics (not pre-registered; change no verdict)", "",
              "```", json.dumps(res["diagnostics"], indent=1, default=str), "```", "",
              "## Notes", ""] + [f"- {n}" for n in res["notes"]]
    return "\n".join(lines) + "\n"


NOTES = [
    "close_time is the API's `close_time` field. For markets that can close early and did, Kalshi rewrites it to the"
    " actual close; no field keeps the original schedule, so the entry candle is placed against the realized close.",
    "Window and split in UTC: 2021-07-01T00:00Z <= close_time < 2026-10-30T00:00Z; in-sample close_time <"
    " 2025-05-01T00:00Z.",
    "Settled yes or no = result 'yes'/'no' and status 'finalized'/'settled'.",
    "Non-multivariate = listed with mve_filter=exclude and no mve_collection_ticker (nor a KXMVE ticker).",
    "Archived markets from /historical/markets; settled markets still in the live set from"
    " /markets?status=settled (/events?status=settled drops settled markets whose event still has open ones);"
    " /events?tickers= maps events to series.",
    "Sampling: int(SHA-1 hex digest of the UTF-8 event_ticker) mod k == 0, k = 1, 2, ... until the eligible markets"
    " kept are <= 60,000; counted before the fee-type drop.",
    "Entry candle: the last daily candle (they end at midnight New York time) with end_period_ts <= close_time - 24 h."
    " The 7 candles for the volume rule are the entry candle and the 6 candles before it as the API returns them"
    " (the API skips some days, so the window can be longer than 7 days; counted in diagnostics).",
    "A market whose lifetime volume (volume_fp) is under 100 contracts can't have 100 in any 7 candles, so its"
    " candles weren't fetched and it is counted under volume (volume_market_total) ahead of the other entry"
    " filters; the trade set is the same as fetching them.",
    "Spread = yes_ask close - yes_bid close at the entry candle; a candle without a bid or ask is skipped as"
    " no_quote. Kalshi shows a missing bid as 0.00 and a missing ask as 1.00, which the favourite range excludes.",
    "Fees use each series' current fee_type and fee_multiplier (some series have 0.5 or 0); fee changes over time are"
    " not modelled. Series absent from /series are dropped and counted (series_missing).",
    "The bars' 'mean' is the mean return per contract over trades; the event-clustered mean is reported beside it"
    " (verdict_event_mean in the JSON). t is over event means, ddof 1.",
    "Buckets use the ask of each side on markets that passed the entry filters: [0.50, 0.70) can take both sides of"
    " one market; [0.70, 0.85), [0.85, 0.97]; the longshot side is any ask in (0, 0.30), the mirror of the"
    " favourite rule.",
    "Maker version: the same trades as K1 (side chosen by the ask rule), bought at that side's bid (YES: yes bid; NO:"
    " 1 - yes ask), no fee. Fills not knowable.",
    "72 h entry: the same rule and filters on the last candle ending >= 72 h before close_time.",
]


def report(data: Path = DATA) -> dict:
    data = Path(data)
    elig = pd.read_parquet(data / "eligible.parquet")
    summ = json.loads((data / "listing_summary.json").read_text())
    series = load_series(data)
    ev = json.loads((data / "event_series.json").read_text())
    k, kept = sample(elig, MAX_MARKETS)
    kept = kept.copy()
    kept["series_ticker"], fallback = series_for(kept, ev, series)
    store = CandleStore(data / "candles")
    e24, c24 = build_entries(kept, series, store, 24)
    e72, c72 = build_entries(kept, series, store, 72)
    counts = Counter({"markets_listed": summ["markets_listed"], "listing_rows_before_dedupe": summ["rows_listed"],
                      "markets_listed_historical": summ["by_source"].get("hist", 0),
                      "markets_listed_live": summ["by_source"].get("live", 0),
                      "eligible": len(elig), "k": k, "sampled_markets": len(kept),
                      "sampled_events": int(kept["event_ticker"].nunique()),
                      "series_from_ticker_prefix": fallback})
    for reason, n in summ["reasons"].items():
        counts[f"listed_{reason}"] = int(n)
    for key, n in c24.items():
        counts[f"skip24_{key}" if key not in ("entered", "fee_type_dropped", "fee_type_dropped_series",
                                              "series_missing") else key] = int(n)
    for key in ("no_entry_candle", "settled_before_entry", "no_quote", "spread", "volume", "candles_missing",
                "entered"):
        counts[f"skip72_{key}"] = int(c72.get(key, 0))
    res = analyse(e24, e72, counts)
    res["counts"]["no_side_in_range_24h"] = res.pop("no_side_in_range")
    for p in ("IS", "OOS"):
        res["counts"][f"trades_{p}"] = res["headline"][p]["trades"]
        res["counts"][f"events_{p}"] = res["headline"][p]["events"]
    res["dropped_series"] = e24.attrs.get("dropped_series", {})
    res["series_fee_types"] = dict(Counter(s.get("fee_type") for s in series.values()))
    cut = data / "cutoff.json"
    res["cutoff"] = json.loads(cut.read_text()) if cut.exists() else None
    res["notes"] = NOTES
    res["generated_at"] = datetime.now(UTC).isoformat()
    _write_json(data / "k1_results.json", res)
    (data / "k1_summary.md").write_text(summary_md(res))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fetch", action="store_true", help="resumable pull into research/data/round5/k1/")
    ap.add_argument("--report", action="store_true", help="k1_results.json and k1_summary.md from the cache")
    ap.add_argument("--data", type=Path, default=DATA)
    ap.add_argument("--workers", type=int, default=4, help="candle threads (one shared rate limiter)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not (a.fetch or a.report):
        ap.error("pass --fetch and/or --report")
    if a.fetch:
        fetch(a.data, a.workers)
    if a.report:
        res = report(a.data)
        print(summary_md(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
