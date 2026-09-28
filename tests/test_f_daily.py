"""research/strategy_f_daily.py: the daily study behind book F (HANDOFF v3.1 section 7F), rebuilt from its definitions."""
from __future__ import annotations

import importlib.util
import json
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("strategy_f_daily",
                                               Path(__file__).resolve().parent.parent / "research" / "strategy_f_daily.py")
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)


def days(n, start=date(2016, 1, 4)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def flat(n, px=50.0, vol=2_000_000):
    """n flat days: $100M/day traded at $50, range +-1."""
    return [{"d": d, "o": px, "h": px + 1, "l": px - 1, "c": px, "v": vol} for d in days(n)]


def panel(**series):
    return S.Panel({k: v for k, v in series.items()})


# ------------------------------------------------------------------ loading
def test_load_folder_reads_kaggle_and_plain_csvs(tmp_path):
    (tmp_path / "AAPL_data.csv").write_text("date,open,high,low,close,volume,Name\n"
                                            "2013-02-08,67.7,68.4,66.9,67.9,158168416,AAPL\n"
                                            "2013-02-11,68.1,69.3,67.6,,129029425,AAPL\n")
    (tmp_path / "MU.csv").write_text("date,open,high,low,close,volume\n2024-01-02,85,86,84,85.5,1000\n")
    data = S.load_folder(tmp_path)
    assert set(data) == {"AAPL", "MU"}
    assert data["AAPL"] == [{"d": date(2013, 2, 8), "o": 67.7, "h": 68.4, "l": 66.9, "c": 67.9, "v": 158168416.0}]
    assert data["MU"][0]["c"] == 85.5


# ------------------------------------------------------------------ features (known by the event day's close)
def test_rvol_divides_by_the_prior_20_day_average_and_filters_use_prior_days():
    a = flat(25)
    a[22] = {**a[22], "v": 6_000_000}
    p = panel(AAA=a)
    f = p.features("AAA", 22)
    assert f["rvol"] == pytest.approx(3.0)
    assert f["dollar_vol20"] == pytest.approx(100e6) and f["prior_close"] == 50.0
    assert p.features("AAA", 19) is None                      # needs 20 prior days
    assert p.eligible("AAA", 22)
    b = flat(25, px=8.0, vol=20_000_000)                     # $160M/day but under $10
    assert not panel(BBB=b).eligible("BBB", 22)
    c = flat(25, vol=500_000)                                 # $25M/day
    assert not panel(CCC=c).eligible("CCC", 22)


def test_close_location_top_30_percent_of_the_range():
    assert S.close_loc({"h": 11, "l": 10, "c": 10.7}) == pytest.approx(0.7)
    assert S.close_loc({"h": 10, "l": 10, "c": 10}) is None


# ------------------------------------------------------------------ returns vs the equal-weight universe
def test_window_return_is_in_excess_of_the_equal_weight_universe():
    a, b = flat(30), flat(30)
    a[25] = {**a[25], "o": 50.0, "c": 55.0}                   # +10% open->close
    b[25] = {**b[25], "o": 50.0, "c": 51.0}                   # +2%
    p = panel(AAA=a, BBB=b)
    # EW of both names = +6%, so AAA's excess is +4%
    assert p.excess("AAA", 25, 25) == pytest.approx(0.04)
    assert p.excess("BBB", 25, 25) == pytest.approx(-0.04)


def test_hold_h_days_buys_the_next_open_and_sells_the_close_h_days_later():
    a, b = flat(40), flat(40)
    a[26] = {**a[26], "o": 50.0}
    a[30] = {**a[30], "c": 60.0}
    p = panel(AAA=a, BBB=b)
    e, x = S.hold_window(25, 5)
    assert (e, x) == (26, 30)
    assert p.excess("AAA", e, x) == pytest.approx(0.20 - 0.10)   # EW of (+20%, 0%)


# ------------------------------------------------------------------ the pre-registered tests
def spike(n=40, at=25, ret=0.05, rvol=3.0, loc=0.9):
    """A stock that closes `ret` up on day `at` on `rvol` x volume with the close at `loc` of the range."""
    a = flat(n)
    c = 50.0 * (1 + ret)
    lo, hi = 50.0, 50.0 + (c - 50.0) / loc if loc else 50.0
    a[at] = {**a[at], "o": 50.0, "h": hi, "l": lo, "c": c, "v": 2_000_000 * rvol}
    for i in range(at + 1, n):
        a[i] = {**a[i], "o": c, "h": c + 1, "l": c - 1, "c": c}
    return a


def test_momentum_cells_are_the_16_preregistered_ones():
    cells = S.momentum_cells()
    assert len(cells) == 16
    assert {(c["rvol_min"], c["up_min"], c["hold"]) for c in cells} == {
        (r, u, h) for r in (2.0, 3.0) for u in (0.03, 0.05) for h in (1, 5, 10, 20)}
    assert S.bonferroni_t(16) == pytest.approx(2.96, abs=0.01)


def test_momentum_event_needs_volume_move_and_close_near_high():
    p = panel(AAA=spike(), BBB=flat(40), CCC=flat(40))
    ev = p.events(S.momentum_rule(2.0, 0.03))
    assert [(s, i) for s, i in ev] == [("AAA", 25)]
    assert not p.events(S.momentum_rule(3.5, 0.03))           # volume too low
    assert not panel(AAA=spike(loc=0.5), BBB=flat(40)).events(S.momentum_rule(2.0, 0.03))   # closed mid-range


def test_control_is_a_big_up_day_on_normal_volume_wherever_it_closes():
    # 7F's control counts (8,359 / 1,017) reproduce only without the close-near-high condition (8,361 / 1,019)
    p = panel(AAA=spike(rvol=1.2), BBB=spike(rvol=2.5), CCC=flat(40), DDD=spike(rvol=1.2, loc=0.4))
    assert [s for s, _ in p.events(S.control_rule(0.03))] == ["AAA", "DDD"]


def test_down_side_mirrors_momentum_with_the_close_near_the_low():
    a = flat(40)
    a[25] = {**a[25], "o": 50.0, "h": 50.0, "l": 47.0, "c": 47.3, "v": 6_000_000}
    p = panel(AAA=a, BBB=flat(40))
    assert [s for s, _ in p.events(S.down_rule(2.0, 0.05))] == ["AAA"]


def test_gap_chase_buys_the_open_and_sells_the_same_close_net_of_10bp():
    a, b = flat(40), flat(40)
    a[25] = {**a[25], "o": 53.0, "h": 53.5, "l": 51.0, "c": 51.94}     # gaps +6%, fades 2%
    p = panel(AAA=a, BBB=b)
    ev = p.events(S.gap_rule(0.04))
    assert ev == [("AAA", 25)]
    r = p.trade_returns(ev, entry="open", hold=0, cost=S.COST)
    # AAA -2%, BBB 0%: EW -1%, excess -1%, net of 10 bp -1.1%
    assert r[0]["ret"] == pytest.approx(-0.011)
    assert not p.events(S.gap_rule(0.04, up=False))


def test_next_open_entry_is_net_of_the_round_trip_cost():
    p = panel(AAA=spike(), BBB=flat(40))
    r = p.trade_returns([("AAA", 25)], entry="next_open", hold=1, cost=S.COST)
    assert r[0]["ret"] == pytest.approx(-0.001)              # flat after the spike: 0 excess - 10 bp
    assert r[0]["entry"] == days(40)[26]


# ------------------------------------------------------------------ statistics
def test_newey_west_t_with_no_lags_is_the_plain_t():
    xs = [0.01, -0.02, 0.03, 0.005, -0.001, 0.02]
    n = len(xs)
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    assert S.nw_t(xs, 0) == pytest.approx(m / math.sqrt(var / n))


def test_newey_west_t_shrinks_with_positive_autocorrelation():
    xs = [0.02] * 5 + [-0.01] * 5 + [0.03] * 5 + [0.0] * 5    # overlapping holds repeat the same move
    assert abs(S.nw_t(xs, 3)) < abs(S.nw_t(xs, 0))


def test_summary_clusters_by_entry_day_and_splits_halves():
    rows = [{"entry": d, "ret": r, "sym": "X"} for d, r in
            zip(days(8), [0.01, 0.02, -0.01, 0.03, 0.0, 0.01, 0.02, -0.02])]
    rows.append({"entry": days(8)[0], "ret": 0.03, "sym": "Y"})
    s = S.summarize(rows, lag=1)
    assert s["n"] == 9 and s["days"] == 8
    assert s["mean_bp"] == pytest.approx(sum(r["ret"] for r in rows) / 9 * 1e4)
    assert s["win"] == pytest.approx(6 / 9)
    # 7F's "net result" is the mean of the daily means (its control rows reproduce that way)
    assert s["day_mean_bp"] == pytest.approx((0.02 + 0.02 - 0.01 + 0.03 + 0.0 + 0.01 + 0.02 - 0.02) / 8 * 1e4)
    assert [h["n"] for h in s["halves"]] == [5, 4]


# ------------------------------------------------------------------ end to end
def test_run_writes_every_test_and_the_reference_numbers(tmp_path):
    folder = tmp_path / "daily"
    folder.mkdir()
    for i, sym in enumerate(["AAA", "BBB", "NVDA"]):
        rows = spike(n=60, at=30 + i) if sym != "BBB" else flat(60)
        body = "\n".join(f"{r['d']},{r['o']},{r['h']},{r['l']},{r['c']},{r['v']}" for r in rows)
        (folder / f"{sym}.csv").write_text("date,open,high,low,close,volume\n" + body + "\n")
    out = tmp_path / "res.json"
    res = S.run(folder, out)
    saved = json.loads(out.read_text())
    assert saved == json.loads(json.dumps(res, default=str))
    names = [t["test"] for t in saved["tests"]]
    assert sum(n.startswith("momentum") for n in names) == 16
    assert {"control up>=3%", "control up>=5%", "gap up>=2%", "gap up>=4%", "gap down<=-2% (not pre-registered)"} <= set(names)
    assert sum(n.startswith("down") for n in names) == 16
    assert saved["bonferroni_t"] == pytest.approx(2.96, abs=0.01)
    assert saved["reference_2013_2018"]["gap up>=2%"] == {"n": 7160, "mean_bp": -29, "t": -8.2}
    down = next(t for t in saved["tests"] if t["test"].startswith("down") and t["hold"] == 1)
    assert down["cost_round_trip"] == S.COST                  # every test is net of the 10 bp round trip
    assert "periods" in saved["tests"][0] and "ai_2023_2026" in saved["tests"][0]
