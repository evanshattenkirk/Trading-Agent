"""Tests for research/strategies_new.py (new strategy candidates F1-F4, see research/strategies_new_prereg.md)."""
import math
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest

RESEARCH = Path(__file__).resolve().parents[1] / "research"
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH.parent))

import strategies_new as sn  # noqa: E402

VIX = 16.0
V = (VIX / 100) ** 2 / 252


# ---------------------------------------------------------------- pricing

def test_rth_frac_matches_backtest_share():
    assert sn.rth_frac(-1) == pytest.approx(1 + 15 / 390)
    assert sn.rth_frac(77) == pytest.approx(15 / 390)
    assert sn.rth_frac(5) == pytest.approx(72 / 78 + 15 / 390)


def test_same_day_leg_sd_matches_strategies_bcd_sd_left():
    import strategies_bcd as bcd
    bcd.M_RTH = 0.80
    sdd = VIX / 100 / math.sqrt(252)
    for k in (2, 5, 47, 70):
        assert sn.leg_sd(VIX, expiry=0, d=0, k=k) == pytest.approx(bcd.sd_left(sdd, k))


def test_next_day_leg_adds_overnight_and_full_next_session():
    k = 70
    today = 0.80 ** 2 * V * (77 - k) / 78
    want = today + 0.60 ** 2 * V + 0.80 ** 2 * V * sn.rth_frac(-1)
    assert sn.leg_sd(VIX, expiry=1, d=0, k=k) ** 2 == pytest.approx(want)
    # once it is the expiry day, a next-day leg is a same-day leg
    assert sn.leg_sd(VIX, expiry=1, d=1, k=3) == pytest.approx(sn.leg_sd(VIX, expiry=0, d=0, k=3))


def test_premium_scale_multiplies_every_variance_term():
    assert sn.leg_sd(VIX, 1, 0, 70, s=0.75) == pytest.approx(0.75 * sn.leg_sd(VIX, 1, 0, 70))


def test_custom_intraday_profile_replaces_flat_share():
    shares = np.full(78, 1 / 78)
    frac = sn.profile_frac(shares)
    assert frac(5) == pytest.approx(sn.rth_frac(5))
    u = np.r_[np.full(6, 3.0), np.full(66, 0.5), np.full(6, 3.0)]
    u = u / u.sum()
    assert sn.profile_frac(u)(47) == pytest.approx(u[48:].sum() + 15 / 390)


# ---------------------------------------------------------------- structures

def test_condor_legs_use_ceil_floor_and_two_dollar_wings():
    legs = sn.legs_condor(765.3, 3.2)
    assert sorted(legs) == sorted([("C", 769, -1, 0), ("C", 771, 1, 0), ("P", 762, -1, 0), ("P", 760, 1, 0)])


def test_put_spread_legs_short_below_spot_with_long_two_lower():
    assert sn.legs_put_spread(765.3, 3.2) == [("P", 762, -1, 0), ("P", 760, 1, 0)]
    assert sn.legs_put_spread(765.3, 3.2, expiry=1) == [("P", 762, -1, 1), ("P", 760, 1, 1)]


def test_calendar_sells_same_day_and_buys_next_day_at_the_money():
    assert sn.legs_calendar(765.6) == [("C", 766, -1, 0), ("C", 766, 1, 1)]


def test_leg_cost_is_wider_for_next_day_legs_at_natural():
    assert sn.COSTS["mid-1c"] == {0: 0.01 + sn.FEE, 1: 0.01 + sn.FEE}
    assert sn.COSTS["natural"] == {0: 0.02 + sn.FEE, 1: 0.03 + sn.FEE}


# ---------------------------------------------------------------- trade simulation

def _synthetic(nd=40, seed=3):
    rng = np.random.default_rng(seed)
    C = 765 * np.exp(np.cumsum(rng.normal(0, 0.0009, (nd, 78)), axis=1))
    O = np.c_[np.full(nd, 765.0), C[:, :-1]]
    H = np.maximum(O, C) * 1.0003
    L = np.minimum(O, C) * 0.9997
    return {"O": O, "H": H, "L": L, "C": C, "V": np.ones((nd, 78))}


def test_run_d_reproduces_strategies_bcd_condor():
    import strategies_bcd as bcd
    bcd.M_RTH = 0.80
    M = _synthetic()
    vixp = np.full(M["C"].shape[0], VIX)
    cost = 0.01 + sn.FEE
    ref = bcd.condor_like(M, vixp, "condor", cost)
    ours = [sn.run_D(M, i, VIX, {0: cost, 1: cost}) for i in range(M["C"].shape[0])]
    got = np.array([r["ret"] for r in ours])
    np.testing.assert_allclose(got, ref.ret.values, rtol=1e-9, atol=1e-12)
    assert [r["why"] for r in ours] == list(ref.why)


def _flat_path(S, k0, k1, d=0):
    return [(d, k, S) for k in range(k0, k1 + 1)]


def test_credit_trade_takes_profit_when_spot_is_still():
    legs = sn.legs_condor(765.0, 3.0)
    r = sn.credit_trade(_flat_path(765.0, 47, 70), legs, VIX, {0: 0.01, 1: 0.01}, width=2)
    assert r["why"] == "take 50%"
    assert r["pnl"] > 0


def test_credit_trade_stops_and_loss_never_exceeds_risk():
    legs = sn.legs_put_spread(765.0, 3.0)
    path = [(0, 5, 765.0)] + [(0, k, 765.0 - 1.5 * (k - 5)) for k in range(6, 71)]
    r = sn.credit_trade(path, legs, VIX, {0: 0.01, 1: 0.01}, width=2, min_credit=None)
    assert r["why"] == "stop"
    assert -1.0 <= r["ret"] < 0


def test_credit_trade_skips_below_minimum_credit():
    legs = sn.legs_put_spread(765.0, 30.0)          # far out of the money: almost no credit
    assert sn.credit_trade(_flat_path(765.0, 5, 70), legs, VIX, {0: 0.01, 1: 0.01}, width=2, min_credit=0.10) is None


def test_calendar_debit_trade_gains_when_spot_is_still_and_stops_on_a_big_move():
    legs = sn.legs_calendar(765.0)
    cost = {0: 0.01, 1: 0.01}
    r = sn.debit_trade(_flat_path(765.0, 5, 70), legs, VIX, cost, tp=0.25, stop=0.35)
    assert r["why"] == "take profit" and r["ret"] >= 0.25
    path = [(0, 5, 765.0)] + [(0, k, 765.0 + 0.8 * (k - 5)) for k in range(6, 71)]
    r = sn.debit_trade(path, legs, VIX, cost, tp=0.25, stop=0.35)
    assert r["why"] == "stop" and r["ret"] <= -0.35


# ---------------------------------------------------------------- paths and filters

def test_next_session_must_be_the_next_calendar_day_mon_to_thu():
    assert sn.next_is_consecutive(date(2026, 9, 24), date(2026, 9, 25))       # Thu -> Fri
    assert not sn.next_is_consecutive(date(2026, 9, 25), date(2026, 9, 28))   # Fri -> Mon
    assert not sn.next_is_consecutive(date(2026, 9, 21), date(2026, 9, 23))   # gap (holiday / missing day)


def test_overnight_path_keeps_entry_session_rebase_factor():
    C = np.array([np.linspace(700, 710, 78), np.linspace(720, 730, 78)])
    days = [date(2026, 9, 24), date(2026, 9, 25)]
    p = sn.overnight_path(C, days, 0, f=765 / 700)
    assert p[0] == (0, 70, pytest.approx(C[0, 70] * 765 / 700))
    assert [(d, k) for d, k, _ in p] == [(0, k) for k in range(70, 78)] + [(1, k) for k in range(0, 6)]
    assert p[-1][2] == pytest.approx(C[1, 5] * 765 / 700)
    assert sn.overnight_path(C, [date(2026, 9, 25), date(2026, 9, 28)], 0, f=1.0) is None


def test_sma_gate_uses_only_prior_closes():
    closes = np.r_[np.linspace(100, 150, 60), 1.0]
    assert sn.sma_gate(closes, 60, n=50)          # prior close 150 above its 50-day average
    closes2 = closes.copy(); closes2[60] = 1e9     # today's close can't change the answer
    assert sn.sma_gate(closes2, 60, n=50) == sn.sma_gate(closes, 60, n=50)
    assert not sn.sma_gate(np.linspace(150, 100, 61), 60, n=50)
    assert not sn.sma_gate(closes, 30, n=50)       # not enough history


def test_trailing_profile_ignores_the_current_session_and_sums_to_one():
    M = _synthetic(nd=30)
    a = sn.trailing_profile(M["O"], M["C"], 20, lookback=10)
    M["C"][20] *= np.linspace(1, 1.2, 78)
    b = sn.trailing_profile(M["O"], M["C"], 20, lookback=10)
    np.testing.assert_allclose(a, b)
    assert a.sum() == pytest.approx(1.0)
    assert sn.trailing_profile(M["O"], M["C"], 5, lookback=10) is None


# ---------------------------------------------------------------- stats

def test_max_drawdown_measures_peak_to_trough_of_the_equity_curve():
    assert sn.max_drawdown([10, -5, -10, 20, -30]) == pytest.approx(-30)
    assert sn.max_drawdown([-5, -5, 20]) == pytest.approx(-10)
    assert sn.max_drawdown([1, 2, 3]) == 0


def test_lots_fit_the_per_position_cap():
    assert sn.lots_for(145.0) == 2
    assert sn.lots_for(300.0) == 1
    assert sn.lots_for(310.0) == 0


def test_breakeven_scale_finds_the_zero_crossing():
    assert sn.breakeven_scale(lambda s: s - 0.8) == pytest.approx(0.8, abs=0.005)
    assert math.isnan(sn.breakeven_scale(lambda s: 1.0))
