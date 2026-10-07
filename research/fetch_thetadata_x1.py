"""Download what X1 (research/x1_earnings_fly_prereg.md) needs: 1-minute NBBO quotes for each kept earnings event's
expiry on its entry and exit sessions, and Alpaca daily bars for the date sanity check. Standalone research script;
run on the Mac after research/fetch_earnings_edgar.py. Needs THETADATA_API_KEY (and Alpaca keys for --daily) in the
repo .env and a venv with `thetadata pandas pyarrow`.

    .venv/bin/python research/fetch_thetadata_x1.py --coverage    # does the plan reach back to 2018? writes nothing
    .venv/bin/python research/fetch_thetadata_x1.py --daily       # research/data/x1/daily.csv from Alpaca
    .venv/bin/python research/fetch_thetadata_x1.py               # full resumable quote pull

Per event: the first listed expiry on or after the exit session; all strikes, both rights; 15:30-16:00 ET on the
entry session and 09:30-16:00 ET on the exit session. META's options were FB before 2022-06-09.

Output: data/thetadata/x1/{SYMBOL}_{exit session}.parquet (quotes plus a `session` column), manifest
data/thetadata/x1_manifest.csv, failures data/thetadata/x1_failures.csv, expiry lists in data/thetadata/x1_expirations/.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_thetadata_equity as T  # noqa: E402
from fetch_earnings_edgar import next_session, universe  # noqa: E402

BASE = ROOT / "data" / "thetadata"
OUT_DIR = BASE / "x1"
EXP_DIR = BASE / "x1_expirations"
MANIFEST = BASE / "x1_manifest.csv"
FAILS = BASE / "x1_failures.csv"
EARNINGS = ROOT / "research" / "data" / "x1" / "earnings.csv"
DAILY = ROOT / "research" / "data" / "x1" / "daily.csv"
FB_UNTIL = dt.date(2022, 6, 9)          # first session META's options traded under META
COVERAGE_NAMES = ["AAPL", "NVDA", "JPM", "KO", "META"]
MAN_HEAD = ["symbol", "exit_session", "root", "expiry", "status", "rows", "bytes", "fetched_at"]

log = logging.getLogger("fetch_x1")


def root(symbol: str, day: dt.date) -> str:
    return "FB" if symbol == "META" and day < FB_UNTIL else symbol


def read_events(path: Path = EARNINGS) -> list[dict]:
    with open(path) as f:
        rows = [r for r in csv.DictReader(f) if r["status"] == "kept"]
    return [{"symbol": r["symbol"], "entry": dt.date.fromisoformat(r["entry_session"]),
             "exit": dt.date.fromisoformat(r["report_session"])} for r in rows]


def pick_expiry(listed, exit_day: dt.date) -> dt.date | None:
    later = [e for e in listed if e >= exit_day]
    return min(later) if later else None


def plan(events: list[dict], expirations) -> tuple[list[dict], list[tuple[dict, str]]]:
    jobs, skipped = [], []
    for e in events:
        r = root(e["symbol"], e["entry"])
        exp = pick_expiry(expirations(r), e["exit"])
        if exp is None:
            skipped.append((e, "no expiry on or after the exit session"))
            continue
        jobs.append({**e, "root": r, "expiry": exp})
    return jobs, skipped


def out_path(job: dict, out_dir: Path = OUT_DIR) -> Path:
    return out_dir / f"{job['symbol']}_{job['exit'].isoformat()}.parquet"


def fetch_job(client, job: dict, sleep=time.sleep):
    """Both sessions, tagged with `session`; None when the entry session has no quotes."""
    import pandas as pd
    a = T.fetch_session(client, job["root"], job["expiry"], job["entry"], sleep=sleep,
                        start_time="15:30:00", end_time="16:00:00")
    if a is None or len(a) == 0:
        return None
    parts = [a.assign(session=job["entry"].isoformat())]
    b = T.fetch_session(client, job["root"], job["expiry"], job["exit"], sleep=sleep)
    if b is not None and len(b):
        parts.append(b.assign(session=job["exit"].isoformat()))
    return pd.concat(parts, ignore_index=True)


def save_job(client, job: dict, pause: float, out_dir: Path = OUT_DIR) -> tuple[str, int, int]:
    df = fetch_job(client, job)
    time.sleep(pause)
    if df is None or len(df) == 0:
        return "no_data", 0, 0
    path = out_path(job, out_dir)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)
    return "ok", len(df), path.stat().st_size


def coverage(client, events: list[dict]) -> None:
    """One 15:45 minute per name per year, on that year's first kept event (else 1 July's next session)."""
    print("options subscription:", client.options_subscription)
    cache = T.ExpiryCache(client, EXP_DIR)
    for s in COVERAGE_NAMES:
        cells = []
        for y in range(2018, 2027):
            mine = [e for e in events if e["symbol"] == s and e["entry"].year == y]
            day = mine[0]["entry"] if mine else next_session(dt.date(y, 6, 30))
            r = root(s, day)
            exp = pick_expiry(cache(r), next_session(day))
            if exp is None:
                cells.append(f"{y}: no expiry")
                continue
            df = T.fetch_session(client, r, exp, day, start_time="15:45:00", end_time="15:46:00")
            n = 0 if df is None else len(df)
            cells.append(f"{y}: {n} rows")
        print(f"{s}: " + ", ".join(cells))


def daily(out: Path = DAILY) -> None:
    from agentdesk.config import _load_dotenv
    from agentdesk.feeds.f_data import alpaca_bars
    _load_dotenv(ROOT / ".env")
    names = universe()
    end = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    bars = asyncio.run(alpaca_bars(names, "1Day", "2017-10-01", end, feed="sip"))
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "date", "open", "close"])
        for s in sorted(bars):
            for b in bars[s]:
                w.writerow([s, b["t"][:10], b["o"], b["c"]])
    print(f"daily bars: {', '.join(f'{s} {len(v)}' for s, v in sorted(bars.items()))} -> {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coverage", action="store_true", help="print the plan's reach for five names; write nothing")
    ap.add_argument("--daily", action="store_true", help="only fetch Alpaca daily bars for the sanity check")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--pause", type=float, default=0.5)
    ap.add_argument("--retry-failed", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("thetadata").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.daily:
        daily()
        return 0
    events = read_events() if EARNINGS.exists() else []
    client = T.make_client()
    if a.coverage:
        coverage(client, events)
        return 0
    if not events:
        raise SystemExit(f"{EARNINGS} missing; run research/fetch_earnings_edgar.py first")
    jobs, skipped = plan(events, T.ExpiryCache(client, EXP_DIR))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now = lambda: dt.datetime.now().isoformat(timespec="seconds")
    for e, why in skipped:
        T.append_csv(MANIFEST, MAN_HEAD, [e["symbol"], e["exit"], "", "", f"skipped: {why}", 0, 0, now()])
    failed = set()
    if FAILS.exists() and not a.retry_failed:
        failed = {(r["symbol"], r["exit_session"]) for r in csv.DictReader(FAILS.open())}
    todo = [j for j in jobs if not out_path(j).exists() and (j["symbol"], j["exit"].isoformat()) not in failed]
    log.info("%d events: %d skipped, %d on disk or failed before, %d to fetch", len(events), len(skipped),
             len(jobs) - len(todo), len(todo))
    t0, n_ok, n_bytes = time.time(), 0, 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = {pool.submit(save_job, client, j, a.pause): j for j in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            j = futs[fut]
            try:
                status, rows, size = fut.result()
                n_ok += status == "ok"
                n_bytes += size
                T.append_csv(MANIFEST, MAN_HEAD, [j["symbol"], j["exit"], j["root"], j["expiry"], status, rows, size,
                                                  now()])
            except Exception as e:  # noqa: BLE001 - logged; a rerun picks it up
                log.error("%s %s FAILED: %r", j["symbol"], j["exit"], e)
                T.append_csv(FAILS, ["symbol", "exit_session", "error", "at"],
                             [j["symbol"], j["exit"], repr(e)[:500], now()])
            if i % 25 == 0 or i == len(todo):
                el = time.time() - t0
                log.info("progress %d/%d ok=%d %.0fMB elapsed %.0fmin eta %.0fmin", i, len(todo), n_ok,
                         n_bytes / 1e6, el / 60, el / i * (len(todo) - i) / 60)
    log.info("DONE: %d events saved this run, %.0f MB", n_ok, n_bytes / 1e6)
    return 0


if __name__ == "__main__":
    sys.exit(main())
