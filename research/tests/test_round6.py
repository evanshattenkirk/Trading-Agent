"""Synthetic-data tests for research/round6.py and research/round6_fetch.py (round 6 pre-registration)."""
import math
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import round6 as r6  # noqa: E402
import round6_fetch as f6  # noqa: E402

ET = ZoneInfo("America/New_York")


def test_fee_rounds_up_per_100_contract_order():
    # 0.07 * 0.5 * 0.5 = 0.0175 a contract -> $1.75 per 100, exact
    assert r6.fee(0.5) == pytest.approx(0.0175)
    # 0.07 * 0.9 * 0.1 = 0.0063 -> $0.63 per 100
    assert r6.fee(0.9) == pytest.approx(0.0063)
    # 0.07 * 0.33 * 0.67 = 0.0154770 -> $1.5477 -> rounded up to $1.55 per 100
    assert r6.fee(0.33) == pytest.approx(0.0155)
    assert r6.fee(0.5, 0.035) == pytest.approx(0.0088)


def test_return_includes_fee_in_cost():
    assert r6.ret(1, 0.5, 0.0175) == pytest.approx((1 - 0.5175) / 0.5175)
    assert r6.ret(0, 0.5, 0.0175) == pytest.approx(-1.0)


def test_quote_at_exact_then_fallback_within_5_minutes():
    c = [{"end": 100, "bid": 0.1, "ask": 0.2}, {"end": 400, "bid": 0.3, "ask": None},
         {"end": 340, "bid": 0.25, "ask": 0.27}]
    assert r6.quote_at(c, 400) == (0.25, 0.27)          # 400's ask missing -> latest in (100, 400]
    assert r6.quote_at(c, 100) == (0.1, 0.2)
    assert r6.quote_at([{"end": 99, "bid": 0.1, "ask": 0.2}], 400) is None


def test_side_trade_picks_larger_edge_and_respects_threshold():
    # fair 0.60, yes ask 0.50: edge 0.10 - fee(0.50) = 0.0825 -> buy YES
    s = r6.side_trade(0.60, 0.48, 0.50, 1)
    assert s[0] == "yes" and s[1] == 0.50
    # fair 0.30, yes bid 0.45 -> NO costs 0.55, edge 0.70 - 0.55 - fee = 0.1327 -> buy NO, payout 1 if result no
    s = r6.side_trade(0.30, 0.45, 0.47, 0)
    assert s[0] == "no" and s[1] == pytest.approx(0.55) and s[2] > 0
    assert r6.side_trade(0.51, 0.49, 0.50, 1) is None
    # not buyable above 0.98
    assert r6.side_trade(1.0, 0.985, 0.99, 1) is None


def test_digital_curve_from_flat_calls_and_puts():
    # spot 100; calls/puts priced so verticals imply P(S >= m) = 0.8, 0.6 (below) and 0.4, 0.2 (above)
    rows = []
    strikes = [98, 99, 100, 101, 102]
    call = {98: 2.6, 99: 1.8, 100: 1.2, 101: 0.8, 102: 0.6}
    put = {98: 0.6, 99: 0.8, 100: 1.2, 101: 1.8, 102: 2.6}
    for k in strikes:
        rows.append({"strike": k, "right": "CALL", "bid": call[k] - 0.05, "ask": call[k] + 0.05})
        rows.append({"strike": k, "right": "PUT", "bid": put[k] - 0.05, "ask": put[k] + 0.05})
    xs, ys, spot = r6.digital_curve(pd.DataFrame(rows))
    assert spot == pytest.approx(100.0)
    assert list(xs) == [98.5, 99.5, 100.5, 101.5]
    assert ys == pytest.approx([0.8, 0.6, 0.4, 0.2])
    curve = (xs, ys, spot)
    assert r6.fair_index(curve, ("between", 990.0, 1010.0)) == pytest.approx(0.7 - 0.3)
    assert r6.fair_index(curve, ("greater", 1000.0, None)) == pytest.approx(0.5)
    assert r6.fair_index(curve, ("less", None, 1000.0)) == pytest.approx(0.5)
    assert r6.fair_index(curve, ("greater", 900.0, None)) is None


def test_digital_curve_is_made_non_increasing():
    rows = []
    call = {100: 1.0, 101: 0.7, 102: 0.2, 103: 0.1}       # verticals 0.3, 0.5, 0.1 -> 0.3, 0.3, 0.1
    put = {100: 1.0, 101: 1.7, 102: 2.2, 103: 3.1}
    for k in call:
        rows.append({"strike": k, "right": "CALL", "bid": call[k] - 0.01, "ask": call[k] + 0.01})
        rows.append({"strike": k, "right": "PUT", "bid": put[k] - 0.01, "ask": put[k] + 0.01})
    xs, ys, _ = r6.digital_curve(pd.DataFrame(rows))
    assert all(a >= b for a, b in zip(ys[:-1], ys[1:]))


def test_summarize_clusters_by_event_and_splits_halves():
    rows = [{"event": "a", "close": 1, "ret": 1.0, "p": 0.5}, {"event": "a", "close": 1, "ret": -1.0, "p": 0.5},
            {"event": "b", "close": 2, "ret": 0.5, "p": 0.5}, {"event": "c", "close": 3, "ret": 0.1, "p": 0.5},
            {"event": "d", "close": 4, "ret": 0.3, "p": 0.5}]
    s = r6.summarize(rows)
    assert s["events"] == 4 and s["trades"] == 5
    assert s["mean"] == pytest.approx((0 + 0.5 + 0.1 + 0.3) / 4)
    assert s["half1_mean"] == pytest.approx(0.25) and s["half2_mean"] == pytest.approx(0.2)


def test_verdict_needs_t_and_both_halves():
    good = {"events": 100, "t": 3.0, "half1_mean": 0.01, "half2_mean": 0.02}
    assert r6.verdict(good) == "PASS"
    assert r6.verdict(good | {"half2_mean": -0.01}) == "FAIL"
    assert r6.verdict(good | {"t": 2.5}) == "FAIL"
    assert r6.verdict(good, extra=False) == "FAIL"


def test_cpi_strikes_from_fields_or_legacy_subtitle():
    assert r6.cpi_kind({"strike_type": "greater", "floor_strike": 0.3}) == ("greater", 0.3, None)
    assert r6.cpi_kind({"yes_sub_title": "Above 0.4%"}) == ("greater", 0.4, None)
    assert r6.cpi_kind({"yes_sub_title": "Above -0.1%"}) == ("greater", -0.1, None)
    assert r6.cpi_kind({"yes_sub_title": "Something else"}) is None


def test_btc_ticker_times_and_pairing():
    assert datetime.fromtimestamp(f6.btc_close("KXBTCD-26NOV1717"), ET) == datetime(2026, 11, 17, 17, tzinfo=ET)
    assert f6.btc_close("BTCD-24APR01-17") is None
    assert f6.btc_range_event("KXBTCD-26NOV1717") == "KXBTC-26NOV1717"
    assert f6.btc_range_event("BTCD-24AUG0717") == "BTC-24AUG0717"


def test_cpi_entry_day_skips_weekends():
    assert f6.prev_weekday(date(2026, 11, 16)) == date(2026, 11, 13)      # Monday -> Friday
    assert f6.prev_weekday(date(2026, 11, 18), 2) == date(2026, 11, 16)


def test_candle_windows_cover_entries():
    close = int(datetime(2026, 11, 17, 16, tzinfo=ET).timestamp())
    (a, b), = f6.candle_windows("KXINX", close)
    for hh in (10, 12, 14, 15):
        e = int(datetime(2026, 11, 17, hh, tzinfo=ET).timestamp())
        assert a < e - 300 and e <= b
    (a, b), = f6.candle_windows("KXBTCD", close)
    for off in (45, 15, 5):
        assert a < close - off * 60 - 300 and close - off * 60 <= b


def test_choose_k_caps_sample():
    evs = [f"E{i}" for i in range(5000)]
    k = f6.choose_k(evs, 1500)
    kept = [e for e in evs if f6.event_hash(e) % k == 0]
    assert len(kept) <= 1500 and (k == 1 or len([e for e in evs if f6.event_hash(e) % (k - 1) == 0]) > 1500)
