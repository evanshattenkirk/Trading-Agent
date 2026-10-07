"""Tests for research/h4_overnight_fly.py (H4 in research/book_h_candidates_prereg.md section 4)."""
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH.parent))

import bd_real_quotes as bd  # noqa: E402
import fetch_thetadata_spy_next as fn  # noqa: E402
import h4_overnight_fly as h4  # noqa: E402


def raw_chain(prices: dict, minutes=range(570, 961), half_spread=0.01, drop=()):
    """prices: {(right, strike): mid}; same quote every minute; drop: {(right, strike, minute)} rows left out."""
    rows = []
    for m in minutes:
        for (r, k), mid in prices.items():
            if (r, k, m) in drop:
                continue
            rows.append({"ms_of_day": m * 60_000, "strike": k, "right": r,
                         "bid": round(mid - half_spread, 2), "ask": round(mid + half_spread, 2)})
    return pd.DataFrame(rows)


def panel(prices, **kw):
    return bd.QuotePanel(bd.normalize(raw_chain(prices, **kw)))


ENTRY = {("C", 650.0): 2.60, ("P", 650.0): 2.60, ("C", 655.0): 0.80, ("P", 645.0): 0.80,
         ("C", 651.0): 2.10, ("P", 651.0): 3.10, ("C", 649.0): 3.10, ("P", 649.0): 2.10}
EXIT = {("C", 650.0): 2.00, ("P", 650.0): 2.20, ("C", 655.0): 0.50, ("P", 645.0): 0.55,
        ("C", 651.0): 1.50, ("P", 651.0): 2.70, ("C", 649.0): 2.50, ("P", 649.0): 1.70}


# ---------------------------------------------------------------- calendar rules

def test_ex_dividend_dates_are_quarterly_third_fridays():
    assert h4.ex_div_date(date(2025, 9, 19))
    assert h4.ex_div_date(date(2026, 3, 20))
    assert not h4.ex_div_date(date(2025, 9, 12))
    assert not h4.ex_div_date(date(2025, 10, 17))


def test_night_rules():
    assert h4.night_reason(date(2025, 9, 17), date(2025, 9, 18)) is None
    assert h4.night_reason(date(2025, 9, 12), date(2025, 9, 15)) is None          # Friday -> Monday
    assert h4.night_reason(date(2025, 9, 18), date(2025, 9, 19)) == "ex_div_eve"
    assert h4.night_reason(date(2025, 9, 15), date(2025, 9, 17)) == "expiry_not_next_session"
    assert h4.night_reason(date(2025, 11, 28), date(2025, 12, 1)) == "half_day"
    assert h4.night_reason(date(2025, 7, 2), date(2025, 7, 3)) is None              # exit on a half day is fine


def test_next_expiry_after():
    exps = {date(2025, 9, 15), date(2025, 9, 17), date(2025, 9, 19)}
    assert fn.next_expiry_after(date(2025, 9, 15), exps) == date(2025, 9, 17)
    assert fn.next_expiry_after(date(2025, 9, 12), exps) == date(2025, 9, 15)
    assert fn.next_expiry_after(date(2025, 9, 19), exps) is None


# ---------------------------------------------------------------- one trade

def test_iron_fly_legs():
    assert h4.legs(650.2, 5) == [("C", 650.0, -1), ("P", 650.0, -1), ("C", 655.0, 1), ("P", 645.0, 1)]


def test_trade_pnl_at_patient_fills():
    t = h4.trade(panel(ENTRY), panel(EXIT), "patient", wing=5)
    # entry: sell 650C/650P at mid-1c = 2.59 each (bid 2.59), buy wings at 0.81 each
    credit = 2 * 2.59 - 2 * 0.81
    # exit: buy back 650C at 2.01, 650P at 2.21; sell wings at 0.49 and 0.54
    debit = 2.01 + 2.21 - 0.49 - 0.54
    assert t["credit"] == pytest.approx(credit)
    assert t["debit"] == pytest.approx(debit)
    assert t["pnl"] == pytest.approx(100 * (credit - debit) - 0.32)
    assert t["max_risk"] == pytest.approx(100 * (5 - credit) + 0.32)
    assert t["ret"] == pytest.approx(t["pnl"] / t["max_risk"])
    assert t["strike"] == 650.0 and t["exit_minute"] == h4.EXIT_MIN


def test_taker_is_worse_than_patient_and_mid():
    p, e = panel(ENTRY, half_spread=0.02), panel(EXIT, half_spread=0.02)
    pn = {m: h4.trade(p, e, m, wing=5)["pnl"] for m in bd.MODELS}
    assert pn["mid"] > pn["patient"] > pn["taker"]


def test_wide_leg_skips_the_night():
    prices = dict(ENTRY)
    raw = raw_chain(prices)
    wide = (raw.strike == 655.0) & (raw.right == "C")
    raw.loc[wide, "bid"], raw.loc[wide, "ask"] = 0.60, 1.00          # 40c wide on a 0.80 mid
    t = h4.trade(bd.QuotePanel(bd.normalize(raw)), panel(EXIT), "patient", wing=5)
    assert t == {"skip": "wide_leg"}


def test_exit_falls_forward_to_the_first_full_minute_until_10():
    drop = {("C", 655.0, m) for m in range(570, 590)}                 # wing unquoted until 09:50
    t = h4.trade(panel(ENTRY), panel(EXIT, drop=drop), "patient", wing=5)
    assert t["exit_minute"] == 590
    drop_all = {("C", 655.0, m) for m in range(570, 961)}
    assert h4.trade(panel(ENTRY), panel(EXIT, drop=drop_all), "patient", wing=5) == {"skip": "no_exit_quote"}


def test_closing_debit_is_capped_at_the_width():
    crash = {("C", 650.0): 0.01, ("P", 650.0): 12.0, ("C", 655.0): 0.01, ("P", 645.0): 6.5}
    t = h4.trade(panel(ENTRY), panel(crash), "taker", wing=5)
    assert t["debit"] == pytest.approx(5.0)
    assert t["pnl"] == pytest.approx(-t["max_risk"])


def test_short_straddle_return_is_on_premium():
    s = h4.straddle(panel(ENTRY), panel(EXIT), "mid")
    assert s == pytest.approx((5.20 - 4.20 - 4 * bd.FEE) / 5.20)


# ---------------------------------------------------------------- stats and bar

def test_pass_bar():
    good = {"is": {"patient": {"mean": 1.0}}, "oos": {"patient": {"t": 2.5}, "taker": {"mean": 0.5}}}
    assert h4.passes(good)
    assert not h4.passes({**good, "oos": {"patient": {"t": 2.0}, "taker": {"mean": 0.5}}})
    assert not h4.passes({**good, "is": {"patient": {"mean": -1.0}}})
    assert not h4.passes({**good, "oos": {"patient": {"t": 2.5}, "taker": {"mean": -0.1}}})


def test_stats_of_trades():
    s = h4.stats(pd.Series([10.0, -5.0, 20.0, 5.0]), pd.Series([0.1, -0.05, 0.2, 0.05]))
    assert s["n"] == 4 and s["mean"] == pytest.approx(7.5)
    assert s["pf"] == pytest.approx(7.0)
    assert s["worst"] == pytest.approx(-5.0)
    assert s["ret_pct"] == pytest.approx(7.5)


def test_run_end_to_end(tmp_path):
    pytest.importorskip("pyarrow")                    # the run writes parquet
    nq, q, out = tmp_path / "spy_next", tmp_path / "spy_0dte", tmp_path / "out"
    nq.mkdir(), q.mkdir()
    for d, e in ((date(2022, 3, 1), date(2022, 3, 2)),          # traded (out-of-sample)
                 (date(2021, 3, 2), date(2021, 3, 3)),          # traded (in-sample)
                 (date(2022, 3, 17), date(2022, 3, 18))):       # ex-dividend eve: skipped
        raw = raw_chain(ENTRY)
        raw["expiration"] = e.isoformat()
        raw.to_parquet(nq / f"{d.isoformat()}.parquet")
        raw_chain(EXIT).to_parquet(q / f"{e.isoformat()}.parquet")
    res = h4.run(q, nq, out)
    assert res["skips"] == {"ex_div_eve": 1}
    assert res["H4"]["oos"]["patient"]["n"] == 1 and res["H4"]["is"]["patient"]["n"] == 1
    assert res["H4"]["oos"]["patient"]["mean"] == pytest.approx(100 * (3.56 - 3.19) - 0.32)
    assert (out / "report.md").exists() and (out / "h4_trades.csv").exists()
