"""Tests for research/book_h_candidates.py (H1-H3, see research/book_h_candidates_prereg.md)."""
import math
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH.parent))

import book_h_candidates as bh  # noqa: E402


# ---------------------------------------------------------------- calendar

def test_next_session_skips_weekend_and_holidays():
    cal = bh.Calendar(2012, 2013)
    assert cal.next_session(date(2012, 10, 26)) == date(2012, 10, 31)      # Sandy closed Mon-Tue
    assert cal.next_session(date(2013, 3, 28)) == date(2013, 4, 1)         # Good Friday
    assert cal.next_session(date(2013, 7, 3)) == date(2013, 7, 5)
    assert cal.prev_session(date(2013, 4, 1)) == date(2013, 3, 28)


def test_ford_funeral_closure_is_known():
    cal = bh.Calendar(2006, 2007)
    assert cal.next_session(date(2006, 12, 29)) == date(2007, 1, 3)


# ---------------------------------------------------------------- session prices

def _minute_bars(day, closes_by_minute):
    rows = [{"day": day, "hm": hm, "close": c} for hm, c in closes_by_minute.items()]
    return pd.DataFrame(rows)


def test_session_prices_take_the_close_of_the_bar_ending_at_each_time():
    day = date(2010, 6, 1)
    bars = {hm: 100.0 + (hm - 570) * 0.01 for hm in range(570, 960)}
    px = bh.session_prices_1m(_minute_bars(day, bars))
    row = px.loc[day]
    assert row.p0935 == pytest.approx(bars[574])          # bar 09:34 ends at 09:35
    assert row.p0931 == pytest.approx(bars[570])
    assert row.p1555 == pytest.approx(bars[954])
    assert row.p1500 == pytest.approx(bars[899])
    assert row.p1000 == pytest.approx(bars[599])
    assert row.close == pytest.approx(bars[959])


def test_session_prices_forward_fill_a_missing_bar():
    day = date(2010, 6, 1)
    bars = {hm: 100.0 + hm * 0.001 for hm in range(570, 960) if hm != 954}
    px = bh.session_prices_1m(_minute_bars(day, bars))
    assert px.loc[day].p1555 == pytest.approx(bars[953])


def test_session_prices_5m_use_the_bar_starting_five_minutes_earlier():
    day = date(2025, 6, 2)
    rows = [{"day": day, "hm": 570 + 5 * i, "close": 500.0 + i} for i in range(78)]
    px = bh.session_prices_5m(pd.DataFrame(rows))
    r = px.loc[day]
    assert r.p0935 == 500.0                                # the 09:30 bar closes at 09:35
    assert r.p1555 == 500.0 + 76                           # the 15:50 bar
    assert r.close == 500.0 + 77
    assert math.isnan(r.p0931)


# ---------------------------------------------------------------- costs and nights

def test_costs_match_the_prereg():
    assert bh.COSTS["mid1"] == (pytest.approx(0.131), pytest.approx(0.431))
    assert bh.COSTS["taker"] == (pytest.approx(1.0), pytest.approx(1.3))


def test_round_trip_bp():
    assert bh.trade_bp(100.0, 100.10, "mid1") == pytest.approx(10.0 - 0.131 - 0.431)


def _px(rows):
    df = pd.DataFrame(rows).set_index("day")
    for c in ("p0931", "p0935", "p1000", "p1500", "p1555", "close"):
        if c not in df:
            df[c] = np.nan
    return df


def test_h1_trades_every_valid_night_and_skips_a_missing_next_session():
    cal = bh.Calendar(2010, 2010)
    px = _px([
        {"day": date(2010, 6, 1), "p1555": 100.0, "p0935": 99.0, "close": 100.0},
        {"day": date(2010, 6, 2), "p1555": 101.0, "p0935": 101.0, "close": 101.0},
        # 2010-06-03 missing from the data: the night of 06-02 is a no-trade
        {"day": date(2010, 6, 4), "p1555": 102.0, "p0935": 102.0, "close": 102.0},
    ])
    out = bh.overnight(px, cal, "mid1")
    assert out[date(2010, 6, 1)] == pytest.approx(bh.trade_bp(100.0, 101.0, "mid1"))
    assert out[date(2010, 6, 2)] == 0.0
    assert out[date(2010, 6, 4)] == 0.0                    # next session not in the data


def test_h2_needs_a_down_day_and_a_valid_previous_session():
    cal = bh.Calendar(2010, 2010)
    px = _px([
        {"day": date(2010, 6, 1), "p1555": 100.0, "p0935": 100.0, "close": 100.0},
        {"day": date(2010, 6, 2), "p1555": 99.0, "p0935": 99.5, "close": 99.0},      # down vs 100 close
        {"day": date(2010, 6, 3), "p1555": 99.5, "p0935": 100.0, "close": 99.5},     # up vs 99 close
        {"day": date(2010, 6, 4), "p1555": 99.0, "p0935": 99.0, "close": 99.0},
    ])
    out = bh.overnight(px, cal, "mid1", cond=bh.down_day)
    assert out[date(2010, 6, 1)] == 0.0                    # previous session (05-31 holiday, 05-28) absent
    assert out[date(2010, 6, 2)] == pytest.approx(bh.trade_bp(99.0, 100.0, "mid1"))   # exits at 06-03 09:35
    assert out[date(2010, 6, 3)] == 0.0


def test_overnight_weekday_only_and_dividend_sensitivities():
    cal = bh.Calendar(2010, 2010)
    px = _px([
        {"day": date(2010, 6, 4), "p1555": 100.0, "p0935": 100.0, "close": 100.0},   # Friday
        {"day": date(2010, 6, 7), "p1555": 100.0, "p0935": 101.0, "close": 100.0},
        {"day": date(2010, 6, 8), "p1555": 100.0, "p0935": 100.0, "close": 100.0},
    ])
    wk = bh.overnight(px, cal, "mid1", weekday_only=True)
    assert wk[date(2010, 6, 4)] == 0.0
    assert wk[date(2010, 6, 7)] == pytest.approx(bh.trade_bp(100.0, 100.0, "mid1"))
    dv = bh.overnight(px, cal, "mid1", div_bp=0.6)
    assert dv[date(2010, 6, 7)] == pytest.approx(bh.trade_bp(100.0, 100.0, "mid1") + 0.6)


# ---------------------------------------------------------------- H3

def test_rsi2_dip_enters_below_10_above_sma200_and_exits_above_sma5():
    n = 230
    closes = list(np.linspace(100, 130, n))                # steady uptrend: RSI(2) = 100
    p1555 = list(closes)
    # three sharp down days, then recovery
    for k, v in ((200, 126.0), (201, 124.0), (202, 122.0)):
        closes[k] = p1555[k] = v
    for k in range(203, n):
        closes[k] = p1555[k] = 122.0 + (k - 202) * 1.5
    days = pd.bdate_range("2010-01-04", periods=n).date
    px = pd.DataFrame({"p1555": p1555, "close": closes}, index=days)
    daily, trades = bh.rsi2_dip(px, "mid1")
    assert len(trades) == 1
    t = trades[0]
    assert t["entry"] == days[201]                         # first day RSI(2) < 10 (day 200 is one down day)
    exit_i = list(days).index(t["exit"])
    sma5 = np.mean(closes[exit_i - 4:exit_i] + [p1555[exit_i]])
    assert p1555[exit_i] > sma5
    assert daily.sum() == pytest.approx(t["bp"])
    want = (p1555[exit_i] / p1555[201] - 1) * 1e4 - 0.131 - 0.431
    assert t["bp"] == pytest.approx(want)


def test_rsi2_dip_time_stop():
    n = 230
    closes = list(np.linspace(100, 130, n))
    for k in range(200, n):                                # down and staying down
        closes[k] = 128.0 - (k - 199) * 0.2
    px = pd.DataFrame({"p1555": closes, "close": closes}, index=pd.bdate_range("2010-01-04", periods=n).date)
    _, trades = bh.rsi2_dip(px, "mid1", time_stop=10)
    assert trades and trades[0]["sessions"] == 10


# ---------------------------------------------------------------- stats and bar

def test_stats_t_and_trade_counts():
    x = pd.Series([1.0, 0.0, 3.0, -1.0], index=pd.bdate_range("2010-01-04", periods=4).date)
    s = bh.stats(x, trades=[1.0, 3.0, -1.0])
    assert s["mean"] == pytest.approx(0.75)
    assert s["t"] == pytest.approx(0.75 / (np.std([1, 0, 3, -1], ddof=1) / 2))
    assert s["trades"] == 3
    assert s["pf"] == pytest.approx(4.0)


def test_stage1_bar():
    ok = {"is": {"mid1": {"mean": 0.1}}, "oos": {"mid1": {"t": 2.4}, "taker": {"mean": 0.01}}}
    assert bh.stage1_pass(ok)
    assert not bh.stage1_pass({**ok, "oos": {"mid1": {"t": 2.2}, "taker": {"mean": 0.01}}})
    assert not bh.stage1_pass({**ok, "is": {"mid1": {"mean": -0.1}}})
    assert not bh.stage1_pass({**ok, "oos": {"mid1": {"t": 3.0}, "taker": {"mean": -0.01}}})


def test_holdout_bar():
    assert bh.holdout_pass({"mid1": {"t": 1.7}, "taker": {"mean": 0.2}})
    assert not bh.holdout_pass({"mid1": {"t": 1.6}, "taker": {"mean": 0.2}})
