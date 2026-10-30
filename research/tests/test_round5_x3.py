"""Tests for research/round5_x3.py (rules in research/round5_prereg.md section 4). Synthetic data only."""
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH.parent))

import round5_x3 as x3  # noqa: E402
import strategy_f_intraday as fi  # noqa: E402


def sessions(n, start=date(2016, 1, 4)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def bars(sym, closes, vols, days, spread=0.5):
    return pd.DataFrame({"date": days[:len(closes)], "act_symbol": sym, "open": closes,
                         "high": [c + spread for c in closes], "low": [c - spread for c in closes],
                         "close": closes, "volume": vols})


# ---------------------------------------------------------------- universe membership

def test_security_names_that_are_not_common_shares_are_dropped():
    keep = ["Apple Inc. - Common Stock", "United Rentals, Inc. Common Stock", "Unity Software Inc. Common Stock"]
    drop = ["Foo Acquisition Corp - Warrant", "Foo Acquisition Corp - Units", "Bar Corp - Rights",
            "Baz Bank 6.5% Series A Preferred Stock", "Qux Ltd American Depositary Shares", "Acme 5% Senior Notes due 2030",
            "Acme Corp Warrants"]
    assert not any(x3.bad_security_name(s) for s in keep)
    assert all(x3.bad_security_name(s) for s in drop)


def test_eligible_symbols_need_common_non_test_non_etf_rows():
    sym = pd.DataFrame({"act_symbol": ["AAA", "BBB", "CCC", "DDD", "EEE.W"],
                        "security_name": ["AAA Inc Common Stock", "BBB ETF", "CCC Test", "DDD Corp Common Stock",
                                          "EEE Corp Warrant"],
                        "is_etf": ["0", "1", "0", "0", "0"], "is_test_issue": ["0", "0", "1", "0", "0"]})
    assert x3.eligible_symbols(sym) == {"AAA", "DDD"}


def test_exclusion_lists_are_normalised():
    assert x3.norm("brk-b") == "BRK.B" and x3.norm("BF/B") == "BF.B" and x3.norm(" ko ") == "KO"
    ex = x3.excluded_symbols(["MMM", "BRK.B"], ["AAPL", "BF-B"], ["PLTR"], ["KO"])
    assert ex == {"MMM", "BRK.B", "AAPL", "BF.B", "PLTR", "KO"}


def test_calendar_keeps_only_dates_with_enough_names():
    days = sessions(3)
    df = pd.concat([bars(f"S{i}", [10.0] * 3, [1.0] * 3, days) for i in range(5)]
                   + [bars("ODD", [10.0], [1.0], [date(2016, 1, 9)])])        # a Saturday print, one name
    assert list(x3.calendar(df, min_names=3)) == days


# ---------------------------------------------------------------- point-in-time features

def test_features_match_the_repo_definitions_through_the_prior_close():
    days = sessions(40)
    rng = np.random.default_rng(1)
    closes = list(np.round(20 + np.cumsum(rng.normal(0, 0.5, 40)), 2))
    vols = list(rng.integers(1_000_000, 3_000_000, 40).astype(float))
    df = bars("AAA", closes, vols, days, spread=0.3)
    df["high"] = df["close"] + rng.uniform(0.1, 0.9, 40)
    df["low"] = df["close"] - rng.uniform(0.1, 0.9, 40)
    f = x3.add_features(df).set_index("date")
    ref = fi._features({"AAA": [{"d": d, "h": h, "l": lo, "c": c, "v": v} for d, h, lo, c, v in
                                zip(df["date"], df["high"], df["low"], df["close"], df["volume"])]})["AAA"]
    for i in (14, 15, 20, 39):
        d = days[i]
        assert f.loc[d, "prev_close"] == pytest.approx(ref[d]["close"])
        assert f.loc[d, "avg_vol14"] == pytest.approx(ref[d]["avg_vol14"])
        if ref[d]["atr"] is None:
            assert math.isnan(f.loc[d, "atr14"])
        else:
            assert f.loc[d, "atr14"] == pytest.approx(ref[d]["atr"])
        if i >= 20:
            assert f.loc[d, "dv20"] == pytest.approx(ref[d]["dv20"])
            assert f.loc[d, "vol20"] == pytest.approx(np.mean(vols[i - 20:i]))
    assert math.isnan(f.loc[days[13], "atr14"])      # 12 true ranges before day 13: no ATR yet
    assert f.loc[days[1], "ret"] == pytest.approx(closes[1] / closes[0] - 1)


def test_split_window_blocks_t_minus_20_through_t_plus_1():
    cal = np.array(sessions(60))
    days = list(cal)
    panel = pd.DataFrame({"act_symbol": "AAA", "date": days})
    splits = pd.DataFrame({"act_symbol": ["AAA", "ZZZ"], "ex_date": [days[30], days[5]]})
    blocked = x3.split_blocked(panel, splits, cal)
    hit = [d for d, b in zip(days, blocked) if b]
    assert hit == days[29:51]                         # t+1 = ex-date (t = 29) through t-20 = ex-date (t = 50)


def test_split_on_a_non_session_date_maps_to_the_next_session():
    cal = np.array(sessions(30))
    days = list(cal)
    panel = pd.DataFrame({"act_symbol": "AAA", "date": days})
    sat = days[10] + timedelta(days=(5 - days[10].weekday()) % 7 or 7)      # the Saturday after days[10]
    splits = pd.DataFrame({"act_symbol": ["AAA"], "ex_date": [sat]})
    nxt = next(d for d in days if d > sat)
    i = days.index(nxt)
    hit = [d for d, b in zip(days, x3.split_blocked(panel, splits, cal)) if b]
    assert hit == days[i - 1:min(i + 21, len(days))]


# ---------------------------------------------------------------- panel, signal, trade

def liquid(sym, days, jump_at=None, jump=1.05, next_close=None, vol_mult=3.0, base=50.0):
    closes = [base] * len(days)
    vols = [2_000_000.0] * len(days)
    if jump_at is not None:
        closes[jump_at] = base * jump
        vols[jump_at] = 2_000_000.0 * vol_mult
        for k in range(jump_at + 1, len(days)):
            closes[k] = next_close if next_close is not None else base * jump
    return bars(sym, closes, vols, days, spread=0.5)


def test_short_return_pays_the_next_sessions_dividend_and_excess_nets_the_universe():
    days = sessions(40)
    df = pd.concat([liquid("AAA", days, jump_at=30, next_close=50.0),
                    liquid("BBB", days), liquid("CCC", days)])
    divs = pd.DataFrame({"act_symbol": ["AAA"], "ex_date": [days[31]], "amount": [0.5]})
    symbols = pd.DataFrame({"act_symbol": ["AAA", "BBB", "CCC"], "security_name": ["A Common Stock"] * 3,
                            "is_etf": ["0"] * 3, "is_test_issue": ["0"] * 3, "last_seen": ["2026-10-24"] * 3})
    p = x3.build_panel(df, symbols, pd.DataFrame(columns=["act_symbol", "ex_date"]), divs, set(),
                       cal=np.array(days))
    row = p[(p.act_symbol == "AAA") & (p.date == days[30])].iloc[0]
    assert row.universe and row.next_ok
    assert row.short_ret == pytest.approx((52.5 - 50.0 - 0.5) / 52.5)
    univ = np.mean([(52.5 - 50.0 - 0.5) / 52.5, 0.0, 0.0])
    assert row.univ_short == pytest.approx(univ)
    tr = x3.signals(p, rvol_min=2.0)
    assert list(tr.act_symbol) == ["AAA"] and list(tr.date) == [days[30]]
    net = x3.net_returns(tr, side_bp=2.0)
    assert net["excess"].iloc[0] == pytest.approx(row.short_ret - 0.0004 - univ)
    assert net["raw"].iloc[0] == pytest.approx(row.short_ret - 0.0004)


def test_universe_floors_and_exclusions():
    days = sessions(40)
    cheap = liquid("LOW", days, base=8.0)                                  # prior close < $10
    thin = liquid("THN", days)
    thin["volume"] = 400_000.0                                             # 14-day volume < 1M and $ volume < 25M
    calm = liquid("CLM", days)
    calm["high"], calm["low"] = calm["close"] + 0.1, calm["close"] - 0.1   # ATR 0.2 < 0.50
    ok = liquid("OK", days)
    spx = liquid("SPX1", days)
    df = pd.concat([cheap, thin, calm, ok, spx])
    names = ["LOW", "THN", "CLM", "OK", "SPX1"]
    symbols = pd.DataFrame({"act_symbol": names, "security_name": ["X Common Stock"] * 5, "is_etf": ["0"] * 5,
                            "is_test_issue": ["0"] * 5, "last_seen": ["2026-10-24"] * 5})
    p = x3.build_panel(df, symbols, pd.DataFrame(columns=["act_symbol", "ex_date"]),
                       pd.DataFrame(columns=["act_symbol", "ex_date", "amount"]), {"SPX1"}, cal=np.array(days))
    on = p[p.date == days[30]].set_index("act_symbol")
    assert set(on.index[on.universe]) == {"OK"}
    assert "SPX1" not in set(p.act_symbol)                                 # excluded names never enter


def test_signal_needs_up_3pct_and_2x_volume_and_a_next_session_bar():
    days = sessions(40)
    up_lowvol = liquid("LV", days, jump_at=30, vol_mult=1.2)
    small = liquid("SM", days, jump_at=30, jump=1.02)
    gone = liquid("GN", days, jump_at=30).iloc[:31]                        # no bar after the signal day
    hit = liquid("HT", days, jump_at=30)
    df = pd.concat([up_lowvol, small, gone, hit])
    names = ["LV", "SM", "GN", "HT"]
    symbols = pd.DataFrame({"act_symbol": names, "security_name": ["X Common Stock"] * 4, "is_etf": ["0"] * 4,
                            "is_test_issue": ["0"] * 4, "last_seen": ["2026-10-24"] * 4})
    p = x3.build_panel(df, symbols, pd.DataFrame(columns=["act_symbol", "ex_date"]),
                       pd.DataFrame(columns=["act_symbol", "ex_date", "amount"]), set(), cal=np.array(days))
    assert set(x3.signals(p, rvol_min=2.0).act_symbol) == {"HT"}
    assert set(x3.signals(p, rvol_max=1.5).act_symbol) == {"LV"}             # the normal-volume control
    assert x3.counts(p, rvol_min=2.0)["no_next_session_bar"] == 1


# ---------------------------------------------------------------- statistics

def test_stats_cluster_by_day_and_profit_factor_is_over_trades():
    t = pd.DataFrame({"date": [date(2016, 1, 4)] * 2 + [date(2016, 1, 5), date(2021, 3, 1), date(2021, 3, 2)],
                      "x": [0.02, 0.00, -0.01, 0.03, 0.01]})
    s = x3.stats(t, "x")
    day = np.array([0.01, -0.01, 0.03, 0.01])
    assert s["trades"] == 5 and s["days"] == 4
    assert s["day_mean_bp"] == pytest.approx(day.mean() * 1e4)
    assert s["t"] == pytest.approx(day.mean() / (day.std(ddof=1) / 2))
    assert s["pf"] == pytest.approx(0.06 / 0.01)
    assert s["win"] == pytest.approx(3 / 5)
    h = x3.halves(t, "x")
    assert h["2016-2020"]["days"] == 2 and h["2021-2026"]["days"] == 2


def test_verdict_needs_all_four():
    good = {"pf": 1.2, "t": 3.0, "halves": {"2016-2020": {"day_mean_bp": 5}, "2021-2026": {"day_mean_bp": 3}},
            "stress_day_mean_bp": 1.0}
    assert x3.verdict(good)["pass"]
    for k, v in [("pf", 1.05), ("t", 2.5), ("stress_day_mean_bp", -0.1)]:
        assert not x3.verdict({**good, k: v})["pass"]
    bad_half = {**good, "halves": {"2016-2020": {"day_mean_bp": 5}, "2021-2026": {"day_mean_bp": -1}}}
    assert not x3.verdict(bad_half)["pass"]
