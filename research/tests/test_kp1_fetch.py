"""KP1 fetchers: MARA Pool blocks (mempool.space), network heights, Coinbase Exchange daily candles."""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kp1_fetch as F  # noqa: E402

POOL = "https://mempool.space/api/v1/mining/pool/marapool/blocks"


def test_quarter_bounds_utc():
    assert F.quarter_bounds("2024Q1") == (1704067200, 1711929600)
    assert F.quarter_bounds("2024Q4") == (1727740800, 1735689600)


def _block(h, ts, reward):
    return {"height": h, "timestamp": ts, "extras": {"reward": reward}}


def test_mara_paging_stops_and_dedupes():
    pages = {
        POOL: [_block(700002, 30, 7), _block(700001, 20, 6)],
        f"{POOL}/700001": [_block(700001, 20, 6), _block(690000, 10, 5)],
        f"{POOL}/690000": [_block(684999, 5, 4), _block(684000, 4, 3)],
    }
    calls = []

    def get(url):
        calls.append(url)
        return pages[url]

    got = F.fetch_mara_blocks(get)
    assert got == [(690000, 10, 5), (700001, 20, 6), (700002, 30, 7)]
    assert calls == [POOL, f"{POOL}/700001", f"{POOL}/690000"]


def test_block_at_boundary_goes_to_new_quarter():
    start, end = F.quarter_bounds("2024Q2")
    blocks = [(1, start - 1, 1), (2, start, 1), (3, end - 1, 1), (4, end, 1)]
    assert [b[0] for b in F.blocks_in_quarter(blocks, "2024Q2")] == [2, 3]


def test_height_at_reads_height():
    def get(url):
        assert url == "https://mempool.space/api/v1/mining/blocks/timestamp/1704067200"
        return {"height": 823785, "hash": "x", "timestamp": "2023-12-31T23:46:12.000Z"}

    assert F.height_at(get, 1704067200) == 823785


def _ts(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def test_coinbase_windows_and_missing_days():
    calls = []

    def get(url):
        calls.append(url)
        if "start=2024-01-01" in url:
            # newest first, as Coinbase returns them; 2024-01-02 is missing
            return [[_ts(date(2024, 1, 3)), 1, 1, 1, 10.0, 2.0], [_ts(date(2024, 1, 1)), 1, 1, 1, 5.0, 3.0]]
        return [[_ts(date(2024, 12, 1)), 1, 1, 1, 4.0, 1.5]]

    got = F.fetch_coinbase_daily(get, "BTC-USD", date(2024, 1, 1), date(2025, 1, 1))
    assert got == [(date(2024, 1, 1), 5.0, 3.0), (date(2024, 1, 3), 10.0, 2.0), (date(2024, 12, 1), 4.0, 1.5)]
    assert len(calls) == 2  # 366 days -> two 300-day windows
    assert all("granularity=86400" in c and "/products/BTC-USD/candles" in c for c in calls)
    assert F.usd_volume(got) == {date(2024, 1, 1): 15.0, date(2024, 1, 3): 20.0, date(2024, 12, 1): 6.0}
