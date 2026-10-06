"""Tests for research/vix_carry.py (V1 in research/vix_carry_prereg.md) and research/fetch_cboe_vx.py."""
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH.parent))

import fetch_cboe_vx as fx  # noqa: E402
import vix_carry as vc  # noqa: E402


def weekdays(lo: date, hi: date) -> list[date]:
    out, d = [], lo
    while d <= hi:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------- settlement dates

def test_vx_settlement_is_30_days_before_next_months_third_friday():
    s = vc.nyse_session
    assert vc.vx_settlement(2018, 1, s) == date(2018, 1, 17)
    assert vc.vx_settlement(2018, 2, s) == date(2018, 2, 14)
    assert vc.vx_settlement(2024, 12, s) == date(2024, 12, 18)       # third Friday of January 2025 is the 17th


def test_vx_settlement_moves_back_when_the_friday_is_a_holiday():
    s = vc.nyse_session
    assert vc.vx_settlement(2025, 3, s) == date(2025, 3, 18)        # Good Friday 2025-04-18 -> Thursday - 30 days
    assert vc.vx_settlement(2026, 5, s) == date(2026, 5, 19)        # Juneteenth 2026-06-19


def test_fetch_month_list_and_archive_url():
    months = fx.months(date(2007, 11, 1), date(2008, 2, 28))
    assert months[0] == (2007, 11) and (2008, 1) in months
    assert fx.archive_url(2008, 3) == fx.URL_OLD.format(code="H", yy="08")


# ---------------------------------------------------------------- CSV readers

def test_read_cboe_futures_csv_skips_a_disclaimer_and_zero_settles(tmp_path):
    p = tmp_path / "VX_2018-02.csv"
    p.write_text("CFE data is compiled for the convenience of site visitors\n"
                 "Trade Date,Futures,Open,High,Low,Close,Settle,Change,Total Volume,EFP,Open Interest\n"
                 "2018-01-02,F (Feb 2018),12.5,12.9,12.4,12.6,12.65,0.1,100,0,500\n"
                 "01/03/2018,F (Feb 2018),12.5,12.9,12.4,12.6,0,0.1,100,0,500\n"
                 "2018-01-04,F (Feb 2018),12.5,12.9,12.4,12.6,12.40,0.1,100,0,500\n")
    s = vc.read_cboe_csv(p)
    assert list(s.index) == [date(2018, 1, 2), date(2018, 1, 4)]
    assert s.iloc[0] == pytest.approx(12.65)


def test_read_index_csv(tmp_path):
    p = tmp_path / "VIX3M_History.csv"
    p.write_text("DATE,OPEN,HIGH,LOW,CLOSE\n01/02/2008,24.0,24.5,23.5,24.1\n01/03/2008,24.1,24.9,23.9,24.6\n")
    s = vc.read_index_csv(p)
    assert s[date(2008, 1, 3)] == pytest.approx(24.6)


# ---------------------------------------------------------------- roll schedule and index

S = [date(2018, 1, 3), date(2018, 1, 10), date(2018, 1, 17), date(2018, 1, 24)]
SESSIONS = weekdays(date(2018, 1, 3), date(2018, 1, 23))


def test_roll_weights_run_from_dt_minus_1_over_dt_down_to_zero():
    sch = vc.roll_schedule(SESSIONS, S)
    first = sch.loc[date(2018, 1, 3)]
    assert first.front == S[1] and first.second == S[2]
    assert first.w1 == pytest.approx(4 / 5)                       # 5 sessions 3rd..9th, 4 after the 3rd
    assert sch.loc[date(2018, 1, 9)].w1 == pytest.approx(0.0)       # last session before the front settles
    nxt = sch.loc[date(2018, 1, 10)]
    assert nxt.front == S[2] and nxt.second == S[3] and nxt.w1 == pytest.approx(4 / 5)
    assert date(2018, 1, 17) not in sch.index                      # needs a contract past S[3]


def test_index_return_uses_yesterdays_weights_and_contracts():
    days = weekdays(date(2018, 1, 3), date(2018, 1, 16))
    p1 = pd.Series(np.linspace(12.0, 12.9, len(days)), index=days)
    p2 = pd.Series(np.linspace(13.0, 13.9, len(days)), index=days)
    p3 = pd.Series(np.linspace(14.0, 14.9, len(days)), index=days)
    prices = {S[1]: p1, S[2]: p2, S[3]: p3}
    R, info = vc.index_returns(days, S, prices)
    t0, t1 = date(2018, 1, 3), date(2018, 1, 4)
    w = 4 / 5
    exp = (w * p1[t1] + (1 - w) * p2[t1]) / (w * p1[t0] + (1 - w) * p2[t0]) - 1
    assert R[t1] == pytest.approx(exp)
    # the Tuesday before S[1] holds only the second month, so S[1]'s missing settle that day doesn't matter
    tue, wed = date(2018, 1, 9), date(2018, 1, 10)
    prices[S[1]] = p1.drop(wed)
    R2, _ = vc.index_returns(days, S, prices)
    assert R2[wed] == pytest.approx(p2[wed] / p2[tue] - 1)
    assert info["missing"] == 0


def test_missing_settles_carry_forward_two_sessions_then_go_missing():
    days = weekdays(date(2018, 1, 3), date(2018, 1, 9))
    p1 = pd.Series(12.0, index=days)
    p2 = pd.Series(13.0, index=days)
    gap = [date(2018, 1, 4), date(2018, 1, 5), date(2018, 1, 8)]
    prices = {S[1]: p1.drop(gap), S[2]: p2.drop(gap), S[3]: pd.Series(dtype=float)}
    R, info = vc.index_returns(days, S, prices)
    assert R[date(2018, 1, 4)] == pytest.approx(0.0) and R[date(2018, 1, 5)] == pytest.approx(0.0)
    assert np.isnan(R[date(2018, 1, 8)]) and info["missing"] >= 1


# ---------------------------------------------------------------- positions and strategy returns

D = weekdays(date(2018, 1, 1), date(2018, 1, 12))


def test_position_uses_the_ratio_from_two_closes_back():
    c = pd.Series([0.9, 1.1, 0.9, 0.9, 0.9, 1.2, 0.9, 0.9, 0.9, 0.9], index=D)
    p = vc.positions(c, D, 1.0)
    # p(t) = 1 if c(t-1) < 1: first session has no c(t-1) -> flat
    assert list(p) == [0, 1, 0, 1, 1, 1, 0, 1, 1, 1]


def test_missing_ratio_keeps_the_previous_position():
    c = pd.Series([0.9, 0.9, np.nan, 1.2, 0.9], index=D[:5])
    assert list(vc.positions(c, D[:5], 1.0)) == [0, 1, 1, 1, 0]


def test_strategy_return_applies_leverage_fee_costs_and_sale_fee():
    days = D[:5]
    R = pd.Series([0.0, 0.02, -0.04, 0.01, 0.03], index=days)
    p = pd.Series([1, 1, 0, 0, 0], index=days, dtype=float)
    s = vc.strategy(R, p, "mid1")
    fee = 0.0095 / 252
    assert s.iloc[0] == pytest.approx(0.0)
    assert s.iloc[1] == pytest.approx(-0.5 * 0.02 - fee - 3e-4)              # entered at close 0: cost lands here
    assert s.iloc[2] == pytest.approx(-0.5 * -0.04 - fee)                    # p(1)=1 held over session 2
    assert s.iloc[3] == pytest.approx(-(3e-4 + 0.3e-4))                      # exit at close 2: cost + sale fee
    assert s.iloc[4] == pytest.approx(0.0)


def test_missing_index_return_earns_nothing_that_session():
    days = D[:3]
    R = pd.Series([0.0, np.nan, 0.02], index=days)
    p = pd.Series([1, 1, 1], index=days, dtype=float)
    s = vc.strategy(R, p, "zero")
    assert s.iloc[1] == 0.0 and s.iloc[2] == pytest.approx(-0.01 - 0.0095 / 252)


# ---------------------------------------------------------------- stats and bar

def test_newey_west_t_is_smaller_for_positively_autocorrelated_returns():
    rng = np.random.default_rng(1)
    e = rng.normal(0.001, 0.01, 3000)
    x = pd.Series(np.convolve(e, np.ones(5) / 5, mode="same"))
    st = vc.stats(x)
    assert st["t_nw"] < st["t_iid"] and st["t"] == pytest.approx(st["t_nw"])


def test_stats_drawdown_and_worst():
    x = pd.Series([0.1, -0.5, 0.2])
    st = vc.stats(x)
    assert st["worst"] == pytest.approx(-0.5)
    assert st["max_dd"] == pytest.approx(-0.5)                                # 1.1 -> 0.55 -> 0.66


def test_pass_bar():
    good = {"is": {"mid1": {"mean": 1e-4}}, "oos": {"mid1": {"t": 2.5}, "taker": {"mean": 1e-5}}}
    assert vc.passes(good)
    assert not vc.passes({**good, "is": {"mid1": {"mean": -1e-5}}})
    assert not vc.passes({**good, "oos": {"mid1": {"t": 2.2}, "taker": {"mean": 1e-5}}})
    assert not vc.passes({**good, "oos": {"mid1": {"t": 2.5}, "taker": {"mean": -1e-5}}})


def test_svxy_validation_correlation():
    days = weekdays(date(2018, 3, 1), date(2019, 6, 28))
    rng = np.random.default_rng(3)
    R = pd.Series(rng.normal(0, 0.04, len(days)), index=days)
    px = pd.Series(100 * np.cumprod(1 - 0.5 * R.values - 0.0095 / 252), index=days)
    v = vc.validate(R, px)
    assert v["corr"] == pytest.approx(1.0, abs=1e-9) and v["valid"]
    assert vc.validate(R, px.iloc[:100])["valid"] is None                    # too short to judge


# ---------------------------------------------------------------- end to end

def write_synthetic(folder: Path):
    """Two years of synthetic VX contracts in steady contango (each future 1.0 above the next nearer one and
    sliding toward spot), VIX 14 and VIX3M 16: the strategy should be short throughout and make money."""
    (folder / "vx").mkdir(parents=True)
    sess = [d for d in weekdays(date(2013, 1, 1), date(2015, 6, 30)) if vc.nyse_session(d)]
    spot = 14.0
    for y in range(2012, 2016):
        for m in range(1, 13):
            st = vc.vx_settlement(y, m, vc.nyse_session)
            if st < date(2013, 1, 1) or st > date(2015, 9, 30):
                continue
            rows = ["Trade Date,Futures,Open,High,Low,Close,Settle,Change,Total Volume,EFP,Open Interest"]
            for d in sess:
                if d >= st or (st - d).days > 250:
                    continue
                f = spot + 2.0 * (st - d).days / 30
                rows.append(f"{d.isoformat()},F,{f},{f},{f},{f},{f:.4f},0,1,0,1")
            (folder / "vx" / f"VX_{y}-{m:02d}.csv").write_text("\n".join(rows) + "\n")
    idx = "\n".join(f"{d.strftime('%m/%d/%Y')},{spot},{spot},{spot},{spot}" for d in sess)
    (folder / "VIX_History.csv").write_text("DATE,OPEN,HIGH,LOW,CLOSE\n" + idx + "\n")
    idx3 = "\n".join(f"{d.strftime('%m/%d/%Y')},16,16,16,16" for d in sess)
    (folder / "VIX3M_History.csv").write_text("DATE,OPEN,HIGH,LOW,CLOSE\n" + idx3 + "\n")


def test_run_end_to_end(tmp_path):
    data, out = tmp_path / "cboe", tmp_path / "out"
    write_synthetic(data)
    res = vc.run(data, out)
    oos = res["V1"]["oos"]["mid1"]
    assert oos["n"] > 100 and oos["mean"] > 0 and oos["held"] > 0.95
    assert res["validation"]["valid"] is None                                 # no SVXY file
    assert (out / "vix_carry.md").exists() and (out / "vix_carry_results.json").exists()
    assert "threshold 0.95" in res["sensitivities"]
