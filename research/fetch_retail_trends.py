"""R1 search interest (research/r1_retail_search_prereg.md section 3): Google Trends, United States, all categories,
monthly, 2004-01-01 to 2026-10-31, one request per retailer entity, so each series keeps one scale. Runs anywhere
with network access to trends.google.com and `pip install pytrends pandas`.

    python research/fetch_retail_trends.py      # -> research/data/retail/trends.csv (symbol, entity, month, value)

Rerunning skips entities already in the file. Google samples and rescales Trends data, so a fresh pull differs a
little; the saved file is R1's data of record.
"""
from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "research" / "data" / "retail" / "trends.csv"
TIMEFRAME = "2004-01-01 2026-10-31"
ENTITIES = {  # prereg section 2
    "WMT": "/m/0841v", "COST": "/m/029f32", "HD": "/m/01zj1t", "LOW": "/m/037922", "TGT": "/m/01b39j",
    "BBY": "/m/01zrdx", "DG": "/m/0204_2", "DLTR": "/m/040rs1", "TJX": "/m/097b3s", "ROST": "/m/08950y",
    "KSS": "/m/037x4r", "M": "/m/01pkxd", "ULTA": "/m/03p3r21", "TSCO": "/m/0378_f", "DKS": "/m/06fgv_",
    "BJ": "/m/0797b5", "BURL": "/m/03p5mm", "AZO": "/m/02z6wl", "ORLY": "/m/0h7_x_",
}
FIELDS = ["symbol", "entity", "month", "value"]


def done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with open(path) as f:
        return {r["symbol"] for r in csv.DictReader(f)}


def pull(client, entity: str, tries: int = 6, sleep=time.sleep):
    for i in range(tries):
        try:
            client.build_payload([entity], timeframe=TIMEFRAME, geo="US")
            df = client.interest_over_time()
            if df.empty:
                raise RuntimeError(f"no data for {entity}")
            return df
        except Exception as e:  # pytrends raises plain exceptions on 429
            if i == tries - 1:
                raise
            wait = 30 * (i + 1)
            print(f"  {entity}: {type(e).__name__} {str(e)[:80]}; retry in {wait}s")
            sleep(wait)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    from pytrends.request import TrendReq
    client = TrendReq(hl="en-US", tz=0, timeout=(10, 30))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    have = done(a.out)
    new = not a.out.exists()
    with open(a.out, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for sym, ent in ENTITIES.items():
            if sym in have:
                continue
            df = pull(client, ent)
            col = [c for c in df.columns if c != "isPartial"][0]
            for ts, v in df[col].items():
                w.writerow({"symbol": sym, "entity": ent, "month": ts.strftime("%Y-%m"), "value": int(v)})
            fh.flush()
            print(f"{sym} {ent}: {len(df)} months")
            time.sleep(8)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
