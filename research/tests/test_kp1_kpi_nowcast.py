"""KP1 nowcasts, naive baselines and walk-forward scoring (rules: research/kp1_kpi_prereg.md)."""
import math
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
