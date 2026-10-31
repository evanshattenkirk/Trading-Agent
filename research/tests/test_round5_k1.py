"""Tests for research/round5_k1.py (K1, Kalshi favourite-longshot bias; rules in research/round5_prereg.md section 5).

Synthetic data only: no network. A fake `get(path, params)` stands in for the Kalshi API.
"""
import gzip
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH.parent))

import round5_k1 as k1  # noqa: E402

DAY = 86400


def ts(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def mkt(**kw) -> dict:
    m = {"ticker": "SER-EV1-A", "event_ticker": "SER-EV1", "market_type": "binary", "status": "finalized",
         "result": "yes", "open_time": "2024-01-01T00:00:00Z", "close_time": "2024-01-10T00:00:00Z",
         "settlement_ts": "2024-01-10T01:00:00Z", "can_close_early": False}
    m.update(kw)
    return m


def hcandle(end: int, bid, ask, vol: float) -> dict:
    """/historical/markets/{t}/candlesticks format."""
    q = lambda v: None if v is None else f"{v:.4f}"
    return {"end_period_ts": end, "open_interest": "0.00", "volume": f"{vol:.2f}",
            "price": {"close": None, "high": None, "low": None, "mean": None, "open": None, "previous": None},
            "yes_ask": {"close": q(ask), "high": "1.0000", "low": q(ask), "open": "1.0000"},
            "yes_bid": {"close": q(bid), "high": q(bid), "low": "0.0000", "open": "0.0000"}}


def lcandle(end: int, bid, ask, vol: float) -> dict:
    """/series/{s}/markets/{t}/candlesticks format (live data set)."""
    q = lambda v: None if v is None else f"{v:.4f}"
    return {"end_period_ts": end, "open_interest_fp": "0.00", "volume_fp": f"{vol:.2f}", "price": {},
            "yes_ask": {"close_dollars": q(ask), "high_dollars": "1.0000", "low_dollars": q(ask), "open_dollars": "1"},
            "yes_bid": {"close_dollars": q(bid), "high_dollars": q(bid), "low_dollars": "0", "open_dollars": "0"}}


# Daily candles end at 04:00 UTC (midnight New York in summer time, which is all the tests need).
CLOSE = "2024-01-10T00:00:00Z"
ENDS = [ts("2024-01-02T04:00:00Z") + i * DAY for i in range(8)]          # Jan 2 .. Jan 9, 04:00 UTC


def week(bid=0.20, ask=0.24, vol=20.0):
    return k1.norm_candles([hcandle(e, bid, ask, vol) for e in ENDS])


# ---------------------------------------------------------------- market eligibility

def test_an_ordinary_settled_binary_market_is_eligible():
    assert k1.eligibility_reason(mkt()) is None
    assert k1.eligibility_reason(mkt(result="no")) is None


def test_scalar_and_multivariate_markets_are_dropped():
    assert k1.eligibility_reason(mkt(market_type="scalar")) == "not_binary"
    assert k1.eligibility_reason(mkt(mve_collection_ticker="KXMVESPORTS-R")) == "mve"
    assert k1.eligibility_reason(mkt(ticker="KXMVECROSS-S1-X", event_ticker="KXMVECROSS-S1")) == "mve"
    assert k1.eligibility_reason(mkt(mve_collection_ticker="")) is None


def test_voided_scalar_or_unsettled_results_are_dropped():
    for r in ("void", "", "scalar", None):
        assert k1.eligibility_reason(mkt(result=r)) == "not_settled_yes_no"
    assert k1.eligibility_reason(mkt(status="active", result="")) == "not_settled_yes_no"
    assert k1.eligibility_reason(mkt(status="determined")) == "not_settled_yes_no"
    assert k1.eligibility_reason(mkt(status="settled")) is None


def test_close_time_window_is_2021_07_01_to_2026_10_29_inclusive_utc():
    ok = dict(open_time="2021-06-01T00:00:00Z")
    assert k1.eligibility_reason(mkt(close_time="2021-07-01T00:00:00Z", **ok)) is None
    assert k1.eligibility_reason(mkt(close_time="2021-06-30T23:59:59Z", **ok)) == "close_outside_window"
    assert k1.eligibility_reason(mkt(close_time="2026-10-29T23:59:59Z", open_time="2026-10-01T00:00:00Z")) is None
    assert k1.eligibility_reason(mkt(close_time="2026-10-30T00:00:00Z",
                                     open_time="2026-10-01T00:00:00Z")) == "close_outside_window"


def test_market_must_be_open_at_least_three_days():
    assert k1.eligibility_reason(mkt(open_time="2024-01-07T00:00:00Z")) is None            # exactly 3 days
    assert k1.eligibility_reason(mkt(open_time="2024-01-07T00:00:01Z")) == "open_lt_3d"
    assert k1.eligibility_reason(mkt(open_time=None)) == "open_lt_3d"


# ---------------------------------------------------------------- SHA-1 sampling

def test_event_hash_is_sha1_of_the_event_ticker_as_an_integer():
    assert k1.event_hash("KXFED-26APR") == int(hashlib.sha1(b"KXFED-26APR").hexdigest(), 16)


def test_choose_k_is_one_when_the_cap_is_not_exceeded():
    counts = {f"EV-{i}": 3 for i in range(10)}
    assert k1.choose_k(counts, cap=30) == 1


def test_choose_k_is_the_smallest_k_keeping_at_most_cap_markets():
    counts = {f"EV-{i}": 1 + i % 4 for i in range(400)}

    def kept(k):
        return sum(n for e, n in counts.items() if int(hashlib.sha1(e.encode()).hexdigest(), 16) % k == 0)

    cap = 180
    expect = next(k for k in range(1, 1000) if kept(k) <= cap)
    assert expect > 2
    assert k1.choose_k(counts, cap=cap) == expect


def test_sample_keeps_whole_events():
    df = pd.DataFrame({"ticker": [f"E{i}-{j}" for i in range(50) for j in range(3)],
                       "event_ticker": [f"E{i}" for i in range(50) for j in range(3)]})
    k, kept = k1.sample(df, cap=60)
    assert k == k1.choose_k(df.groupby("event_ticker").size().to_dict(), cap=60)
    assert len(kept) <= 60
    assert (kept.groupby("event_ticker").size() == 3).all()
    assert all(k1.event_hash(e) % k == 0 for e in kept["event_ticker"])


# ---------------------------------------------------------------- candles

def test_norm_candles_reads_both_api_formats():
    h = k1.norm_candles([hcandle(100, 0.41, 0.44, 12.5)])[0]
    v = k1.norm_candles([lcandle(100, 0.41, 0.44, 12.5)])[0]
    for c in (h, v):
        assert c == {"end": 100, "bid": pytest.approx(0.41), "ask": pytest.approx(0.44), "vol": pytest.approx(12.5)}
    assert k1.norm_candles([hcandle(100, None, 0.44, 0)])[0]["bid"] is None


def test_norm_candles_sorts_by_end():
    c = k1.norm_candles([hcandle(300, 0.1, 0.2, 1), hcandle(100, 0.1, 0.2, 1)])
    assert [x["end"] for x in c] == [100, 300]


# ---------------------------------------------------------------- entry candle

def test_entry_is_the_last_candle_ending_at_least_24h_before_close():
    e = k1.entry(week(), ts(CLOSE), ts("2024-01-10T01:00:00Z"), lead_hours=24)
    assert e["skip"] is None
    assert e["end"] == ts("2024-01-08T04:00:00Z")      # the Jan 9 04:00 candle ends only 20 h before the close


def test_a_candle_ending_exactly_24h_before_close_is_used():
    close = ts("2024-01-09T04:00:00Z") + DAY
    e = k1.entry(week(), close, close + 60, lead_hours=24)
    assert e["end"] == ts("2024-01-09T04:00:00Z")


def test_72h_entry_variant():
    e = k1.entry(week(), ts(CLOSE), ts(CLOSE), lead_hours=72)
    assert e["end"] == ts("2024-01-06T04:00:00Z")


def test_no_candle_early_enough_is_skipped():
    e = k1.entry(week(), ENDS[0] + DAY - 1, ENDS[0] + DAY, lead_hours=24)
    assert e["skip"] == "no_entry_candle"
    assert k1.entry([], ts(CLOSE), ts(CLOSE), 24)["skip"] == "no_entry_candle"


def test_market_settled_before_the_candle_end_is_skipped():
    e = k1.entry(week(), ts(CLOSE), ts("2024-01-08T03:59:59Z"), lead_hours=24)
    assert e["skip"] == "settled_before_entry"
    assert k1.entry(week(), ts(CLOSE), None, lead_hours=24)["skip"] is None     # no settlement time recorded


def test_spread_wider_than_five_cents_is_skipped():
    assert k1.entry(week(bid=0.48, ask=0.53), ts(CLOSE), None, 24)["skip"] is None        # exactly $0.05
    assert k1.entry(week(bid=0.47, ask=0.53), ts(CLOSE), None, 24)["skip"] == "spread"


def test_missing_bid_or_ask_at_the_entry_candle_is_skipped():
    c = week()
    c[6]["bid"] = None                                     # the Jan 8 entry candle
    assert k1.entry(c, ts(CLOSE), None, 24)["skip"] == "no_quote"


def test_volume_counts_the_seven_candles_up_to_and_including_the_entry_candle():
    c = k1.norm_candles([hcandle(e, 0.2, 0.22, v) for e, v in zip(ENDS, [500, 10, 10, 10, 10, 10, 39.99, 900])])
    # entry = Jan 8 (index 6); the 7 candles up to it are indexes 0..6 -> 500 + 50 + 39.99
    e = k1.entry(c, ts(CLOSE), None, 24)
    assert e["vol7"] == pytest.approx(589.99)
    c2 = k1.norm_candles([hcandle(e, 0.2, 0.22, v) for e, v in zip(ENDS, [500, 10, 10, 10, 10, 10, 9.99, 900])])
    c2 = [x for x in c2]
    # drop the first candle: now the 7 up to the entry are 10*5 + 9.99 (only 6 exist) -> under 100
    e2 = k1.entry(c2[1:], ts(CLOSE), None, 24)
    assert e2["vol7"] == pytest.approx(59.99)
    assert e2["skip"] == "volume"


def test_volume_of_exactly_100_passes():
    c = k1.norm_candles([hcandle(e, 0.2, 0.22, v) for e, v in zip(ENDS, [0, 10, 10, 10, 10, 10, 50, 0])])
    e = k1.entry(c, ts(CLOSE), None, 24)
    assert e["vol7"] == pytest.approx(100)
    assert e["skip"] is None


def test_entry_returns_the_quote_at_the_candle_close():
    e = k1.entry(week(bid=0.81, ask=0.83, vol=50), ts(CLOSE), None, 24)
    assert (e["bid"], e["ask"]) == (pytest.approx(0.81), pytest.approx(0.83))


# ---------------------------------------------------------------- side choice

def test_buy_yes_when_the_yes_ask_is_between_70_and_97_cents():
    assert k1.pick_side(0.68, 0.70) == ("yes", pytest.approx(0.70))
    assert k1.pick_side(0.95, 0.97) == ("yes", pytest.approx(0.97))
    assert k1.pick_side(0.97, 0.98) is None
    assert k1.pick_side(0.66, 0.69) is None


def test_buy_no_when_the_no_ask_is_between_70_and_97_cents():
    assert k1.pick_side(0.30, 0.33) == ("no", pytest.approx(0.70))
    assert k1.pick_side(0.03, 0.05) == ("no", pytest.approx(0.97))
    assert k1.pick_side(0.02, 0.04) is None
    assert k1.pick_side(0.31, 0.35) is None


def test_sides_in_a_bucket_can_hold_both_sides():
    s = k1.sides_in(0.50, 0.52, 0.50, 0.70, hi_closed=False)
    assert sorted(s) == [("no", pytest.approx(0.50)), ("yes", pytest.approx(0.52))]
    assert k1.sides_in(0.68, 0.70, 0.50, 0.70, hi_closed=False) == []


def test_longshot_side_is_any_ask_under_30_cents():
    assert k1.sides_in(0.08, 0.10, 0.0, 0.30, hi_closed=False, lo_open=True) == [("yes", pytest.approx(0.10))]
    assert k1.sides_in(0.95, 1.00, 0.0, 0.30, hi_closed=False, lo_open=True) == [("no", pytest.approx(0.05))]
    assert k1.sides_in(0.00, 0.02, 0.0, 0.30, hi_closed=False, lo_open=True) == [("yes", pytest.approx(0.02))]
    assert k1.sides_in(0.00, 0.00, 0.0, 0.30, hi_closed=False, lo_open=True) == []           # no ask at all


# ---------------------------------------------------------------- fees and returns

def test_quadratic_taker_fee():
    assert k1.taker_fee(0.80, 1) == pytest.approx(0.07 * 0.8 * 0.2)
    assert k1.taker_fee(0.80, 0.5) == pytest.approx(0.5 * 0.07 * 0.8 * 0.2)
    assert k1.taker_fee(0.80, 0) == 0


def test_fee_rounded_up_to_a_whole_cent():
    assert k1.round_up_cent(0.0112) == pytest.approx(0.02)
    assert k1.round_up_cent(0.07 * 0.97 * 0.03) == pytest.approx(0.01)
    assert k1.round_up_cent(0.0175) == pytest.approx(0.02)
    assert k1.round_up_cent(0.01) == pytest.approx(0.01)
    assert k1.round_up_cent(0.07 * 0.5 * 0.5 * 4 / 7) == pytest.approx(0.01)      # 0.01 after float noise
    assert k1.round_up_cent(0.0) == 0


def test_return_per_contract():
    fee = k1.taker_fee(0.80, 1)
    assert k1.contract_return(0.80, fee, True) == pytest.approx((1 - 0.80 - fee) / (0.80 + fee))
    assert k1.contract_return(0.80, fee, False) == pytest.approx(-1.0)


# ---------------------------------------------------------------- periods and statistics

def test_in_sample_is_close_before_2025_05_01():
    assert k1.period(ts("2025-04-30T23:59:59Z")) == "IS"
    assert k1.period(ts("2025-05-01T00:00:00Z")) == "OOS"


def test_summary_is_clustered_by_event():
    df = pd.DataFrame({"event_ticker": ["A", "A", "B", "C"], "ret": [0.2, 0.4, -0.1, 0.1],
                       "won": [True, True, False, True]})
    s = k1.summarize(df, "ret")
    assert s["trades"] == 4 and s["events"] == 3
    assert s["mean"] == pytest.approx(0.15)
    assert s["event_mean"] == pytest.approx(0.1)
    assert s["t"] == pytest.approx(0.1 / (0.2 / 3 ** 0.5))
    assert s["win_rate"] == pytest.approx(0.75)


def test_summary_of_too_few_events_has_no_t():
    s = k1.summarize(pd.DataFrame({"event_ticker": ["A"], "ret": [0.1], "won": [True]}), "ret")
    assert s["t"] is None
    assert k1.summarize(pd.DataFrame({"event_ticker": [], "ret": [], "won": []}), "ret")["trades"] == 0


def test_verdict_needs_all_three_bars():
    good_is, good_oos, good_up = {"mean": 0.01}, {"mean": 0.01, "t": 2.6}, {"mean": 0.001}
    v = k1.verdict(good_is, good_oos, good_up)
    assert v["pass"] is True and v["clears_2_33"] is True
    assert k1.verdict({"mean": -0.01}, good_oos, good_up)["pass"] is False
    assert k1.verdict(good_is, {"mean": 0.01, "t": 2.5}, good_up)["pass"] is False
    assert k1.verdict(good_is, {"mean": 0.01, "t": 2.5}, good_up)["clears_2_33"] is True
    assert k1.verdict(good_is, good_oos, {"mean": -0.001})["pass"] is False
    assert k1.verdict(good_is, {"mean": 0.01, "t": None}, good_up)["pass"] is False


# ---------------------------------------------------------------- from markets to trades

SERIES = {"SER": {"ticker": "SER", "fee_type": "quadratic", "fee_multiplier": 1, "category": "Economics"},
          "MAK": {"ticker": "MAK", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0.5,
                  "category": "Sports"},
          "CMB": {"ticker": "CMB", "fee_type": "quadratic_with_combo_maker_fees", "fee_multiplier": 1,
                  "category": "Sports"}}


def market_rows(specs):
    """specs: (ticker, event_ticker, result, close_time); the series is the event ticker's prefix."""
    rows = []
    for tk, ev, result, close in specs:
        rows.append(k1.market_row(mkt(ticker=tk, event_ticker=ev, result=result, close_time=close,
                                      open_time="2023-01-01T00:00:00Z", settlement_ts=close), "hist"))
    df = pd.DataFrame(rows)
    df["series_ticker"] = [s[1].split("-")[0] for s in specs]
    return df


def candles_at(close: str, bid: float, ask: float, vol: float = 30.0):
    c = ts(close)
    return k1.norm_candles([hcandle(c - (8 - i) * DAY - 20 * 3600, bid, ask, vol) for i in range(8)])


def test_build_entries_counts_each_skip_and_the_fee_type_drop():
    df = market_rows([("SER-1-A", "SER-1", "yes", "2024-03-01T00:00:00Z"),
                      ("SER-2-A", "SER-2", "no", "2024-03-01T00:00:00Z"),
                      ("MAK-1-A", "MAK-1", "yes", "2025-06-01T00:00:00Z"),
                      ("CMB-1-A", "CMB-1", "yes", "2025-06-01T00:00:00Z"),
                      ("ZZZ-1-A", "ZZZ-1", "yes", "2025-06-01T00:00:00Z"),
                      ("SER-3-A", "SER-3", "yes", "2025-06-01T00:00:00Z")])
    candles = {"SER-1-A": candles_at("2024-03-01T00:00:00Z", 0.80, 0.82),
               "SER-2-A": candles_at("2024-03-01T00:00:00Z", 0.40, 0.50),            # spread 10c
               "MAK-1-A": candles_at("2025-06-01T00:00:00Z", 0.10, 0.12),
               "SER-3-A": []}
    entries, counts = k1.build_entries(df, SERIES, candles, lead_hours=24)
    assert counts["fee_type_dropped"] == 1 and counts["series_missing"] == 1
    assert counts["spread"] == 1 and counts["no_entry_candle"] == 1
    assert sorted(entries["ticker"]) == ["MAK-1-A", "SER-1-A"]
    row = entries.set_index("ticker").loc["MAK-1-A"]
    assert row["category"] == "Sports" and row["fee_multiplier"] == 0.5 and row["period"] == "OOS"


def test_market_row_keeps_lifetime_volume():
    assert k1.market_row(mkt(volume_fp="572.10"), "hist")["volume"] == pytest.approx(572.1)
    assert k1.market_row(mkt(volume=40), "hist")["volume"] == pytest.approx(40)
    assert k1.market_row(mkt(), "hist")["volume"] is None


def test_a_market_that_traded_under_100_contracts_in_its_life_fails_volume_without_candles():
    df = market_rows([("SER-1-A", "SER-1", "yes", "2024-03-01T00:00:00Z"),
                      ("SER-2-A", "SER-2", "yes", "2024-03-01T00:00:00Z")])
    df["volume"] = [99.0, None]
    entries, counts = k1.build_entries(df, SERIES, {"SER-2-A": candles_at("2024-03-01T00:00:00Z", 0.8, 0.82)}, 24)
    assert counts["volume"] == 1 and counts["volume_market_total"] == 1 and counts["candles_missing"] == 0
    assert list(entries["ticker"]) == ["SER-2-A"]
    assert k1.needs_candles({"volume": 99.0}) is False
    assert k1.needs_candles({"volume": 100.0}) is True and k1.needs_candles({"volume": None}) is True


def test_favourite_trades_price_fee_and_payout():
    df = market_rows([("SER-1-A", "SER-1", "yes", "2024-03-01T00:00:00Z"),
                      ("SER-2-A", "SER-2", "yes", "2024-03-01T00:00:00Z"),
                      ("MAK-1-A", "MAK-1", "yes", "2025-06-01T00:00:00Z")])
    candles = {"SER-1-A": candles_at("2024-03-01T00:00:00Z", 0.80, 0.82),               # YES at 0.82, wins
               "SER-2-A": candles_at("2024-03-01T00:00:00Z", 0.10, 0.12),               # NO at 0.90, loses
               "MAK-1-A": candles_at("2025-06-01T00:00:00Z", 0.50, 0.52)}               # no favourite
    entries, counts = k1.build_entries(df, SERIES, candles, lead_hours=24)
    t = k1.trades(entries, k1.FAVOURITE).set_index("ticker")
    assert list(t.index) == ["SER-1-A", "SER-2-A"]
    a, b = t.loc["SER-1-A"], t.loc["SER-2-A"]
    assert (a["side"], a["price"], bool(a["won"])) == ("yes", pytest.approx(0.82), True)
    assert a["fee"] == pytest.approx(0.07 * 0.82 * 0.18)
    assert a["ret"] == pytest.approx((1 - 0.82 - a["fee"]) / (0.82 + a["fee"]))
    assert a["ret_up"] == pytest.approx((1 - 0.82 - 0.02) / 0.84)
    assert (b["side"], b["price"], bool(b["won"])) == ("no", pytest.approx(0.90), False)
    assert b["ret"] == pytest.approx(-1.0)


def test_maker_version_buys_at_the_bid_without_fee():
    df = market_rows([("SER-1-A", "SER-1", "yes", "2024-03-01T00:00:00Z"),
                      ("SER-2-A", "SER-2", "no", "2024-03-01T00:00:00Z")])
    candles = {"SER-1-A": candles_at("2024-03-01T00:00:00Z", 0.80, 0.82),
               "SER-2-A": candles_at("2024-03-01T00:00:00Z", 0.10, 0.12)}
    entries, _ = k1.build_entries(df, SERIES, candles, lead_hours=24)
    t = k1.trades(entries, k1.FAVOURITE, maker=True).set_index("ticker")
    assert t.loc["SER-1-A", "price"] == pytest.approx(0.80) and t.loc["SER-1-A", "fee"] == 0
    assert t.loc["SER-1-A", "ret"] == pytest.approx(0.2 / 0.8)
    assert t.loc["SER-2-A", "side"] == "no" and t.loc["SER-2-A", "price"] == pytest.approx(0.88)
    assert t.loc["SER-2-A", "ret"] == pytest.approx(0.12 / 0.88)


def test_analyse_reports_is_oos_and_verdict():
    specs, candles = [], {}
    for i in range(6):
        for period_close in ("2024-03-01T00:00:00Z", "2025-08-01T00:00:00Z"):
            tk = f"SER-{period_close[:4]}{i}-A"
            specs.append((tk, tk[:-2], "yes" if i % 3 else "no", period_close))
            candles[tk] = candles_at(period_close, 0.80, 0.82)
    entries, counts = k1.build_entries(market_rows(specs), SERIES, candles, lead_hours=24)
    res = k1.analyse(entries, entries, counts)
    h = res["headline"]
    assert h["IS"]["trades"] == 6 and h["OOS"]["trades"] == 6
    assert h["IS"]["win_rate"] == pytest.approx(4 / 6)
    assert set(res["verdict"]) >= {"pass", "clears_2_33", "bars"}
    assert set(res["buckets"]) == {"0.50-0.70", "0.70-0.85", "0.85-0.97", "longshot_<0.30"}
    assert res["buckets"]["0.70-0.85"]["OOS"]["trades"] == 6
    assert res["by_category"]["Economics"]["OOS"]["trades"] == 6
    assert res["maker"]["OOS"]["trades"] == 6
    assert res["entry_72h"]["OOS"]["trades"] == 6


# ---------------------------------------------------------------- resumable fetch (fake API)

class FakeAPI:
    def __init__(self, pages, fail_on=None):
        self.pages, self.fail_on, self.calls = pages, fail_on, []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise ConnectionError("dropped")
        cur = params.get("cursor") or ""
        items, nxt = self.pages[cur]
        return {"markets": items, "cursor": nxt}


def test_paginate_saves_pages_and_resumes_from_the_saved_cursor(tmp_path):
    pages = {"": ([{"ticker": "a"}], "c1"), "c1": ([{"ticker": "b"}], "c2"), "c2": ([{"ticker": "c"}], "")}
    api = FakeAPI(pages, fail_on=2)
    with pytest.raises(ConnectionError):
        k1.paginate(api, "/historical/markets", {"limit": 1000}, "markets", tmp_path)
    api2 = FakeAPI(pages)
    k1.paginate(api2, "/historical/markets", {"limit": 1000}, "markets", tmp_path)
    assert [c[1].get("cursor") for c in api2.calls] == ["c1", "c2"]
    assert [m["ticker"] for m in k1.read_pages(tmp_path, "markets")] == ["a", "b", "c"]
    api3 = FakeAPI(pages)
    k1.paginate(api3, "/historical/markets", {"limit": 1000}, "markets", tmp_path)      # finished: no calls
    assert api3.calls == []


def test_fetch_candles_one_uses_the_right_endpoint_and_skips_cached(tmp_path):
    calls = []

    def get(path, params):
        calls.append((path, params))
        if path.startswith("/series/"):
            raise k1.NotFound(path)
        return {"ticker": "T-1", "candlesticks": [hcandle(5, 0.1, 0.2, 3)]}

    row = {"ticker": "T-1", "series_ticker": "T", "source": "live", "open_ts": 0, "close_ts": 10 * DAY}
    k1.fetch_candles_one(get, row, tmp_path)
    assert [c[0] for c in calls] == ["/series/T/markets/T-1/candlesticks", "/historical/markets/T-1/candlesticks"]
    assert calls[1][1] == {"start_ts": 0, "end_ts": 10 * DAY, "period_interval": 1440}
    with gzip.open(k1.candle_path(tmp_path, "T-1"), "rt") as f:
        assert json.load(f)["candlesticks"][0]["end_period_ts"] == 5
    k1.fetch_candles_one(get, row, tmp_path)
    assert len(calls) == 2


def test_fetch_candles_one_records_a_market_with_no_candles(tmp_path):
    def get(path, params):
        raise k1.NotFound(path)

    row = {"ticker": "T-2", "series_ticker": "T", "source": "hist", "open_ts": 0, "close_ts": DAY}
    k1.fetch_candles_one(get, row, tmp_path)
    assert k1.load_candles(tmp_path, "T-2") == []


# ---------------------------------------------------------------- end to end: fetch then report (fake API)

class FakeKalshi:
    """Serves /series, /historical/cutoff, both listings (two pages each), /events?tickers= and candles."""

    def __init__(self):
        def m(tk, close, result, vol="500.00", **kw):
            return mkt(ticker=tk, event_ticker=tk.rsplit("-", 1)[0], close_time=close, result=result,
                       open_time="2024-01-01T00:00:00Z", settlement_ts=close, volume_fp=vol,
                       created_time="2026-08-01T00:00:00Z", **kw)
        self.hist = [[m("SER-H1-A", "2024-06-01T00:00:00Z", "yes"), m("SER-H1-B", "2024-06-01T00:00:00Z", "no"),
                      m("SER-H2-A", "2024-06-01T00:00:00Z", "yes", vol="10.00")],
                     [m("SER-H3-A", "2024-06-01T00:00:00Z", "no", market_type="scalar"),
                      m("CMB-H4-A", "2024-06-01T00:00:00Z", "yes")]]
        self.live = [[m("SER-L1-A", "2026-09-20T00:00:00Z", "yes")], [m("SER-L2-A", "2026-09-20T00:00:00Z", "no")]]
        self.calls = []

    def _page(self, pages, params):
        cur = params.get("cursor") or "0"
        if not cur.isdigit():                       # a constructed segment cursor: nothing older in this fake
            return {"markets": [], "cursor": ""}
        i = int(cur)
        return {"markets": pages[i], "cursor": str(i + 1) if i + 1 < len(pages) else ""}

    def __call__(self, path, params=None):
        params = params or {}
        self.calls.append(path)
        if path == "/series":
            return {"series": list(SERIES.values())}
        if path == "/historical/cutoff":
            return {"market_settled_ts": "2026-08-31T00:00:00Z"}
        if path == "/historical/markets":
            return self._page(self.hist, params)
        if path == "/markets":
            return self._page(self.live, params)
        if path == "/events":
            return {"events": [{"event_ticker": e, "series_ticker": e.split("-")[0]}
                               for e in params["tickers"].split(",")], "cursor": ""}
        if path == "/markets/candlesticks":                      # batch: live markets only
            return {"markets": [{"market_ticker": t, "candlesticks": self._candles(t)}
                                for t in params["market_tickers"].split(",") if "-L" in t]}
        tk = path.split("/")[-2]
        close = ts("2026-09-20T00:00:00Z") if "-L" in tk else ts("2024-06-01T00:00:00Z")
        if path.startswith("/series/") != ("-L" in tk):
            raise k1.NotFound(path)
        return {"ticker": tk, "candlesticks": self._candles(tk)}

    def _candles(self, tk):
        close = ts("2026-09-20T00:00:00Z") if "-L" in tk else ts("2024-06-01T00:00:00Z")
        bid, ask = {"SER-H1-A": (0.84, 0.86), "SER-H1-B": (0.14, 0.16), "SER-L1-A": (0.90, 0.92),
                    "SER-L2-A": (0.05, 0.07)}[tk]
        mk = lcandle if "-L" in tk else hcandle
        return [mk(close - (8 - i) * DAY - 20 * 3600, bid, ask, 30.0) for i in range(8)]


def test_fetch_then_report_end_to_end(tmp_path):
    api = FakeKalshi()
    k1.fetch(tmp_path, workers=2, api=api)
    candle_calls = [c for c in api.calls if c.endswith("/candlesticks")]
    assert sorted(c.split("/")[-2] for c in candle_calls) == ["SER-H1-A", "SER-H1-B", "markets"]   # live: 1 batch
    k1.fetch(tmp_path, workers=2, api=api)                       # everything cached: no new candle calls
    assert len([c for c in api.calls if c.endswith("/candlesticks")]) == len(candle_calls)

    res = k1.report(tmp_path)
    c = res["counts"]
    assert c["markets_listed"] == 7 and c["eligible"] == 6 and c["k"] == 1 and c["sampled_markets"] == 6
    assert c["listed_not_binary"] == 1 and c["fee_type_dropped"] == 1 and c["skip24_volume_market_total"] == 1
    assert c["trades_IS"] == 2 and c["trades_OOS"] == 2
    t = k1.trades(k1.build_entries(*_entries_inputs(tmp_path))[0]).set_index("ticker")
    assert t.loc["SER-H1-B", "side"] == "no" and t.loc["SER-H1-B", "price"] == pytest.approx(0.86)
    assert bool(t.loc["SER-L2-A", "won"]) is True
    assert (tmp_path / "k1_results.json").exists() and (tmp_path / "k1_summary.md").exists()
    assert "Verdict" in (tmp_path / "k1_summary.md").read_text()


def _entries_inputs(data):
    eligible = pd.read_parquet(data / "eligible.parquet")
    series = k1.load_series(data)
    _, kept = k1.sample(eligible)
    kept = kept.copy()
    kept["series_ticker"], _ = k1.series_for(kept, json.loads((data / "event_series.json").read_text()), series)
    return kept, series, k1.CandleStore(data / "candles"), 24


def test_market_table_keeps_only_eligible_rows_and_counts_each_ticker_once(tmp_path):
    good = mkt(ticker="SER-1-A", event_ticker="SER-1")
    pages = {"hist_markets": [[good, mkt(ticker="SER-1-B", event_ticker="SER-1", market_type="scalar")],
                              [mkt(ticker="SER-2-A", event_ticker="SER-2", open_time="2024-01-09T00:00:00Z")]],
             "live_markets": [[good, mkt(ticker="SER-3-A", event_ticker="SER-3", status="active", result="")]]}
    for d, ps in pages.items():
        (tmp_path / d).mkdir()
        for i, p in enumerate(ps, 1):
            k1._write_json_gz(tmp_path / d / f"p{i:06d}.json.gz", {"markets": p, "cursor": ""})
    eligible, summ = k1.build_market_table(tmp_path)
    assert list(eligible["ticker"]) == ["SER-1-A"] and eligible["source"].tolist() == ["hist"]
    assert summ["rows_listed"] == 5 and summ["markets_listed"] == 4
    assert summ["by_source"] == {"hist": 3, "live": 1}
    assert summ["reasons"] == {"eligible": 1, "not_binary": 1, "open_lt_3d": 1, "not_settled_yes_no": 1}


# ---------------------------------------------------------------- time-segmented listing

def test_make_cursor_reproduces_a_real_kalshi_cursor():
    real = "CgwI89LR1AYQgIeLuwISKEtYTUxCSFItMjZBVUczMDE2MDVCQUxBVEgtQkFMQ05BUlZFWjc1LTI"
    assert k1.make_cursor(1788111219, 660784000, "KXMLBHR-26AUG301605BALATH-BALCNARVEZ75-2") == real


def test_paginate_starts_at_a_given_cursor_and_stops_below_a_created_time(tmp_path):
    def page(times, nxt):
        return ([{"ticker": f"m{t}", "created_time": datetime.fromtimestamp(t, timezone.utc).isoformat()}
                 for t in times], nxt)

    pages = {"start": page([500, 400], "c1"), "c1": page([300, 200], "c2"), "c2": page([100, 50], "")}
    api = FakeAPI(pages)
    st = k1.paginate(api, "/historical/markets", {"limit": 2}, "markets", tmp_path, start_cursor="start",
                     stop_before_ts=250)
    assert [c[1]["cursor"] for c in api.calls] == ["start", "c1"]       # page c1 reaches below 250: stop
    assert st["done"] is True
    assert [m["ticker"] for m in k1.read_pages(tmp_path, "markets")] == ["m500", "m400", "m300", "m200"]


def test_hist_segments_cover_the_whole_listing_without_gaps():
    segs = k1.hist_segments()
    assert segs[0]["dir"] == "hist_markets" and segs[0]["start_cursor"] is None
    assert segs[-1]["stop_before_ts"] is None
    for a, b in zip(segs, segs[1:]):
        # the newer segment runs past the older one's starting point, so nothing falls between them
        assert a["stop_before_ts"] < b["start_ts"]


def test_limiter_ignores_the_usual_share_of_429s_and_slows_only_when_most_requests_fail():
    lim = k1.Limiter(interval=0.2, lo=0.2, hi=2.0, window=20)

    def feed(n_ok, n_429):
        for _ in range(n_ok):
            lim.ok()
        for _ in range(n_429):
            lim.throttled()

    feed(14, 6)                                  # 30% 429s: Kalshi's usual share from this network
    assert lim.interval == pytest.approx(0.2)
    feed(8, 12)                                  # 60%: slow down
    assert lim.interval == pytest.approx(0.3)
    feed(18, 2)                                  # 10%: speed back up, never past lo
    assert lim.interval == pytest.approx(0.24)
    feed(20, 0)
    assert lim.interval == pytest.approx(0.2)
    assert lim.throttles == 20


def test_live_candles_come_in_batches_trimmed_to_each_markets_life(tmp_path):
    rows = [{"ticker": f"L-{i}", "series_ticker": "L", "source": "live", "open_ts": i * DAY, "close_ts": (i + 5) * DAY}
            for i in range(5)]
    calls = []

    def get(path, params):
        calls.append((path, params))
        tks = params["market_tickers"].split(",")
        return {"markets": [{"market_ticker": t, "candlesticks": [lcandle(e * DAY, 0.4, 0.42, 1)
                                                                  for e in range(0, 12)]}
                            for t in tks if t != "L-3"]}

    left = k1.fetch_candles_batch(get, rows, tmp_path, max_tickers=3)
    assert [c[0] for c in calls] == ["/markets/candlesticks", "/markets/candlesticks"]
    assert calls[0][1]["period_interval"] == 1440 and calls[0][1]["market_tickers"] == "L-0,L-1,L-2"
    assert calls[0][1]["start_ts"] == 0 and calls[0][1]["end_ts"] == 7 * DAY
    assert [r["ticker"] for r in left] == ["L-3"]                     # not returned: left for the one-by-one fetch
    c = k1.load_candles(tmp_path, "L-1")
    assert [x["end"] for x in c] == [e * DAY for e in range(1, 7)]   # only candles inside open..close
    assert k1.load_candles(tmp_path, "L-3") is None
