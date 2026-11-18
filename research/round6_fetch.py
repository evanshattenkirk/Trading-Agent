"""Round 6 pulls (research/round6_prereg.md): Kalshi events, markets and minute candles for PM1-PM5, Coinbase BTC-USD
1-minute candles for PM3/PM4, and the Cleveland Fed monthly nowcast file for PM5. Public endpoints, no account.
Everything lands in research/data/round6/ (gitignored) and every step skips what is already on disk.

    .venv/bin/python research/round6_fetch.py events            # /events for every series used
    .venv/bin/python research/round6_fetch.py markets  SERIES   # markets of each event (S&P/Nasdaq/CPI: all; BTC: sample)
    .venv/bin/python research/round6_fetch.py coinbase          # BTC-USD minutes around every sampled hour
    .venv/bin/python research/round6_fetch.py candles  SERIES   # minute candles around each entry time
    .venv/bin/python research/round6_fetch.py nowcast           # Cleveland Fed nowcast_month.json

Candle windows (ET for the index series): KXINX / KXNASDAQ100 09:54-15:01 on the close day (entries 10:00, 12:00,
14:00, 15:00); KXBTCD / KXBTC close-51 min to close-4 min (entries close-45, -15, -5); KXCPI the two entry days
(15:00 ET one and two weekdays before the close), 14:54-15:01 each.
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from round5_k1 import Api, Limiter, NotFound, _read_json_gz, _write_json_gz, event_hash  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "research" / "data" / "round6"
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
SERIES = ("KXINX", "KXNASDAQ100", "KXBTCD", "KXBTC", "KXCPI")
SAMPLE_END = int(datetime(2026, 11, 14, 5, tzinfo=UTC).timestamp())   # closes on or before 2026-11-13 (ET)
BTC_CAP = 1500
HIST_CUTOFF = int(datetime(2026, 9, 18, tzinfo=UTC).timestamp())   # GET /historical/cutoff, 2026-11-18
NOWCAST_URL = "https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast_month.json?sc_lang=en"
COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles"

log = logging.getLogger("round6")


def ts(s: str | None) -> int | None:
    return None if not s else int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def et_ts(day, hh: int, mm: int) -> int:
    return int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET).timestamp())


def prev_weekday(day, n: int = 1):
    d = day
    while n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def choose_k(events: list[str], cap: int) -> int:
    hs = [event_hash(e) for e in events]
    k = 1
    while sum(1 for h in hs if h % k == 0) > cap:
        k += 1
    return k


def btc_sample(events: list[dict]) -> list[str]:
    """PM3's hours: KXBTCD events closing on or before the sample end, SHA-1 sampled to at most 1,500."""
    ok = [e["event_ticker"] for e in events if e.get("_close") and e["_close"] <= SAMPLE_END]
    k = choose_k(ok, BTC_CAP)
    return sorted(e for e in ok if event_hash(e) % k == 0)


def candle_windows(series: str, close_ts: int) -> list[tuple[int, int]]:
    if series in ("KXINX", "KXNASDAQ100"):
        day = datetime.fromtimestamp(close_ts, ET).date()
        return [(et_ts(day, 9, 54), et_ts(day, 15, 1))]
    if series in ("KXBTCD", "KXBTC"):
        return [(close_ts - 51 * 60, close_ts - 4 * 60)]
    if series == "KXCPI":
        day = datetime.fromtimestamp(close_ts, ET).date()
        return [(et_ts(prev_weekday(day, n), 14, 54), et_ts(prev_weekday(day, n), 15, 1)) for n in (2, 1)]
    raise ValueError(series)


# ------------------------------------------------------------------ steps
class Fetch:
    def __init__(self, threads: int = 6):
        self.api = Api(limiter=Limiter(interval=0.15, lo=0.1), in_flight=threads)
        self.threads = threads
        DATA.mkdir(parents=True, exist_ok=True)

    def events(self):
        for s in SERIES:
            out = DATA / f"events_{s}.json.gz"
            if out.exists():
                continue
            evs, cur = [], None
            while True:
                p = {"series_ticker": s, "limit": 200}
                if cur:
                    p["cursor"] = cur
                d = self.api("/events", p)
                evs += d.get("events", [])
                cur = d.get("cursor")
                if not cur or not d.get("events"):
                    break
            _write_json_gz(out, evs)
            log.info("%s: %d events", s, len(evs))

    def _markets_of(self, et: str) -> list[dict]:
        ms = []
        for path in ("/historical/markets", "/markets"):
            cur = None
            while True:
                p = {"event_ticker": et, "limit": 1000}
                if cur:
                    p["cursor"] = cur
                try:
                    d = self.api(path, p)
                except NotFound:
                    break
                ms += d.get("markets", [])
                cur = d.get("cursor")
                if not cur or not d.get("markets"):
                    break
            if ms:      # an event's markets are either all archived or all live
                break
        seen, out = set(), []
        for m in ms:
            if m["ticker"] not in seen:
                seen.add(m["ticker"])
                out.append(m)
        return out

    def event_list(self, series: str) -> list[str]:
        evs = _read_json_gz(DATA / f"events_{series}.json.gz")
        if series == "KXBTCD":
            return self.btc_events()
        if series == "KXBTC":
            return [btc_range_event(e) for e in self.btc_events()]
        # weekly (INXW, NASDAQ100W) and monthly (INXM, NASDAQ100M) events are not daily markets
        return sorted(e["event_ticker"] for e in evs if not e["event_ticker"].split("-")[0].endswith(("W", "M")))

    def btc_events(self) -> list[str]:
        f = DATA / "btc_sample.json"
        if f.exists():
            return json.loads(f.read_text())["events"]
        evs = _read_json_gz(DATA / "events_KXBTCD.json.gz")
        # events don't carry close times; the ticker does: KXBTCD-26NOV1717 = 2026-11-17 17:00 ET
        for e in evs:
            e["_close"] = btc_close(e["event_ticker"])
        ok = [e["event_ticker"] for e in evs if e["_close"] and e["_close"] <= SAMPLE_END]
        k = choose_k(ok, BTC_CAP)
        keep = sorted(e for e in ok if event_hash(e) % k == 0)
        f.write_text(json.dumps({"k": k, "eligible": len(ok), "events": keep}, indent=1))
        return keep

    def markets(self, series: str):
        d = DATA / "markets" / series
        d.mkdir(parents=True, exist_ok=True)
        todo = [e for e in self.event_list(series) if not (d / f"{e}.json.gz").exists()]
        log.info("%s markets: %d events to fetch", series, len(todo))
        with ThreadPoolExecutor(self.threads) as ex:
            futs = {ex.submit(self._markets_of, e): e for e in todo}
            for i, f in enumerate(as_completed(futs)):
                _write_json_gz(d / f"{futs[f]}.json.gz", f.result())
                if i % 200 == 0:
                    log.info("%s markets %d/%d (calls %d, 429s %d)", series, i, len(todo), self.api.calls,
                             self.api.limiter.throttles)

    def _candles(self, series: str, m: dict) -> list:
        close = ts(m.get("close_time"))
        out = []
        for a, b in candle_windows(series, close):
            p = {"start_ts": a, "end_ts": b, "period_interval": 1}
            raw = None
            paths = [f"/historical/markets/{m['ticker']}/candlesticks",
                     f"/series/{series}/markets/{m['ticker']}/candlesticks"]
            if close >= HIST_CUTOFF:    # settled after the archive cutoff: still in the live set
                paths.reverse()
            for path in paths:
                try:
                    r = self.api(path, p)
                except NotFound:
                    continue
                if r.get("candlesticks"):
                    raw = r["candlesticks"]
                    break
            out += raw or []
        return out

    def candles(self, series: str, only=None):
        md = DATA / "markets" / series
        cd = DATA / "candles" / series
        cd.mkdir(parents=True, exist_ok=True)
        jobs = []
        for f in sorted(md.glob("*.json.gz")):
            for m in _read_json_gz(f):
                if m.get("result") not in ("yes", "no") or not m.get("close_time"):
                    continue
                if ts(m["close_time"]) > SAMPLE_END:
                    continue
                if only and not only(m):
                    continue
                if not (cd / f"{m['ticker']}.json.gz").exists():
                    jobs.append(m)
        log.info("%s candles: %d markets to fetch", series, len(jobs))
        with ThreadPoolExecutor(self.threads) as ex:
            futs = {ex.submit(self._candles, series, m): m for m in jobs}
            for i, f in enumerate(as_completed(futs)):
                _write_json_gz(cd / f"{futs[f]['ticker']}.json.gz", f.result())
                if i % 500 == 0:
                    log.info("%s candles %d/%d (calls %d, 429s %d, interval %.2f)", series, i, len(jobs),
                             self.api.calls, self.api.limiter.throttles, self.api.limiter.interval)

    def coinbase(self):
        """BTC-USD 1-minute candles for [close - 1,500 min, close - 4 min] of every sampled hour (300 per call)."""
        d = DATA / "coinbase"
        d.mkdir(parents=True, exist_ok=True)
        need = set()
        for e in self.btc_events():
            c = btc_close(e)
            start = (c - 1500 * 60) // 18000 * 18000
            for a in range(start, c, 18000):
                need.add(a)
        todo = sorted(a for a in need if not (d / f"{a}.json.gz").exists())
        log.info("coinbase: %d chunks", len(todo))

        def one(a):
            iso = lambda t: datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
            url = f"{COINBASE}?granularity=60&start={iso(a)}&end={iso(a + 18000 - 60)}"
            for attempt in range(10):
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "agentdesk-research/1"})
                    with urllib.request.urlopen(req, timeout=60) as r:
                        return json.load(r)
                except Exception as ex:  # noqa: BLE001
                    log.warning("coinbase retry %s: %r", a, ex)
                    time.sleep(1.5 * (attempt + 1))
            raise RuntimeError(url)

        with ThreadPoolExecutor(4) as ex:
            futs = {ex.submit(one, a): a for a in todo}
            for i, f in enumerate(as_completed(futs)):
                _write_json_gz(d / f"{futs[f]}.json.gz", f.result())
                time.sleep(0.05)
                if i % 500 == 0:
                    log.info("coinbase %d/%d", i, len(todo))

    def nowcast(self):
        out = DATA / "nowcast_month.json.gz"
        if out.exists():
            return
        req = urllib.request.Request(NOWCAST_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
        with gzip.open(out, "wb") as f:
            f.write(body)


def btc_range_event(above_event: str) -> str:
    """PM4's pair of a PM3 hour: KXBTCD-26NOV1717 -> KXBTC-26NOV1717, BTCD-24AUG0717 -> BTC-24AUG0717."""
    pre, suf = above_event.split("-", 1)
    return {"KXBTCD": "KXBTC", "BTCD": "BTC"}[pre] + "-" + suf


MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def btc_close(event_ticker: str) -> int | None:
    """KXBTCD-26NOV1717 -> 2026-11-17 17:00 ET (the hour the market settles)."""
    try:
        suf = event_ticker.split("-", 1)[1]
        yy, mon, dd, hh = int(suf[:2]), MONTHS[suf[2:5]], int(suf[5:7]), int(suf[7:9])
        return int(datetime(2000 + yy, mon, dd, hh, tzinfo=ET).timestamp())
    except (IndexError, KeyError, ValueError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["events", "markets", "candles", "coinbase", "nowcast"])
    ap.add_argument("series", nargs="?")
    ap.add_argument("--threads", type=int, default=6)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    f = Fetch(a.threads)
    if a.step == "candles" and a.series in ("KXBTCD", "KXBTC"):
        # PM3/PM4 only quote the markets nearest spot: research/round6.py btc-wanted writes the list
        want = set(json.loads((DATA / "btc_wanted.json").read_text())[a.series])
        f.candles(a.series, only=lambda m: m["ticker"] in want)
    elif a.step == "candles" and a.series == "KXNASDAQ100":
        # Nasdaq is only PM1's confirmation sample, which reads `between` markets only
        f.candles(a.series, only=lambda m: m.get("strike_type") == "between")
    elif a.step in ("markets", "candles"):
        getattr(f, a.step)(a.series)
    else:
        getattr(f, a.step)()


if __name__ == "__main__":
    main()
