"""Tests for research/strategies_new.py (new strategy candidates F1-F4, see research/strategies_new_prereg.md)."""
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


# ---------------------------------------------------------------- runner

def test_period_labels_match_the_bcd_study():
    assert sn.period_of(date(2005, 1, 3)) == "2005-14"
    assert sn.period_of(date(2014, 12, 31)) == "2005-14"
    assert sn.period_of(date(2015, 1, 2)) == "2015-20"
    assert sn.period_of(date(2025, 4, 1)) == "2025-26"


def _days(n, start=date(2026, 1, 5)):
    from datetime import timedelta
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_collect_skips_sessions_without_prior_vix_and_tags_period_and_year():
    M = _synthetic(nd=12)
    days = _days(12)
    vixp = np.full(12, VIX); vixp[3] = np.nan
    df = sn.collect("F2", days, M, vixp, sn.COSTS["mid-1c"])
    assert 3 not in set(df.i)
    assert set(df.columns) >= {"i", "date", "period", "year", "ret", "pnl", "risk", "why"}
    assert (df.period == "2025-26").all() and (df.year == 2026).all()


def test_f2_trend_is_the_subset_of_f2_that_passes_the_gate():
    M = _synthetic(nd=70, seed=5)
    M["C"] = M["C"] * np.linspace(1, 1.3, 70)[:, None]       # rising market so the gate opens
    M["O"] = M["O"] * np.linspace(1, 1.3, 70)[:, None]
    days = _days(70)
    vixp = np.full(70, VIX)
    all_ = sn.collect("F2", days, M, vixp, sn.COSTS["mid-1c"], min_credit_override=None)
    tr = sn.collect("F2-trend", days, M, vixp, sn.COSTS["mid-1c"], min_credit_override=None)
    assert len(tr) > 0 and set(tr.i) <= set(all_.i)
    assert min(tr.i) >= 50
    closes = M["C"][:, -1]
    assert all(sn.sma_gate(closes, i) for i in tr.i)


def test_summarize_reports_periods_years_drawdown_and_capacity():
    df = pd.DataFrame({"i": range(6), "date": _days(6), "period": ["2025-26"] * 6, "year": [2026] * 6,
                       "ret": [0.1, -0.2, 0.1, 0.1, -0.05, 0.1], "pnl": [0.15, -0.3, 0.15, 0.15, -0.08, 0.15],
                       "risk": [1.5] * 6, "why": ["take 50%"] * 6})
    s = sn.summarize(df)
    assert s["all"]["n"] == 6
    assert s["2025-26"]["n"] == 6 and s["by_year"]["2026"]["n"] == 6
    assert s["all"]["max_dd_usd_1lot"] == pytest.approx(-30.0)
    assert s["all"]["lots_at_cap"] == 2
    assert s["all"]["avg_usd_1lot"] == pytest.approx(df.pnl.mean() * 100)


def test_correlation_with_d_uses_shared_sessions_only():
    a = pd.DataFrame({"date": _days(5), "pnl": [1.0, 2.0, 3.0, 4.0, 5.0]})
    b = pd.DataFrame({"date": _days(5)[1:], "pnl": [2.0, 3.0, 4.0, 5.0]})
    assert sn.corr_with(a, b) == pytest.approx(1.0)


def _res(t_is=3.5, nat_oos=(1.0, 2.0), be=0.8, lots=1):
    mid = {"2005-14": {"t": t_is}, "all": {"lots_at_cap": lots}}
    nat = {"2015-20": {"avg": nat_oos[0]}, "2025-26": {"avg": nat_oos[1]}}
    return mid, nat, be


def test_decision_rule_needs_every_pre_registered_check():
    assert sn.decision(*_res())["pass"]
    assert not sn.decision(*_res(t_is=2.5))["pass"]
    assert not sn.decision(*_res(nat_oos=(1.0, -0.1)))["pass"]
    assert not sn.decision(*_res(be=0.9))["pass"]
    assert not sn.decision(*_res(be=float("nan")))["pass"]
    assert not sn.decision(*_res(lots=0))["pass"]
    assert sn.decision(*_res(be=0.9))["checks"]["breakeven_s<=0.85"] is False


# ---------------------------------------------------------------- addendum 1: metric fixes and G1/G2

def test_multi_session_leg_adds_a_night_and_a_session_per_session_ahead():
    k = 76
    want = (0.80 ** 2 * V * (77 - k) / 78 + 3 * 0.60 ** 2 * V + 2 * 0.80 ** 2 * V
            + 0.80 ** 2 * V * sn.rth_frac(-1))
    assert sn.leg_sd(VIX, expiry=3, d=0, k=k) ** 2 == pytest.approx(want)


def test_capacity_is_per_trade_and_skips_trades_that_do_not_fit():
    df = pd.DataFrame({"i": range(4), "date": _days(4), "period": ["2025-26"] * 4, "year": [2026] * 4,
                       "ret": [0.1, 0.1, 0.1, -0.1], "pnl": [0.2, 0.2, 0.5, -0.4], "risk": [1.5, 2.0, 4.0, 1.5],
                       "why": ["time"] * 4})
    s = sn.summarize(df)["all"]
    assert s["lots_at_cap"] == 1                    # median of [2, 1, 0, 2] -> 1.5 -> floor 1
    assert s["share_trades_fitting_cap"] == pytest.approx(0.75)
    yrs = (df.date.max() - df.date.min()).days / 365.25
    assert s["usd_per_year_at_cap"] == pytest.approx((0.2 * 2 + 0.2 * 1 + 0 - 0.4 * 2) * 100 / yrs)


def test_collect_can_restrict_to_a_fixed_set_of_sessions():
    M = _synthetic(nd=12)
    days = _days(12)
    vixp = np.full(12, VIX)
    keep = {days[2], days[7]}
    df = sn.collect("D", days, M, vixp, sn.COSTS["mid-1c"], keep_dates=keep)
    assert set(df.date) == keep


def test_month_turn_enters_second_to_last_session_and_exits_third_session():
    days = [date(2026, 1, d) for d in (26, 27, 28, 29, 30)] + [date(2026, 2, d) for d in (2, 3, 4, 5, 6)] + \
           [date(2026, 2, d) for d in (23, 24, 25, 26, 27)] + [date(2026, 3, d) for d in (2, 3, 4, 5)]
    pairs = sn.month_turn_entries(days)
    assert (days[3], days[7]) in [(days[i], days[j]) for i, j in pairs]          # Jan 29 -> Feb 4
    assert (days[13], days[17]) in [(days[i], days[j]) for i, j in pairs]        # Feb 26 -> Mar 4
    assert len(pairs) == 2


def test_vix_stretch_uses_the_prior_close_against_the_ten_before_it_without_overlap():
    vixp = np.r_[np.full(12, 15.0), 19.0, 19.5, 20.0, np.full(10, 15.0)]    # vixp[i] = VIX close of session i-1
    pairs = sn.vix_stretch_entries(vixp, n_sessions=len(vixp), hold=5)
    assert pairs[0] == (12, 17)
    assert all(i >= 17 for i, _ in pairs[1:])                            # no entry while a position is open
    assert sn.vix_stretch_entries(np.full(20, 15.0), 20, 5) == []


def test_g_trade_is_a_call_debit_spread_expiring_on_the_exit_session():
    M = _synthetic(nd=8)
    r = sn.run_G(M, 1, 6, VIX, sn.COSTS["mid-1c"])
    f = sn.REBASE / M["O"][1, 0]
    S0, S1 = M["C"][1, 76] * f, M["C"][6, 70] * f
    assert r["legs"] == [("C", round(S0), 1, 5), ("C", round(S0) + 5, -1, 5)]
    assert 0 < r["debit"] < 5
    assert r["und_bp"] == pytest.approx((S1 / S0 - 1) * 1e4 - 2.0)


def test_leg_cost_follows_days_left_at_the_time_of_the_fill():
    legs = sn.legs_put_spread(765.0, 3.0, expiry=1)
    nat = sn.COSTS["natural"]
    assert sn._cost(legs, nat, d=0) == pytest.approx(2 * (0.03 + sn.FEE))     # opened as next-day legs
    assert sn._cost(legs, nat, d=1) == pytest.approx(2 * (0.02 + sn.FEE))     # closed on expiry day
    assert sn._cost([("C", 765, 1, 5)], nat, d=0) == pytest.approx(0.03 + sn.FEE)


# ---------------------------------------------------------------- vega sensitivity (hold-to-close, exit marked at the day's VIX)

def test_exit_mark_at_a_higher_vix_helps_the_calendar_and_hurts_the_condor():
    cost = {0: 0.01, 1: 0.01}
    cal = sn.legs_calendar(765.0)
    flat = sn.debit_trade(_flat_path(765.0, 5, 70), cal, VIX, cost, tp=math.inf, stop=math.inf)
    up = sn.debit_trade(_flat_path(765.0, 5, 70), cal, VIX, cost, tp=math.inf, stop=math.inf, vix_mark=VIX * 1.2)
    assert up["pnl"] > flat["pnl"]
    ic = sn.legs_condor(765.0, 3.0)
    flat = sn.credit_trade(_flat_path(765.0, 5, 70), ic, VIX, cost, 2, tp=-math.inf, stop=math.inf, min_credit=None)
    up = sn.credit_trade(_flat_path(765.0, 5, 70), ic, VIX, cost, 2, tp=-math.inf, stop=math.inf, min_credit=None,
                         vix_mark=VIX * 1.2)
    assert up["pnl"] < flat["pnl"]


def test_vega_check_pairs_hold_to_close_runs_with_and_without_the_same_day_vix():
    M = _synthetic(nd=6)
    days = _days(6)
    vixp = np.full(6, VIX)
    vix_today = np.full(6, VIX * 1.1)
    out = sn.vega_check("F3", days, M, vixp, vix_today, sn.COSTS["mid-1c"])
    assert set(out.columns) >= {"date", "ret_prior_vix", "ret_day_vix"}
    assert (out.ret_day_vix > out.ret_prior_vix).all()
