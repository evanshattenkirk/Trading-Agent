"""Earnings report times for X1 (research/x1_earnings_fly_prereg.md) from SEC EDGAR: every Form 8-K with Item 2.02
(Results of Operations) for book E's 30 names, with its acceptance time in US Eastern time, the session the report
first trades in, and whether X1 keeps it. Standalone research script; run on the Mac (sec.gov isn't reachable from
the cloud sessions).

    .venv/bin/python research/fetch_earnings_edgar.py            # writes research/data/x1/earnings.csv
    SEC_USER_AGENT="Name email@example.com" .venv/bin/python research/fetch_earnings_edgar.py   # if SEC returns 403

SEC asks automated clients for a User-Agent naming a contact; the default names this repo. Output columns: symbol,
accepted_et, timing (bmo / amc / dmh = filed during market hours), report_session, entry_session, status (kept, or
why not). The time zone of EDGAR's acceptanceDateTime is checked against two anchors (AAPL files after the close, KO
before the open); the script refuses to write a file that fails them. Rules, with amendment 1 (2026-10-07), in
research/x1_earnings_fly_prereg.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nyse_calendar import holidays  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "research" / "data" / "x1" / "earnings.csv"
ET = ZoneInfo("America/New_York")
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
UA = os.environ.get("SEC_USER_AGENT", "AgentDesk research github.com/evanshattenkirk/Trading-Agent")
OPEN, CLOSE = dtime(9, 30), dtime(16, 0)
SPACING_DAYS = 45
FIRST = date(2017, 10, 1)            # a quarter before the sample, so the 45-day spacing starts clean
ANCHORS = {"AAPL": "amc", "KO": "bmo"}           # amendment 1: JPM files its 8-K after its call
# SEC's ticker map points at the current registrant; older filings sit under the earlier one (amendment 1)
EXTRA_CIKS = {"XOM": [34088], "DIS": [1001039]}
FIELDS = ["symbol", "accepted_et", "timing", "report_session", "entry_session", "status"]


def universe() -> list[str]:
    sys.path.insert(0, str(ROOT))
    from agentdesk.config import load_config
    return list(load_config()["crew"]["earnings"]["universe"])


# ------------------------------------------------------------------ pure parts (tested)
def ticker_ciks(js: dict, symbols) -> dict[str, int]:
    """SEC's company_tickers.json ({"0": {"cik_str", "ticker", "title"}, ...}) -> {symbol: CIK} for `symbols`."""
    want = {s.upper() for s in symbols}
    return {v["ticker"].upper(): int(v["cik_str"]) for v in js.values() if v["ticker"].upper() in want}


def columns_to_rows(block: dict) -> list[dict]:
    """EDGAR's columnar filing lists ({"form": [...], "items": [...], ...}) -> one dict per filing."""
    keys = [k for k, v in block.items() if isinstance(v, list)]
    n = len(block[keys[0]]) if keys else 0
    return [{k: block[k][i] for k in keys} for i in range(n)]


def results_filings(rows: list[dict]) -> list[dict]:
    """Form 8-K (not 8-K/A) filings whose items include 2.02."""
    return [r for r in rows if r.get("form") == "8-K"
            and "2.02" in [x.strip() for x in str(r.get("items", "")).split(",")]]


def accepted_et(stamp: str, zone: str) -> datetime:
    """EDGAR's acceptanceDateTime ("2024-08-01T20:31:05.000Z") in Eastern time. zone="utc" reads the stamp as UTC;
    zone="et" reads its clock as Eastern already (the Z then is only a label)."""
    naive = datetime.fromisoformat(stamp.replace("Z", "")[:19])
    if zone == "utc":
        return naive.replace(tzinfo=timezone.utc).astimezone(ET).replace(tzinfo=None)
    if zone == "et":
        return naive
    raise ValueError(zone)


def is_session(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def next_session(d: date) -> date:
    d += timedelta(days=1)
    while not is_session(d):
        d += timedelta(days=1)
    return d


def prev_session(d: date) -> date:
    d -= timedelta(days=1)
    while not is_session(d):
        d -= timedelta(days=1)
    return d


def classify(acc: datetime) -> dict:
    """Timing and sessions for one acceptance time (naive, Eastern). The report session R is the first session whose
    09:30 open comes after the acceptance; an 8-K filed during market hours on session D follows a release made before
    D's open (or after the prior close), so R = D (amendment 1). The entry session is the one before R."""
    d, t = acc.date(), acc.time()
    if is_session(d) and t < OPEN:
        timing, r = "bmo", d
    elif is_session(d) and t < CLOSE:
        timing, r = "dmh", d
    else:
        timing, r = "amc", next_session(d)
    return {"timing": timing, "report_session": r, "entry_session": prev_session(r)}


def space(events: list[dict], days: int = SPACING_DAYS) -> list[dict]:
    """Sorted by acceptance, filings group into clusters that start at a filing and take every later one less than
    `days` calendar days after it; the LAST of each cluster is kept (amendment 1: TSLA's quarterly delivery 8-Ks, and
    any pre-announcement, come before the results release in the same cluster)."""
    evs = sorted(events, key=lambda e: e["accepted_et"])
    out, i = [], 0
    while i < len(evs):
        j = i
        while j + 1 < len(evs) and (evs[j + 1]["accepted_et"] - evs[i]["accepted_et"]).days < days:
            j += 1
        kept = evs[j]["accepted_et"].date()
        for k in range(i, j + 1):
            out.append({**evs[k], "status": "kept" if k == j else f"dropped: same {days}-day cluster as {kept}"})
        i = j + 1
    return out


def events_for(symbol: str, filings: list[dict], zone: str) -> list[dict]:
    rows = []
    for f in results_filings(filings):
        acc = accepted_et(f["acceptanceDateTime"], zone)
        if acc.date() < FIRST:
            continue
        rows.append({"symbol": symbol, "accepted_et": acc, **classify(acc)})
    return space(rows)


def anchor_share(events: list[dict], symbol: str, timing: str) -> float | None:
    got = [e for e in events if e["symbol"] == symbol and not e["status"].startswith("dropped")]
    return sum(e["timing"] == timing for e in got) / len(got) if got else None


def pick_zone(by_zone: dict[str, list[dict]], need: float = 0.80) -> str | None:
    """The acceptance-time reading under which every anchor name has at least `need` of its filings at its known
    timing; None if neither reading does."""
    for zone, events in by_zone.items():
        shares = [anchor_share(events, s, t) for s, t in ANCHORS.items()]
        if all(x is not None and x >= need for x in shares):
            return zone
    return None


def to_csv_rows(events: list[dict]) -> list[dict]:
    return [{"symbol": e["symbol"], "accepted_et": e["accepted_et"].isoformat(timespec="seconds"),
             "timing": e["timing"], "report_session": e["report_session"].isoformat(),
             "entry_session": e["entry_session"].isoformat(), "status": e["status"]} for e in events]


# ------------------------------------------------------------------ network
def get_json(url: str, tries: int = 4) -> dict:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 403:
                raise SystemExit(f"SEC refused {url} (403). Set SEC_USER_AGENT to 'Name contact-email' and rerun.")
            if e.code == 404 or i == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if i == tries - 1:
                raise
        time.sleep(2 ** i)
    raise RuntimeError(url)


def all_filings(cik: int) -> list[dict]:
    """The recent block plus every older page that reaches back to FIRST."""
    js = get_json(SUBMISSIONS_URL.format(name=f"CIK{cik:010d}.json"))
    rows = columns_to_rows(js["filings"]["recent"])
    for page in js["filings"].get("files", []):
        if page.get("filingTo", "9999") < FIRST.isoformat():
            continue
        time.sleep(0.2)
        rows += columns_to_rows(get_json(SUBMISSIONS_URL.format(name=page["name"])))
    return rows


def merge_filings(lists: list[list[dict]]) -> list[dict]:
    """Filings from several CIKs of one company, each accession number once."""
    seen, out = set(), []
    for rows in lists:
        for r in rows:
            key = r.get("accessionNumber") or (r.get("form"), r.get("acceptanceDateTime"))
            if key not in seen:
                seen.add(key)
                out.append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    names = universe()
    ciks = ticker_ciks(get_json(TICKERS_URL), names)
    missing = sorted(set(names) - set(ciks))
    if missing:
        raise SystemExit(f"no CIK for {missing}")
    filings = {}
    for s in names:
        ids = [ciks[s]] + EXTRA_CIKS.get(s, [])
        filings[s] = merge_filings([all_filings(c) for c in ids])
        print(f"{s}: CIK {ids}, {len(results_filings(filings[s]))} 8-K 2.02 filings in total")
        time.sleep(0.2)
    by_zone = {z: [e for s in names for e in events_for(s, filings[s], z)] for z in ("utc", "et")}
    for z, ev in by_zone.items():
        print(f"reading stamps as {z}: " + ", ".join(f"{s} {t} {anchor_share(ev, s, t)}" for s, t in ANCHORS.items()))
    zone = pick_zone(by_zone)
    if zone is None:
        raise SystemExit("neither time-zone reading passes the AAPL/KO anchors; check EDGAR's acceptanceDateTime")
    events = by_zone[zone]
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(to_csv_rows(sorted(events, key=lambda e: (e["symbol"], e["accepted_et"]))))
    kept = [e for e in events if e["status"] == "kept"]
    print(f"stamps read as {zone}; {len(events)} filings, {len(kept)} kept "
          f"({sum(e['timing'] == 'bmo' for e in kept)} bmo, {sum(e['timing'] == 'amc' for e in kept)} amc), "
          f"{sum(e['timing'] == 'dmh' for e in kept)} filed during market hours, "
          f"{sum(e['status'].startswith('dropped') for e in events)} dropped by clustering -> {a.out}")
    for s in names:
        mine = [e for e in kept if e["symbol"] == s]
        print(f"  {s}: {len(mine)} kept ({sum(e['timing'] == 'dmh' for e in mine)} filed during market hours)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
