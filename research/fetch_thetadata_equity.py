"""Download 1-minute NBBO quotes for single-stock options, only where book F2's signals fired.

Standalone research script; not part of the engine. Needs THETADATA_API_KEY in the repo .env, a ThetaData plan with
US equity options history (check the plan's history depth covers 2018), and a Python 3.12+ venv with
`thetadata pandas pyarrow`. Input is research/data/f2/signals.csv from `research/f2_real_quotes.py signals`.

    .venv/bin/python research/fetch_thetadata_equity.py --smoke             # first signal: print, write nothing
    .venv/bin/python research/fetch_thetadata_equity.py                     # full resumable pull
    .venv/bin/python research/fetch_thetadata_equity.py --start 2024-01-01  # a slice first, to check cost and size

For each signal: the expiry the paper book would pick (agentdesk/books/f2_spreads.pick_expiry, 5-12 calendar days),
then every session from the entry day through the exit day (f2_spreads.exit_day), all strikes within +-25% of the
signal's spot, calls and puts, 09:30-16:00 ET. Strikes are stored in dollars.

Output: data/thetadata/f2/{SYMBOL}_{day}_{setup}.parquet (the quotes plus a `session` column). Failures go to
data/thetadata/f2_failures.csv; the per-signal stats to data/thetadata/f2_manifest.csv; each symbol's expiry list is
cached in data/thetadata/f2_expirations/. Rerunning skips signals already on disk or listed as failed.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentdesk.books import f2_spreads as S  # noqa: E402
from nyse_calendar import holidays_between  # noqa: E402

BASE = ROOT / "data" / "thetadata"
OUT_DIR = BASE / "f2"
EXP_DIR = BASE / "f2_expirations"
FAIL_CSV = BASE / "f2_failures.csv"
MANIFEST_CSV = BASE / "f2_manifest.csv"
SIGNALS = ROOT / "research" / "data" / "f2" / "signals.csv"
BAND = 0.25                             # keep strikes within +-25% of spot

log = logging.getLogger("fetch_f2")
_csv_lock = threading.Lock()


def f2_cfg() -> dict:
    from agentdesk.config import load_config
    return load_config()["books"]["F2_debit_spreads"]


def read_signals(path: Path) -> list[dict]:
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["spot"] = float(r["spot"])
        r["symbol"] = r["symbol"].upper()
    return rows


def out_path(sig: dict, out_dir: Path = OUT_DIR) -> Path:
    return out_dir / f"{sig['symbol']}_{sig['day']}_{sig['setup']}.parquet"


def plan(signals: list[dict], expirations, cfg: dict) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Jobs for the signals: (jobs, skipped). `expirations(symbol)` returns the listed expiry dates."""
    jobs, skipped = [], []
    for s in signals:
        d0 = dt.date.fromisoformat(s["day"])
        exp = S.pick_expiry(expirations(s["symbol"]), d0, cfg)
        if exp is None:
            skipped.append((s, f"no expiry {cfg['dte'][0]}-{cfg['dte'][1]} days out"))
            continue
        hol = holidays_between(d0.year, d0.year + 1)
        x = S.exit_day(d0, s["setup"], hol, cfg)
        if x >= exp:
            skipped.append((s, f"exit {x} on or after expiry {exp}"))
            continue
        sessions, d = [], d0
        while d <= x:
            if S.is_session(d, hol):
                sessions.append(d)
            d += dt.timedelta(days=1)
        jobs.append({**s, "expiry": exp, "sessions": sessions})
    return jobs, skipped


def trim(df, spot: float, band: float = BAND):
    """Strikes in dollars (ThetaData's older API sent thousandths), kept within +-band of spot."""
    col = next(c for c in df.columns if c.lower() == "strike")
    k = df[col].astype(float)
    if len(k) and k.median() > 20 * spot:
        k = k / 1000.0
    df = df.assign(**{col: k})
    return df[(k >= spot * (1 - band)) & (k <= spot * (1 + band))]


def _no_data(e: Exception) -> bool:
    return type(e).__name__ == "NoDataFoundError"


def _transient(e: Exception) -> bool:
    if isinstance(e, (ConnectionError, TimeoutError)):
        return True
    try:
        import grpc
    except ImportError:
        return False
    return isinstance(e, grpc.RpcError) and e.code() in {
        grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.INTERNAL, grpc.StatusCode.UNKNOWN, grpc.StatusCode.ABORTED}


def fetch_session(client, symbol: str, expiry: dt.date, day: dt.date, retries: int = 6, sleep=time.sleep):
    """1-minute quotes 09:30-16:00 ET, all strikes, both rights, for one expiry on one session. None if no data."""
    delay = 5.0
    for attempt in range(1, retries + 1):
        try:
            return client.option_history_quote(symbol=symbol, expiration=expiry, date=day, interval="1m",
                                               strike="*", right="both", start_time="09:30:00",
                                               end_time="16:00:00")
        except Exception as e:  # noqa: BLE001 - classified below
            if _no_data(e):
                return None
            if not _transient(e) or attempt == retries:
                raise
            log.warning("%s %s attempt %d: %r; retrying in %.0fs", symbol, day, attempt, e, delay)
        sleep(delay)
        delay = min(delay * 2, 300)
    return None


def fetch_job(client, job: dict, sleep=time.sleep):
    """Every session of one signal, trimmed and tagged with `session`; None when the entry day has no quotes."""
    import pandas as pd
    parts = []
    for d in job["sessions"]:
        df = fetch_session(client, job["symbol"], job["expiry"], d, sleep=sleep)
        if df is None or len(df) == 0:
            if d == job["sessions"][0]:
                return None
            continue
        parts.append(trim(df, job["spot"]).assign(session=d.isoformat()))
    return pd.concat(parts, ignore_index=True) if parts else None


def save_job(client, job: dict, pause: float, out_dir: Path = OUT_DIR) -> tuple[str, int, int]:
    """Returns (status, rows, bytes). Writes atomically so a killed run never leaves a partial file."""
    t0 = time.time()
    df = fetch_job(client, job)
    time.sleep(pause)
    if df is None or len(df) == 0:
        return "no_data", 0, 0
    path = out_path(job, out_dir)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)
    size = path.stat().st_size
    log.info("%s %s %s exp %s: %d sessions, rows=%d size=%.1fMB %.1fs", job["symbol"], job["day"], job["setup"],
             job["expiry"], df["session"].nunique(), len(df), size / 1e6, time.time() - t0)
    return "ok", len(df), size


class ExpiryCache:
    """Each symbol's listed expiries, fetched once and kept on disk (they only grow; delete the folder to refresh)."""

    def __init__(self, client, folder: Path = EXP_DIR):
        self.client, self.folder, self.mem = client, folder, {}
        self._lock = threading.Lock()

    def __call__(self, symbol: str) -> set[dt.date]:
        with self._lock:
            if symbol in self.mem:
                return self.mem[symbol]
            path = self.folder / f"{symbol}.json"
            if path.exists():
                got = {dt.date.fromisoformat(x) for x in json.loads(path.read_text())}
            else:
                import pandas as pd
                df = self.client.option_list_expirations(symbol)
                col = "expiration" if "expiration" in df.columns else df.columns[-1]
                got = {pd.Timestamp(x).date() for x in df[col]}
                self.folder.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(sorted(x.isoformat() for x in got)))
            self.mem[symbol] = got
            return got


def append_csv(path: Path, header: list[str], row: list) -> None:
    with _csv_lock:
        new = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(header)
            w.writerow(row)


def make_client():
    from thetadata import ThetaClient
    return ThetaClient(dataframe_type="pandas", dotenv_path=ROOT / ".env")


def smoke(client, jobs: list[dict]) -> None:
    print("options subscription:", client.options_subscription)
    if not jobs:
        print("no signals to fetch")
        return
    j = jobs[0]
    print(f"{j['symbol']} {j['day']} {j['setup']}: expiry {j['expiry']}, sessions {[str(d) for d in j['sessions']]}")
    t0 = time.time()
    df = fetch_job(client, j)
    print(f"fetched in {time.time() - t0:.1f}s")
    if df is None:
        print("no data")
        return
    import pandas as pd
    pd.set_option("display.width", 200, "display.max_columns", 30)
    print("rows:", len(df), "\n", df.dtypes, "\n", df.head(8))
    col = next(c for c in df.columns if c.lower() == "strike")
    print(f"strikes kept: {df[col].nunique()} from {df[col].min()} to {df[col].max()} (spot {j['spot']})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--smoke", action="store_true", help="fetch the first signal and print it; write nothing")
    ap.add_argument("--signals", type=Path, default=SIGNALS)
    ap.add_argument("--start", type=dt.date.fromisoformat, default=None, help="first signal day to fetch")
    ap.add_argument("--end", type=dt.date.fromisoformat, default=None, help="last signal day to fetch")
    ap.add_argument("--workers", type=int, default=2, help="concurrent requests")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds to sleep after each signal")
    ap.add_argument("--retry-failed", action="store_true", help="also retry signals listed in the failures CSV")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("thetadata").setLevel(logging.WARNING)  # its INFO log dumps the auth response
    logging.getLogger("httpx").setLevel(logging.WARNING)

    sigs = [s for s in read_signals(args.signals)
            if (args.start is None or s["day"] >= str(args.start)) and (args.end is None or s["day"] <= str(args.end))]
    client = make_client()
    jobs, skipped = plan(sigs, ExpiryCache(client), f2_cfg())
    log.info("%d signals: %d to fetch, %d skipped before fetching", len(sigs), len(jobs), len(skipped))
    if args.smoke:
        smoke(client, jobs)
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for s, why in skipped:
        append_csv(MANIFEST_CSV, ["symbol", "day", "setup", "expiry", "status", "rows", "bytes", "fetched_at"],
                   [s["symbol"], s["day"], s["setup"], "", f"skipped: {why}", 0, 0,
                    dt.datetime.now().isoformat(timespec="seconds")])
    failed_before = set()
    if FAIL_CSV.exists() and not args.retry_failed:
        failed_before = {(r["symbol"], r["day"], r["setup"]) for r in csv.DictReader(FAIL_CSV.open())}
    todo = [j for j in jobs if not out_path(j).exists() and (j["symbol"], j["day"], j["setup"]) not in failed_before]
    log.info("%d already on disk or failed before, %d to fetch (%d sessions)", len(jobs) - len(todo), len(todo),
             sum(len(j["sessions"]) for j in todo))

    t0, n_ok, n_bytes = time.time(), 0, 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(save_job, client, j, args.pause): j for j in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            j = futs[fut]
            try:
                status, rows, size = fut.result()
                n_ok += status == "ok"
                n_bytes += size
                append_csv(MANIFEST_CSV, ["symbol", "day", "setup", "expiry", "status", "rows", "bytes", "fetched_at"],
                           [j["symbol"], j["day"], j["setup"], j["expiry"].isoformat(), status, rows, size,
                            dt.datetime.now().isoformat(timespec="seconds")])
            except Exception as e:  # noqa: BLE001 - log and keep going; rerun picks it up
                log.error("%s %s %s FAILED: %r", j["symbol"], j["day"], j["setup"], e)
                append_csv(FAIL_CSV, ["symbol", "day", "setup", "error", "at"],
                           [j["symbol"], j["day"], j["setup"], repr(e)[:500],
                            dt.datetime.now().isoformat(timespec="seconds")])
            if i % 25 == 0 or i == len(todo):
                el = time.time() - t0
                log.info("progress %d/%d ok=%d %.0fMB elapsed %.0fmin eta %.0fmin", i, len(todo), n_ok,
                         n_bytes / 1e6, el / 60, el / i * (len(todo) - i) / 60)
    log.info("DONE in %.1f min: %d signals saved this run, %.0f MB", (time.time() - t0) / 60, n_ok, n_bytes / 1e6)
    return 0


if __name__ == "__main__":
    sys.exit(main())
