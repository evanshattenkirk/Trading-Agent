"""Download what E1/E2 (research/round5_prereg.md section 3) need: 1-minute NBBO quotes, all strikes within +-30% of
spot, both rights, for each kept earnings event's post-announcement expiry on every session from T-10 to the exit
day, and its pre-announcement expiry from T-10 to the earlier of the exit day and that expiry. Standalone research
script; runs in any cloud session holding THETADATA_API_KEY (environment variable, or the repo .env) with a venv that
has `thetadata pandas pyarrow`.

    .venv/bin/python research/fetch_thetadata_e.py --earnings /path/to/x1/earnings.csv --smoke   # one event, no write
    .venv/bin/python research/fetch_thetadata_e.py --earnings /path/to/x1/earnings.csv           # full resumable pull
    .venv/bin/python research/fetch_thetadata_e.py --earnings ... --retry-failed

Events, D, timing and expiries come from research/round5_e.py. META's options were FB before 2022-06-09. Spot for the
strike band is put-call parity at 15:45 ET on the first session pulled (no band if that fails).

Output: data/thetadata/e/{SYMBOL}_{D}.parquet (quotes plus `session` and `expiry` columns), manifest
data/thetadata/e_manifest.csv, failures data/thetadata/e_failures.csv, expiry lists in data/thetadata/e_expirations/.
Rerunning skips events already on disk or listed as failed.
"""
from __future__ import annotations

import argparse
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
import round5_e as R  # noqa: E402

BASE = ROOT / "data" / "thetadata"
OUT_DIR = BASE / "e"
EXP_DIR = BASE / "e_expirations"
MANIFEST = BASE / "e_manifest.csv"
FAILS = BASE / "e_failures.csv"
EARNINGS = ROOT / "research" / "data" / "x1" / "earnings.csv"
BAND = 0.30
MAN_HEAD = ["symbol", "D", "timing", "root", "post", "pre", "status", "pulls", "rows", "bytes", "fetched_at"]

log = logging.getLogger("fetch_e")


def read_events(path: Path) -> list[dict]:
    with open(path) as f:
        return [e for e in (R.event(r) for r in csv.DictReader(f)) if e is not None]


def plan_event(ev: dict, expirations) -> dict:
    """(expiry, session) pulls for one event. `expirations(root)` returns that root's listed expiries."""
    d, timing = ev["D"], ev["timing"]
    start = R.t_minus(d, 10)
    r = R.root(ev["symbol"], start)
    listed = expirations(r)
    post, pre = R.post_expiry(listed, d, timing), R.pre_expiry(listed, d, timing)
    job = {**ev, "root": r, "post": post, "pre": pre, "pulls": []}
    if post is None:
        return {**job, "skip": "no post-announcement expiry"}
    x = R.exit_day(d, timing)
    job["pulls"] = [(post, s) for s in R.sessions_between(start, x)]
    if pre is not None:
        job["pulls"] += [(pre, s) for s in R.sessions_between(start, min(x, pre))]
    return job


def out_path(job: dict, out_dir: Path = OUT_DIR) -> Path:
    return out_dir / f"{job['symbol']}_{job['D'].isoformat()}.parquet"


def band_spot(df) -> float | None:
    import bd_real_quotes as bd
    try:
        p = bd.QuotePanel(bd.normalize(df))
        return bd.spot(p, R.ENTRY_MINUTE)
    except Exception:  # noqa: BLE001 - no band is the fallback
        return None


def fetch_job(client, job: dict, sleep=time.sleep):
    import pandas as pd
    parts, spot = [], None
    for exp, s in job["pulls"]:
        df = T.fetch_session(client, job["root"], exp, s, sleep=sleep)
        if df is None or len(df) == 0:
            continue
        if spot is None:
            spot = band_spot(df) or 0.0
        if spot:
            df = T.trim(df, spot, BAND)
        parts.append(df.assign(session=s.isoformat(), expiry=exp.isoformat()))
    return pd.concat(parts, ignore_index=True) if parts else None


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--earnings", type=Path, default=EARNINGS)
    ap.add_argument("--smoke", action="store_true", help="plan and fetch the first event's first pull; write nothing")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--pause", type=float, default=0.3)
    ap.add_argument("--retry-failed", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("thetadata").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    events = read_events(a.earnings)
    client = T.make_client()
    cache = T.ExpiryCache(client, EXP_DIR)
    if a.smoke:
        job = plan_event(events[-1], cache)
        print({k: v for k, v in job.items() if k != "pulls"}, f"{len(job['pulls'])} pulls")
        exp, s = job["pulls"][-1]
        df = T.fetch_session(client, job["root"], exp, s)
        print(None if df is None else (len(df), list(df.columns), df.head(3).to_dict("records")))
        return 0
    jobs = [plan_event(e, cache) for e in events]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now = lambda: dt.datetime.now().isoformat(timespec="seconds")
    for j in jobs:
        if "skip" in j:
            T.append_csv(MANIFEST, MAN_HEAD, [j["symbol"], j["D"], j["timing"], j["root"], "", "", f"skipped: {j['skip']}",
                                              0, 0, 0, now()])
    jobs = [j for j in jobs if "skip" not in j]
    failed = set()
    if FAILS.exists() and not a.retry_failed:
        failed = {(r["symbol"], r["D"]) for r in csv.DictReader(FAILS.open())}
    todo = [j for j in jobs if not out_path(j).exists() and (j["symbol"], j["D"].isoformat()) not in failed]
    log.info("%d events: %d to fetch (%d pulls)", len(events), len(todo), sum(len(j["pulls"]) for j in todo))
    t0, n_ok, n_bytes = time.time(), 0, 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = {pool.submit(save_job, client, j, a.pause): j for j in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            j = futs[fut]
            try:
                status, rows, size = fut.result()
                n_ok += status == "ok"
                n_bytes += size
                T.append_csv(MANIFEST, MAN_HEAD, [j["symbol"], j["D"], j["timing"], j["root"], j["post"], j["pre"],
                                                  status, len(j["pulls"]), rows, size, now()])
            except Exception as e:  # noqa: BLE001 - logged; a rerun picks it up
                log.error("%s %s FAILED: %r", j["symbol"], j["D"], e)
                T.append_csv(FAILS, ["symbol", "D", "error", "at"], [j["symbol"], j["D"], repr(e)[:500], now()])
            if i % 10 == 0 or i == len(todo):
                el = time.time() - t0
                log.info("progress %d/%d ok=%d %.0fMB elapsed %.0fmin eta %.0fmin", i, len(todo), n_ok,
                         n_bytes / 1e6, el / 60, el / i * (len(todo) - i) / 60)
    log.info("DONE: %d events saved this run, %.0f MB", n_ok, n_bytes / 1e6)
    return 0


if __name__ == "__main__":
    sys.exit(main())
