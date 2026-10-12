"""Tests for research/n3_weekly_put_spread.py and fetch_thetadata_spy_weekly.week_pairs
(rules in research/n3_weekly_put_spread_prereg.md)."""
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH.parent))

import bd_real_quotes as bd  # noqa: E402
import fetch_thetadata_spy_weekly as fw  # noqa: E402
import n3_weekly_put_spread as n3  # noqa: E402
from test_h4_overnight_fly import raw_chain  # noqa: E402


def panel(prices, **kw):
    return bd.QuotePanel(bd.normalize(raw_chain(prices, **kw)))


# d's same-day chain: parity spot 650 at 15:50
SPOT = {("C", 650.0): 0.50, ("P", 650.0): 0.50, ("C", 651.0): 0.10, ("P", 651.0): 1.10,
        ("C", 649.0): 1.10, ("P", 649.0): 0.10}
# the next week's chain on d: ATM straddle 4.00 + 4.00 = $8 implied move -> short 646, width round(650/100) = $6 -> long 640
WEEK = {("C", 650.0): 4.00, ("P", 650.0): 4.00, ("P", 646.0): 2.40, ("P", 640.0): 0.90,
        ("P", 648.0): 3.20, ("P", 642.0): 1.40, ("P", 634.0): 0.30}
# expiry day near 15:40: SPY at 645, so the 646 put is $1 in the money
EXIT = {("C", 645.0): 0.30, ("P", 645.0): 0.30, ("P", 646.0): 1.05, ("P", 640.0): 0.02,
        ("C", 646.0): 0.05, ("P", 648.0): 3.00, ("P", 642.0): 0.03, ("P", 634.0): 0.01}


# ---------------------------------------------------------------- week pairing

def test_week_pairs_take_each_weeks_last_session_and_next_weeks_expiry():
    days = [date(2025, 9, 8), date(2025, 9, 11), date(2025, 9, 12),       # Mon, Thu, Fri
            date(2025, 9, 15), date(2025, 9, 19),
            date(2025, 9, 22), date(2025, 9, 25)]                           # Friday 9/26 closed (pretend)
    exps = {date(2025, 9, 12), date(2025, 9, 19), date(2025, 9, 25)}
    assert fw.week_pairs(days, exps) == [(date(2025, 9, 12), date(2025, 9, 19)),
                                         (date(2025, 9, 19), date(2025, 9, 25))]


def test_week_pairs_cross_the_year_and_skip_unlisted_expiries():
    days = [date(2025, 12, 26), date(2026, 1, 2), date(2026, 1, 9)]
    assert fw.week_pairs(days, {date(2026, 1, 2)}) == [(date(2025, 12, 26), date(2026, 1, 2))]


def test_week_reason():
    assert n3.week_reason(date(2025, 9, 12), date(2025, 9, 19)) is None
    assert n3.week_reason(date(2025, 11, 28), date(2025, 12, 5)) == "half_day"        # day after Thanksgiving
    assert n3.week_reason(date(2025, 12, 19), date(2025, 12, 24)) == "half_day"       # Christmas Eve exit
    assert n3.week_reason(date(2025, 9, 12), date(2025, 9, 26)) == "expiry_not_next_week"


# ---------------------------------------------------------------- strikes and one trade

def test_strikes():
    assert n3.strikes(650.0, 8.0) == (646.0, 6.0)
    assert n3.strikes(205.3, 2.0) == (204.0, 2.0)
    assert n3.strikes(80.0, 1.0) == (80.0, 1.0)          # width floors at $1
    assert n3.strikes(650.0, 8.0, short_mult=1.0, width_mult=2) == (642.0, 12.0)


def test_trade_at_patient_fills():
    t = n3.trade(panel(SPOT), panel(WEEK), panel(EXIT), "patient")
    credit = 2.39 - 0.91                                 # sell 646P at mid-1c, buy 640P at mid+1c
    debit = 1.06 - 0.01                                  # buy back 646P at ask 1.06, sell 640P at bid 0.01
    assert (t["short"], t["width"], t["em"], t["spot"]) == (646.0, 6.0, 8.0, 650.0)
    assert t["credit"] == pytest.approx(credit)
    assert t["debit"] == pytest.approx(debit)
    assert t["pnl"] == pytest.approx(100 * (credit - debit) - 0.16)
    assert t["max_risk"] == pytest.approx(100 * (6 - credit) + 0.16)
    assert t["exit_minute"] == n3.EXIT_MIN


def test_sensitivity_strikes_flow_into_the_trade():
    t = n3.trade(panel(SPOT), panel(WEEK), panel(EXIT), "mid", short_mult=0.25)
    assert t["short"] == 648.0


def test_missing_leg_and_missing_spot_skip():
    assert n3.trade(panel(SPOT), panel(WEEK), panel(EXIT), "mid", short_mult=1.0) == {"skip": "missing_leg"}  # 636P
    no_atm = {k: v for k, v in WEEK.items() if k != ("C", 650.0)}
    assert n3.trade(panel(SPOT), panel(no_atm), panel(EXIT), "mid") == {"skip": "no_implied_move"}
    assert n3.trade(panel({("C", 650.0): 0.5}), panel(WEEK), panel(EXIT), "mid") == {"skip": "no_spot"}


def test_exit_never_looks_after_1540_and_caps_at_width():
    late = {("P", 640.0, m) for m in range(570, n3.EXIT_MIN + 1)}
    assert n3.trade(panel(SPOT), panel(WEEK), panel(EXIT, drop=late), "mid") == {"skip": "no_exit_quote"}
    crash = {("P", 646.0): 26.0, ("P", 640.0): 20.0, ("C", 626.0): 0.01, ("P", 626.0): 20.0}
    t = n3.trade(panel(SPOT), panel(WEEK), panel(crash), "taker")
    assert t["debit"] == pytest.approx(6.0)
    assert t["pnl"] == pytest.approx(-t["max_risk"])


def test_settlement_value():
    t = n3.settle_trade(panel(SPOT), panel(WEEK), panel(EXIT), "patient")
    assert t["settle"] == pytest.approx(645.0)
    assert t["debit"] == pytest.approx(1.0)
    assert t["pnl"] == pytest.approx(100 * (1.48 - 1.0) - 0.08)


def test_spy_move():
    assert n3.spy_move(panel(SPOT), panel(EXIT)) == pytest.approx(645.0 / 650.0 - 1)


# ---------------------------------------------------------------- stats

def test_net_of_spy_regression():
    ret = pd.Series([0.01, -0.02, 0.005, -0.01, 0.02])
    pnl = 3.0 + 1000 * ret + pd.Series([0.5, -0.5, 0.2, -0.2, 0.0])
    r = n3.net_of_spy(pnl, ret)
    assert r["alpha"] == pytest.approx(3.0, abs=0.2)
    assert r["beta"] == pytest.approx(1000, rel=0.05)
    assert r["n"] == 5 and r["up_mean"] > r["down_mean"]


def test_pass_bar():
    good = {"is": {"patient": {"mean": 1.0}}, "oos": {"patient": {"t": 2.4}, "taker": {"mean": 0.5}}}
    assert n3.passes(good)
    assert not n3.passes({**good, "oos": {"patient": {"t": 2.2}, "taker": {"mean": 0.5}}})
    assert not n3.passes({**good, "is": {"patient": {"mean": -1.0}}})


def test_run_end_to_end(tmp_path):
    pytest.importorskip("pyarrow")
    wk, q, out = tmp_path / "spy_weekly", tmp_path / "spy_0dte", tmp_path / "out"
    wk.mkdir(), q.mkdir()
    for d, e in ((date(2022, 3, 4), date(2022, 3, 11)),          # traded (out-of-sample)
                 (date(2021, 3, 5), date(2021, 3, 12)),          # traded (in-sample)
                 (date(2025, 11, 28), date(2025, 12, 5))):       # half day: skipped
        raw = raw_chain(WEEK)
        raw["expiration"] = e.isoformat()
        raw.to_parquet(wk / f"{d.isoformat()}.parquet")
        raw_chain(SPOT).to_parquet(q / f"{d.isoformat()}.parquet")
        raw_chain(EXIT).to_parquet(q / f"{e.isoformat()}.parquet")
    res = n3.run(q, wk, out)
    assert res["skips"] == {"half_day": 1}
    assert res["N3"]["main"]["oos"]["patient"]["n"] == 1 and res["N3"]["main"]["is"]["patient"]["n"] == 1
    assert res["N3"]["main"]["oos"]["patient"]["mean"] == pytest.approx(100 * (1.48 - 1.05) - 0.16)
    assert set(res["N3"]["sensitivities"]) == {"short 0.25 x EM", "short 1.0 x EM", "width 2W", "settlement value"}
    assert (out / "report.md").exists() and (out / "n3_trades.csv").exists()
