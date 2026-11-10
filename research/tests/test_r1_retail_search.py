import math
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import r1_retail_search as R  # noqa: E402


def test_signal_month_is_35_days_before_and_always_ended_before_r():
    assert R.signal_month(date(2024, 3, 5)) == (2024, 1)
    assert R.signal_month(date(2024, 2, 28)) == (2024, 1)
    assert R.signal_month(date(2024, 5, 21)) == (2024, 4)


def test_shift_month_crosses_years():
    assert R.shift_month((2024, 1), -2) == (2023, 11)
    assert R.shift_month((2023, 11), -12) == (2022, 11)
    assert R.shift_month((2023, 12), 1) == (2024, 1)


def test_growth_uses_three_months_against_a_year_earlier():
    s = {(2023, m): 50.0 for m in range(1, 13)}
    s.update({(2022, m): 40.0 for m in range(1, 13)})
    s[(2023, 1)] = 80.0
    # R = 2023-03-05 -> M = 2023-01, months 2022-11..2023-01 vs 2021-11..2022-01 (missing) -> None
    assert R.growth(s, date(2023, 3, 5)) is None
    # R = 2023-05-10 -> M = 2023-04: (50+50+50)/3 vs (40+40+40)/3
    assert math.isclose(R.growth(s, date(2023, 5, 10)), math.log(50 / 40))
    s[(2022, 3)] = 0.0
    s[(2022, 2)] = 0.0
    s[(2022, 4)] = 0.0
    assert R.growth(s, date(2023, 5, 10)) is None


def test_surprise_needs_four_prior_reports_with_g():
    rs = [date(2020, 1, 1), date(2020, 4, 1), date(2020, 7, 1), date(2020, 10, 1), date(2021, 1, 1), date(2021, 4, 1)]
    g = dict(zip(rs, [0.1, 0.2, 0.3, 0.4, 0.5, None]))
    s = R.surprises(rs, g)
    assert s[rs[3]] is None                      # only 3 before it
    assert math.isclose(s[rs[4]], 0.5 - 0.25)    # mean of 0.1..0.4
    assert s[rs[5]] is None                      # own g missing
    g[rs[1]] = None
    assert R.surprises(rs, g)[rs[4]] is None     # a prior report without g


def test_window_return_is_stock_minus_spy():
    c = {("WMT", date(2024, 1, 2)): 100.0, ("WMT", date(2024, 1, 3)): 103.0,
         ("SPY", date(2024, 1, 2)): 400.0, ("SPY", date(2024, 1, 3)): 404.0}
    assert math.isclose(R.window_return(c, "WMT", date(2024, 1, 2), date(2024, 1, 3)), 0.03 - 0.01)
    assert R.window_return(c, "COST", date(2024, 1, 2), date(2024, 1, 3)) is None


def test_clustered_mean_matches_plain_t_with_singleton_clusters():
    x = [0.01, -0.02, 0.03, 0.005, 0.0]
    res = R.clustered_mean(x, ["a", "b", "c", "d", "e"])
    mu = sum(x) / 5
    sd = math.sqrt(sum((v - mu) ** 2 for v in x) / 4)
    assert math.isclose(res["t"], mu / (sd / math.sqrt(5)), rel_tol=1e-9)


def test_clustering_widens_the_error_when_a_week_moves_together():
    x = [0.02, 0.02, 0.02, -0.01, 0.0, 0.01]
    loose = R.clustered_mean(x, list("abcdef"))["t"]
    tight = R.clustered_mean(x, ["w1", "w1", "w1", "w2", "w3", "w4"])["t"]
    assert tight < loose


def test_build_trades_signs_by_surprise_and_charges_cost():
    rs = [date(2019, 2, 20), date(2019, 5, 20), date(2019, 8, 20), date(2019, 11, 20), date(2020, 2, 20)]
    series = {}
    for y in range(2017, 2021):
        for m in range(1, 13):
            series[(y, m)] = 50.0
    series[(2019, 12)] = 90.0                    # surge in M-2..M for R = 2020-02-20 (M = 2020-01)
    events = [{"symbol": "WMT", "report_session": r, "entry_session": date(r.year, r.month, r.day - 1)} for r in rs]
    r, e = rs[-1], date(2020, 2, 19)
    closes = {("WMT", e): 100.0, ("WMT", r): 102.0, ("SPY", e): 300.0, ("SPY", r): 300.0,
              ("WMT", date(2020, 2, 21)): 101.0, ("SPY", date(2020, 2, 21)): 300.0}
    trades, skips = R.build_trades(events, {"WMT": series}, closes, [e, r, date(2020, 2, 21)],
                                   sample=(date(2016, 1, 1), date(2026, 1, 1)))
    assert len(trades) == 1 and trades[0]["side"] == 1
    assert math.isclose(trades[0]["net"], 0.02 - 0.0005)
    assert math.isclose(trades[0]["abnormal_3d"], 0.01)
    assert skips["fewer_than_4_prior"] == 4


def test_evaluate_fails_when_one_half_is_negative():
    trades = []
    for i in range(40):
        r = date(2018, 1, 1).replace(year=2018 + i // 10, month=1 + (i % 10))
        good = i >= 20
        trades.append({"symbol": "WMT", "R": r, "s": 1.0 + i, "side": 1, "abnormal": 0.03 if good else -0.001,
                       "gross": 0.03 if good else -0.001, "net": (0.03 if good else -0.001) - 0.0005,
                       "abnormal_3d": 0.0})
    res = R.evaluate(trades)
    assert res["halves"][0]["mean"] < 0 and res["verdict"] == "FAIL"
