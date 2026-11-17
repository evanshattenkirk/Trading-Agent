"""KP1 nowcasts, naive baselines and walk-forward scoring (rules: research/kp1_kpi_prereg.md)."""
import math
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kp1_kpi_nowcast as N  # noqa: E402
from kp1_fetch import quarter_bounds  # noqa: E402
from kp1_inputs import MonthRow  # noqa: E402

URL = "https://example.com"


def month(m, published, funded=27e6, equity=100e9, options=200e6, crypto=10e9, event=0.0, margin=20e9,
          sweep=30e9):
    return MonthRow(month=m, published=date.fromisoformat(published), funded_customers=funded,
                    equity_notional=equity, options_contracts=options, crypto_notional=crypto,
                    event_contracts=event, margin_book=margin, cash_sweep=sweep, sec_lending_revenue=0.0)


def inputs(actuals=None, dates=None, monthly=(), blocks=(), heights=None, coinbase=None):
    return N.Inputs(actuals=actuals or {}, report_dates=dates or {}, monthly=list(monthly),
                    mara_blocks=list(blocks), heights=heights or {}, coinbase=coinbase or {})


# ---------- HOOD ----------

def test_trading_days_use_the_nyse_calendar():
    assert N.trading_days("2025-01") == 20  # Jan 1, Jan 9 (national day of mourning), Jan 20
    assert N.trading_days("2026-07") == 22  # Jul 3 observed
    assert N.calendar_days("2024-02") == 29


def test_monthly_release_on_report_day_is_excluded():
    rows = [month("2026-07", "2026-08-12"), month("2026-08", "2026-10-27"), month("2026-06", "2026-07-10")]
    got = N.published_months(rows, "2026Q3", date(2026, 10, 27))
    assert [m.month for m in got] == ["2026-07"]


def _hood(prior_lines=None, cur_months=None, prior_months=None, prior_extra=None):
    prior = {"funded_customers": 26.8e6, "revenue": 0.0, "net_interest": 400e6, "other_revenues": 100e6,
             "rev_options": 300e6, "rev_equities": 100e6, "rev_crypto": 200e6, "rev_other_transaction": 50e6}
    if prior_lines is not None:
        prior = {k: v for k, v in prior.items() if not k.startswith("rev_") or k in prior_lines}
        prior.update({k: v for k, v in prior_lines.items()})
    prior["revenue"] = sum(v for k, v in prior.items() if k.startswith("rev_")) + prior["net_interest"] \
        + prior["other_revenues"]
    prior.update(prior_extra or {})
    actuals = {("HOOD", "2026Q2"): prior, ("HOOD", "2026Q1"): {"funded_customers": 26.0e6, "revenue": 1.0e9}}
    dates = {("HOOD", "2026Q2"): date(2026, 7, 29), ("HOOD", "2026Q3"): date(2026, 10, 27)}
    pm = prior_months or [month("2026-04", "2026-05-13"), month("2026-05", "2026-06-10")]
    cm = cur_months or [month("2026-07", "2026-08-12"), month("2026-08", "2026-09-10")]
    return inputs(actuals, dates, pm + cm)


def test_funded_nowcast_is_last_month_plus_its_change():
    data = _hood(cur_months=[month("2026-07", "2026-08-12", funded=27.0e6),
                             month("2026-08", "2026-09-10", funded=27.2e6)])
    model, naive = N.nowcasts("HOOD", "2026Q3", data)["funded_customers"]
    assert model == 27.4e6
    assert naive == 26.8e6 + (26.8e6 - 26.0e6)


def test_funded_month_one_only_uses_prior_quarter_end():
    data = _hood(cur_months=[month("2026-07", "2026-08-12", funded=27.0e6)])
    model, _ = N.nowcasts("HOOD", "2026Q3", data)["funded_customers"]
    assert math.isclose(model, 27.0e6 + (27.0e6 - 26.8e6))


def test_hood_revenue_lines_scale_with_drivers():
    base = N.nowcasts("HOOD", "2026Q3", _hood())["revenue"][0]
    doubled = _hood(cur_months=[month("2026-07", "2026-08-12", equity=200e9, options=400e6, crypto=20e9,
                                      margin=40e9, sweep=60e9),
                                month("2026-08", "2026-09-10", equity=200e9, options=400e6, crypto=20e9,
                                      margin=40e9, sweep=60e9)])
    got = N.nowcasts("HOOD", "2026Q3", doubled)["revenue"][0]
    lines = N.hood_revenue_lines("2026Q3", _hood())
    # same daily rate in both quarters: the line scales by (quarter days / published-month days)
    td = N.trading_days
    assert math.isclose(lines["rev_options"], 300e6 * (sum(td(m) for m in ("2026-07", "2026-08", "2026-09"))
                                                       / (td("2026-07") + td("2026-08")))
                        / (sum(td(m) for m in ("2026-04", "2026-05", "2026-06")) / (td("2026-04") + td("2026-05"))))
    assert math.isclose(lines["rev_crypto"], 200e6 * (92 / 62) / (91 / 61))
    assert lines["rev_other_transaction"] == 50e6 and lines["other_revenues"] == 100e6
    transaction = lines["rev_options"] + lines["rev_equities"] + lines["rev_crypto"]
    assert math.isclose(got - base, transaction + lines["net_interest"])  # each line and NII doubled
    assert math.isclose(lines["net_interest"], 400e6)  # same base -> NII unchanged


def test_missing_prior_line_contributes_zero():
    data = _hood(cur_months=[month("2026-07", "2026-08-12", event=6e9), month("2026-08", "2026-09-10", event=5e9)])
    lines = N.hood_revenue_lines("2026Q3", data)
    assert lines["rev_event_contracts"] == 0.0
    assert not any(math.isnan(v) for v in lines.values())


def test_net_interest_uses_margin_only_when_a_sweep_is_unprinted():
    pm = [month("2026-04", "2026-05-13", margin=10e9, sweep=0.0), month("2026-05", "2026-06-10", margin=10e9, sweep=0.0)]
    cm = [month("2026-07", "2026-08-12", margin=20e9, sweep=30e9), month("2026-08", "2026-09-10", margin=20e9, sweep=30e9)]
    assert N.hood_revenue_lines("2026Q3", _hood(prior_months=pm, cur_months=cm))["net_interest"] == 800e6


def test_arpu_uses_average_customers():
    assert math.isclose(N.arpu(1.0e9, 25e6, 27e6), 4e9 / 26e6)


def test_naive_growth_baseline():
    assert math.isclose(N.naive_growth(110, 100), 121)
    assert N.naive_growth(110, None) is None


# ---------- MARA ----------

def _blocks_in(q, n, reward_sats):
    start, _ = quarter_bounds(q)
    return [(1000 * int(q[:4]) + i, start + 600 * i, reward_sats) for i in range(n)]


def _mara(reported):
    blocks = _blocks_in("2025Q1", 100, 4e8) + _blocks_in("2025Q2", 120, 4e8) + _blocks_in("2025Q3", 150, 4e8)
    heights = {"2025Q1": 0, "2025Q2": 10000, "2025Q3": 22000, "2025Q4": 34000}
    vwap_days = {date(2025, 7, 1) + timedelta(days=i): (100_000.0, 1.0) for i in range(92)}
    return inputs(actuals=reported, blocks=blocks, heights=heights, coinbase={"BTC-USD": vwap_days})


def test_mara_ratio_uses_prior_quarters_only():
    rep = {("MARA", "2025Q1"): {"blocks_won": 110, "bitcoins_mined": 440, "revenue": 1.0},
           ("MARA", "2025Q2"): {"blocks_won": 120, "bitcoins_mined": 480, "revenue": 1.0},
           ("MARA", "2025Q3"): {"blocks_won": 999, "bitcoins_mined": 9999, "revenue": 1.0}}
    got = N.nowcasts("MARA", "2025Q3", _mara(rep))
    assert math.isclose(got["blocks_won"][0], 150 * (1.10 + 1.00) / 2)
    assert math.isclose(got["bitcoins_mined"][0], 600 * (440 / 400 + 480 / 480) / 2)


def test_mara_naive_uses_prior_share_times_network_blocks():
    rep = {("MARA", "2025Q2"): {"blocks_won": 120, "bitcoins_mined": 480, "revenue": 480 * 90_000.0 + 5e6}}
    data = _mara(rep)
    got = N.nowcasts("MARA", "2025Q3", data)
    naive_blocks = 120 / 12000 * 12000
    assert math.isclose(got["blocks_won"][1], naive_blocks)
    assert math.isclose(got["bitcoins_mined"][1], naive_blocks * 4.0)
    # revenue: other revenue = prior revenue - prior BTC x prior VWAP (no Q2 candles -> no VWAP -> no revenue)
    assert got["revenue"] == (None, None)


def test_mara_revenue_adds_carried_other_revenue():
    rep = {("MARA", "2025Q2"): {"blocks_won": 120, "bitcoins_mined": 480, "revenue": 480 * 90_000.0 + 5e6}}
    data = _mara(rep)
    data.coinbase["BTC-USD"].update({date(2025, 4, 1) + timedelta(days=i): (90_000.0, 2.0) for i in range(91)})
    model, naive = N.nowcasts("MARA", "2025Q3", data)["revenue"]
    btc_model = 600 * (480 / 480)
    assert math.isclose(model, btc_model * 100_000.0 + 5e6)
    assert math.isclose(naive, 120 * 4.0 * 100_000.0 + 5e6)


def test_vwap_weights_by_volume():
    days = {date(2025, 7, 1): (100.0, 1.0), date(2025, 7, 2): (200.0, 3.0), date(2025, 6, 30): (1e9, 1e9)}
    assert math.isclose(N.vwap(days, "2025Q3"), (100 + 600) / 4)


# ---------- COIN ----------

def test_coin_ratio_is_mean_of_last_four():
    qs = ["2024Q1", "2024Q2", "2024Q3", "2024Q4", "2025Q1", "2025Q2"]
    coinbase = {"BTC-USD": {}, "ETH-USD": {}}
    for i, q in enumerate(qs):
        start, _ = quarter_bounds(q)
        d = datetime.fromtimestamp(start, tz=timezone.utc).date()
        coinbase["BTC-USD"][d] = (10.0, 100.0)   # 1,000 USD
        coinbase["ETH-USD"][d + timedelta(days=1)] = (1.0, 1000.0 * (i + 1))  # grows each quarter
    vol = {q: 1000.0 + 1000.0 * (i + 1) for i, q in enumerate(qs)}
    ratios = [0.5, 0.1, 0.2, 0.3, 0.4]
    actuals = {("COIN", q): {"transaction_revenue": r * vol[q]} for q, r in zip(qs[:5], ratios)}
    model, naive = N.nowcasts("COIN", "2025Q2", inputs(actuals=actuals, coinbase=coinbase))["transaction_revenue"]
    assert math.isclose(model, vol["2025Q2"] * (0.1 + 0.2 + 0.3 + 0.4) / 4)
    r4, r3 = 0.4 * vol["2025Q1"], 0.3 * vol["2024Q4"]
    assert math.isclose(naive, r4 * r4 / r3)


# ---------- scoring (Task 5) ----------

def test_strike_interval_tiers():
    assert N.strike_interval(9.99) == 0.01
    assert N.strike_interval(10) == 0.1
    assert N.strike_interval(999) == 1
    assert N.strike_interval(1000) == 10
    assert N.strike_interval(12000) == 100


def test_t4_cdf_known_values():
    assert N.t4_cdf(0) == 0.5
    assert abs(N.t4_cdf(2.132) - 0.95) < 1e-3
    assert abs(N.t4_cdf(-2.132) - 0.05) < 1e-3
    assert abs(N.t4_cdf(N.T4_Q90) - 0.90) < 1e-6


def test_prob_is_half_at_bias_corrected_centre():
    errors = [0.01, 0.03, 0.02, 0.04]
    k = 100.0 * math.exp(0.025)
    assert abs(N.prob_at_or_above(k, 100.0, errors) - 0.5) < 1e-12
    assert N.prob_at_or_above(k * 1.2, 100.0, errors) < 0.5


def _rows(n, model_err=0.01, naive_err=0.10, wobble=0.005, seed=7):
    """Synthetic (quarter, actual, model, naive): model error model_err + wobble x N(0,1), naive naive_err x N(0,1)."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        actual = 1000.0 + 10 * i
        rows.append((f"Q{i}", actual, actual * math.exp(-(model_err + wobble * rng.gauss(0, 1))),
                     actual * math.exp(-naive_err * rng.gauss(0, 1))))
    return rows


def test_fewer_than_four_errors_not_scored():
    r4 = N.score_kpi(_rows(4), scale=1)
    assert r4["scored"] == 0 and r4["passes_gate"] is False and r4["brier"]["model"] is None
    r5 = N.score_kpi(_rows(5), scale=1)
    assert r5["scored"] == 1
    assert [q["scored"] for q in r5["quarters"]] == [False] * 4 + [True]


def test_quarter_needs_four_errors_from_both_methods():
    rows = _rows(8)
    rows[0] = (rows[0][0], rows[0][1], rows[0][2], None)  # naive missing in the first quarter
    out = N.score_kpi(rows, scale=1)
    assert [q["scored"] for q in out["quarters"]] == [False] * 5 + [True] * 3


def test_gate_passes_when_much_better_and_calibrated():
    out = N.score_kpi(_rows(30), scale=1)
    assert out["median_abs_err"]["model"] < 0.5 * out["median_abs_err"]["naive"]
    assert 0.60 <= out["coverage80"] <= 0.95
    assert out["passes_gate"] is True


def test_gate_fails_when_no_better_than_half_naive():
    out = N.score_kpi(_rows(30, model_err=0.0, naive_err=0.012, wobble=0.01), scale=1)
    assert out["median_abs_err"]["model"] > 0.5 * out["median_abs_err"]["naive"]
    assert out["passes_gate"] is False


def test_gate_fails_on_overconfident_intervals():
    rows = []
    for i in range(30):
        actual = 1000.0
        e = 0.001 * (i % 2) if i < 15 else 0.05 * (1 if i % 2 else -1)  # calm history, then big misses
        rows.append((f"Q{i}", actual, actual * math.exp(-e), actual * math.exp(-0.5)))
    out = N.score_kpi(rows, scale=1)
    assert out["coverage80"] < 0.60
    assert out["passes_gate"] is False


def test_errors_never_use_the_scored_quarter():
    rows = _rows(10)
    a = N.score_kpi(rows, scale=1)["quarters"][6]
    rows[6] = (rows[6][0], rows[6][1] * 1.5, rows[6][2], rows[6][3])
    b = N.score_kpi(rows, scale=1)["quarters"][6]
    assert a["p_model"] == b["p_model"] and a["p_naive"] == b["p_naive"]
    assert a["strikes"] == b["strikes"]


def test_strikes_centre_on_model_nowcast_in_scaled_units():
    rows = [(f"Q{i}", 2.5e9, 2.5e9 * math.exp(0.01 * ((-1) ** i)), 2.0e9) for i in range(5)]
    q = N.score_kpi(rows, scale=1e9)["quarters"][4]
    centre = round(q["model"] / 1e9 / 0.01) * 0.01
    assert [round(k, 2) for k in q["strikes"]] == [round(centre + d * 0.01, 2) for d in range(-3, 4)]


def test_run_lists_unprinted_quarters_as_dropped():
    actuals = {("MARA", "2024Q1"): {"blocks_won": 368, "bitcoins_mined": 2811, "revenue": 1.0},
               ("MARA", "2023Q3"): {"bitcoins_mined": 3490, "revenue": 1.0},
               ("COIN", "2021Q2"): {"transaction_revenue": 1.0},
               ("HOOD", "2026Q3"): {"funded_customers": 1.0, "revenue": 1.0, "arpu": 1.0}}
    dates = {("HOOD", "2026Q3"): date(2026, 10, 27)}
    monthly = [month("2026-07", "2026-08-12"), month("2026-08", "2026-09-10")]
    out = N.run(inputs(actuals=actuals, dates=dates, monthly=monthly))
    blocks = out["MARA.blocks_won"]
    assert "2023Q3" in blocks["dropped"] and "2024Q1" not in blocks["dropped"]
    assert [q["quarter"] for q in blocks["quarters"]] == ["2024Q1"]
    assert out["HOOD.funded_customers"]["sample"] == ["2026Q3", "2026Q3"]
