"""Book F, large-cap stocks in play (docs/BOOK_F_HANDOFF.md sections 3 and 6)."""
from __future__ import annotations

from datetime import date, time

import pytest

from agentdesk.books import f_stocks_in_play as F

CFG = {"enabled": True, "paper_only": True, "instrument": "shares", "scan_et": "09:35", "rvol5_min": 2.0, "top_n": 5,
       "first_candle": "green", "entry_cutoff_et": "10:30", "stop_atr_frac": 0.10, "exit_et": "15:55",
       "risk_per_trade": 25, "max_notional": 1000, "max_positions": 5, "daily_loss": 75,
       "universe": {"min_price": 10, "min_atr": 0.50, "min_dollar_vol_20d": 100_000_000, "top_sp500_by_dollar_vol": 130,
                    "extra": ["NVDA", "AMD", "AVGO", "MU", "TSM", "ARM", "MRVL", "SMCI", "WDC", "STX", "MSFT", "META",
                              "GOOGL", "AMZN", "AAPL", "ORCL", "PLTR", "TSLA"]},
       "news_tag": "observe", "shorts": "log_only"}
DAY = date(2026, 10, 1)       # a Thursday


def row(sym, rvol, o=100.0, c=101.0, dv=5e8, h=None, l=None, atr=2.0):
    return F.ScanRow(sym, rvol5=rvol, open=o, close=c, or_high=h if h is not None else max(o, c) + 0.2,
                     or_low=l if l is not None else min(o, c) - 0.2, vol5=1000, atr=atr, dollar_vol20=dv)


# ------------------------------------------------------------------ indicators
def test_atr14_is_wilder_through_the_last_bar():
    bars = [{"h": 11.0, "l": 9.0, "c": 10.0}] * 15            # every true range = 2
    assert F.atr14(bars) == pytest.approx(2.0)
    bars = bars + [{"h": 16.0, "l": 10.0, "c": 15.0}]          # TR = max(6, |16-10|, |10-10|) = 6
    assert F.atr14(bars) == pytest.approx((2.0 * 13 + 6.0) / 14)


def test_atr14_needs_fourteen_true_ranges():
    assert F.atr14([{"h": 11.0, "l": 9.0, "c": 10.0}] * 14) is None


def test_rvol5_is_today_over_mean_of_prior_14():
    assert F.rvol5(3000, [1000] * 14) == pytest.approx(3.0)
    assert F.rvol5(3000, [500] * 6 + [1000] * 14) == pytest.approx(3.0)      # only the last 14 sessions count


def test_rvol5_without_14_sessions_or_volume_is_none():
    assert F.rvol5(3000, [1000] * 13) is None
    assert F.rvol5(3000, [0] * 14) is None
    assert F.rvol5(None, [1000] * 14) is None


# ------------------------------------------------------------------ ranking
def test_rank_takes_top_five_green_by_rvol5():
    rows = [row(f"S{i}", 2.0 + i) for i in range(7)]
    res = F.rank_candidates(rows, CFG)
    assert [r.symbol for r in res.picks] == ["S6", "S5", "S4", "S3", "S2"]
    table = {r.symbol: r for r in res.rows}
    assert table["S1"].picked is False and "rank 6" in table["S1"].reason
    assert table["S6"].rank == 1 and table["S6"].picked


def test_rank_ties_are_broken_by_dollar_volume():
    res = F.rank_candidates([row("LOW", 3.0, dv=2e8), row("HIGH", 3.0, dv=9e8)], CFG)
    assert [r.symbol for r in res.picks] == ["HIGH", "LOW"]


def test_rank_excludes_low_rvol_and_doji():
    res = F.rank_candidates([row("QUIET", 1.99), row("DOJI", 5.0, o=100, c=100)], CFG)
    assert res.picks == []
    table = {r.symbol: r for r in res.rows}
    assert "rvol5" in table["QUIET"].reason and "not green" in table["DOJI"].reason


def test_red_first_candle_never_buys_and_goes_to_shadow_shorts():
    res = F.rank_candidates([row("RED", 6.0, o=101, c=100), row("GRN", 2.5)], CFG)
    assert [r.symbol for r in res.picks] == ["GRN"]
    assert [r.symbol for r in res.shorts] == ["RED"]
    red = next(r for r in res.rows if r.symbol == "RED")
    assert red.direction == "red" and not red.picked and "shadow short" in red.reason


# ------------------------------------------------------------------ sizing and stop
def test_shares_respect_the_risk_cap():
    assert F.shares_for(100.0, 99.0, CFG) == 10                # $25 / $1 = 25, but $1000 / $100 = 10
    assert F.shares_for(20.0, 19.0, CFG) == 25                 # risk cap binds: 25 shares, $500 notional


def test_shares_respect_the_notional_cap():
    assert F.shares_for(400.0, 399.0, CFG) == 2                # 1000 / 400 = 2.5


def test_shares_skip_below_one_share():
    assert F.shares_for(1500.0, 1499.0, CFG) == 0              # $1,500 > $1,000 max notional
    assert F.shares_for(50.0, 20.0, CFG) == 0                  # $30 risk per share > $25
    assert F.shares_for(50.0, 50.0, CFG) == 0                  # no risk distance: never size


def test_stop_is_ten_percent_of_atr_below_the_fill():
    assert F.stop_price(100.0, 2.5, CFG) == pytest.approx(99.75)


# ------------------------------------------------------------------ exits
def test_exit_time_is_1555_et_and_1255_on_half_days():
    assert F.exit_time_et(CFG, half_day=False) == time(15, 55)
    assert F.exit_time_et(CFG, half_day=True) == time(12, 55)


def test_should_exit_on_stop_and_at_the_exit_time():
    pos = F.FPos("NVDA", qty=5, entry=100.0, stop=99.8, opened_ts=F.at_et(DAY, time(9, 40)))
    q = F.Q(bid=99.9, ask=100.0, last=99.95, ts=0)
    assert F.should_exit(F.at_et(DAY, time(11, 0)), pos, q, CFG) is None
    assert F.should_exit(F.at_et(DAY, time(11, 0)), pos, F.Q(99.8, 99.9, 99.85, 0), CFG) == "stop"
    assert F.should_exit(F.at_et(DAY, time(15, 55)), pos, q, CFG) == "exit 15:55 ET"
    assert F.should_exit(F.at_et(DAY, time(12, 55)), pos, q, CFG, half_day=True) == "exit 12:55 ET (half day)"
    assert F.should_exit(F.at_et(DAY, time(12, 55)), pos, q, CFG, half_day=False) is None


def test_et_and_ct_differ_by_one_hour():
    from agentdesk.clock import ct_time
    assert ct_time(F.at_et(DAY, time(9, 35, 5))) == time(8, 35, 5)


# ------------------------------------------------------------------ bar fills (paper and backtest share these)
def B(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c}


def test_bar_entry_triggers_only_above_the_or_high():
    assert F.bar_entry_fill(B(99.5, 100.0, 99.0, 99.8), or_high=100.0, limit=100.05) is None    # touch, no break
    assert F.bar_entry_fill(B(99.5, 100.2, 99.0, 100.1), or_high=100.0, limit=100.05) == pytest.approx(100.0)


def test_bar_entry_gap_above_uses_open_within_the_limit():
    assert F.bar_entry_fill(B(100.03, 100.4, 100.0, 100.3), 100.0, 100.05) == pytest.approx(100.03)


def test_bar_entry_gap_through_the_limit_fills_at_the_limit_only_if_it_trades_back():
    assert F.bar_entry_fill(B(100.5, 100.9, 100.3, 100.6), 100.0, 100.05) is None
    assert F.bar_entry_fill(B(100.5, 100.9, 100.02, 100.6), 100.0, 100.05) == pytest.approx(100.05)


def test_bar_entry_applies_slippage():
    assert F.bar_entry_fill(B(99.5, 100.2, 99.0, 100.1), 100.0, 100.05, slip_bp=2) == pytest.approx(100.02)


def test_bar_stop_gap_through_fills_at_the_bar_open():
    assert F.bar_stop_fill(B(98.0, 98.5, 97.5, 98.2), stop=99.0) == pytest.approx(98.0)


def test_bar_stop_touch_fills_at_the_stop_and_no_touch_is_none():
    assert F.bar_stop_fill(B(99.5, 99.8, 98.9, 99.2), stop=99.0) == pytest.approx(99.0)
    assert F.bar_stop_fill(B(99.5, 99.8, 99.1, 99.2), stop=99.0) is None
    assert F.bar_stop_fill(B(99.5, 99.8, 98.9, 99.2), stop=99.0, slip_bp=2) == pytest.approx(99.0 * (1 - 0.0002))


def test_entry_and_stop_in_the_same_bar_assume_the_stop_hit():
    b = B(99.9, 100.3, 99.6, 100.2)                            # breaks 100 and trades back below the 99.75 stop
    out = F.bar_entry_then_stop(b, or_high=100.0, limit=100.05, atr=2.5, cfg=CFG)
    assert out == pytest.approx((100.0, 99.75, 99.75))          # (entry, stop, exit): stopped in the entry bar
    b2 = B(99.9, 100.3, 99.8, 100.2)
    assert F.bar_entry_then_stop(b2, 100.0, 100.05, 2.5, CFG) == pytest.approx((100.0, 99.75, None))
