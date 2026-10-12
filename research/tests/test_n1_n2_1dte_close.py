"""Tests for research/n1_n2_1dte_close.py (rules in research/n1_n2_1dte_close_prereg.md)."""
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH.parent))

import bd_real_quotes as bd  # noqa: E402
import n1_n2_1dte_close as nn  # noqa: E402
from test_h4_overnight_fly import ENTRY, raw_chain  # noqa: E402


def panel(prices, **kw):
    return bd.QuotePanel(bd.normalize(raw_chain(prices, **kw)))


# Expiry day near 15:40: SPY at ~652, so the 650 call is $2 in the money and everything else is nearly worthless.
EXIT = {("C", 650.0): 2.05, ("P", 650.0): 0.05, ("C", 655.0): 0.02, ("P", 645.0): 0.02,
        ("C", 651.0): 1.10, ("P", 651.0): 0.10, ("C", 649.0): 3.00, ("P", 649.0): 0.02,
        ("C", 652.0): 0.30, ("P", 652.0): 0.30}


# ---------------------------------------------------------------- calendar rules

def test_nights_follow_h4_and_skip_half_day_exits():
    assert nn.night_reason(date(2025, 9, 17), date(2025, 9, 18)) is None
    assert nn.night_reason(date(2025, 9, 12), date(2025, 9, 15)) is None          # Friday -> Monday
    assert nn.night_reason(date(2025, 9, 18), date(2025, 9, 19)) == "ex_div_eve"
    assert nn.night_reason(date(2025, 9, 15), date(2025, 9, 17)) == "expiry_not_next_session"
    assert nn.night_reason(date(2025, 11, 28), date(2025, 12, 1)) == "half_day"
    assert nn.night_reason(date(2025, 7, 2), date(2025, 7, 3)) == "exit_half_day"   # 13:00 close on e


# ---------------------------------------------------------------- legs

def test_legs():
    assert nn.legs("N1", 650.2, 5) == [("C", 650.0, -1), ("P", 650.0, -1), ("C", 655.0, 1), ("P", 645.0, 1)]
    assert nn.legs("N2", 649.6, 5) == [("P", 650.0, -1), ("P", 645.0, 1)]
    assert nn.legs("N2", 650.2, 3) == [("P", 650.0, -1), ("P", 647.0, 1)]


# ---------------------------------------------------------------- one trade

def test_n1_trade_at_patient_fills():
    t = nn.trade("N1", panel(ENTRY), panel(EXIT), "patient")
    credit = 2 * 2.59 - 2 * 0.81                      # sell 650s at mid-1c, buy the wings at mid+1c
    # 15:40 on e: buy back 650C at 2.06 and 650P at 0.06 (mid+1c, capped at ask); sell the wings at bid 0.01
    debit = 2.06 + 0.06 - 0.01 - 0.01
    assert t["credit"] == pytest.approx(credit)
    assert t["debit"] == pytest.approx(debit)
    assert t["pnl"] == pytest.approx(100 * (credit - debit) - 0.32)
    assert t["max_risk"] == pytest.approx(100 * (5 - credit) + 0.32)
    assert t["exit_minute"] == nn.EXIT_MIN == 15 * 60 + 40


def test_n2_trade_at_taker_fills():
    t = nn.trade("N2", panel(ENTRY), panel(EXIT), "taker")
    credit = 2.59 - 0.81
    debit = 0.06 - 0.01
    assert t["credit"] == pytest.approx(credit)
    assert t["debit"] == pytest.approx(debit)
    assert t["pnl"] == pytest.approx(100 * (credit - debit) - 0.16)
    assert t["max_risk"] == pytest.approx(100 * (5 - credit) + 0.16)


def test_exit_falls_back_to_the_latest_full_minute_from_1530():
    drop = {("C", 655.0, m) for m in range(nn.EXIT_MIN - 3, 961)}      # wing quote stops at 15:36
    # QuotePanel forward-fills, so a quote that stops still reads; a wing never quoted after 15:30 drops the night
    t = nn.trade("N1", panel(ENTRY), panel(EXIT, drop=drop), "patient")
    assert t["exit_minute"] == nn.EXIT_MIN
    gone = {("C", 655.0, m) for m in range(570, 961)}
    assert nn.trade("N1", panel(ENTRY), panel(EXIT, drop=gone), "patient") == {"skip": "no_exit_quote"}


def test_exit_never_looks_after_1540():
    late = {("C", 655.0, m) for m in range(570, nn.EXIT_MIN + 1)}       # first wing quote at 15:41
    assert nn.trade("N1", panel(ENTRY), panel(EXIT, drop=late), "patient") == {"skip": "no_exit_quote"}


def test_exit_minute_parameter_for_the_1525_sensitivity():
    t = nn.trade("N1", panel(ENTRY), panel(EXIT), "patient", exit_min=15 * 60 + 25)
    assert t["exit_minute"] == 15 * 60 + 25


def test_wide_leg_skips_the_night():
    raw = raw_chain(ENTRY)
    wide = (raw.strike == 645.0) & (raw.right == "P")
    raw.loc[wide, "bid"], raw.loc[wide, "ask"] = 0.60, 1.00
    assert nn.trade("N2", bd.QuotePanel(bd.normalize(raw)), panel(EXIT), "patient") == {"skip": "wide_leg"}


def test_closing_debit_is_capped_at_the_width():
    crash = {("C", 650.0): 0.01, ("P", 650.0): 12.0, ("C", 655.0): 0.01, ("P", 645.0): 6.5}
    t = nn.trade("N2", panel(ENTRY), panel(crash), "taker")
    assert t["debit"] == pytest.approx(5.0)
    assert t["pnl"] == pytest.approx(-t["max_risk"])


def test_settlement_value_uses_intrinsic_and_no_exit_cost():
    # the exit chain's parity spot near the close is 652 (650C 2.05 - 650P 0.05 + 650)
    t = nn.settle_trade("N1", panel(ENTRY), panel(EXIT), "patient")
    credit = 2 * 2.59 - 2 * 0.81
    assert t["settle"] == pytest.approx(652.0)
    assert t["debit"] == pytest.approx(2.0)          # the short 650 call is $2 in the money, the rest expire
    assert t["pnl"] == pytest.approx(100 * (credit - 2.0) - 0.16)    # entry fees only


# ---------------------------------------------------------------- descriptive rows

def test_straddle_and_spy_move():
    s = nn.straddle(panel(ENTRY), panel(EXIT), "mid")
    assert s == pytest.approx((5.20 - 2.10 - 4 * bd.FEE) / 5.20)
    assert nn.spy_move(panel(ENTRY), panel(EXIT)) == pytest.approx(652.0 / 650.0 - 1)


# ---------------------------------------------------------------- bar

def test_pass_bar_uses_2_58():
    good = {"is": {"patient": {"mean": 1.0}}, "oos": {"patient": {"t": 2.6}, "taker": {"mean": 0.5}}}
    assert nn.passes(good)
    assert not nn.passes({**good, "oos": {"patient": {"t": 2.5}, "taker": {"mean": 0.5}}})
    assert not nn.passes({**good, "is": {"patient": {"mean": -1.0}}})
    assert not nn.passes({**good, "oos": {"patient": {"t": 2.6}, "taker": {"mean": -0.1}}})


def test_run_end_to_end(tmp_path):
    pytest.importorskip("pyarrow")
    nq, q, out = tmp_path / "spy_next", tmp_path / "spy_0dte", tmp_path / "out"
    nq.mkdir(), q.mkdir()
    for d, e in ((date(2022, 3, 1), date(2022, 3, 2)),          # traded (out-of-sample)
                 (date(2021, 3, 2), date(2021, 3, 3)),          # traded (in-sample)
                 (date(2022, 3, 17), date(2022, 3, 18))):       # ex-dividend eve: skipped
        raw = raw_chain(ENTRY)
        raw["expiration"] = e.isoformat()
        raw.to_parquet(nq / f"{d.isoformat()}.parquet")
        raw_chain(EXIT).to_parquet(q / f"{e.isoformat()}.parquet")
    res = nn.run(q, nq, out)
    assert res["skips"] == {"ex_div_eve": 1}
    for c in ("N1", "N2"):
        assert res[c]["main"]["oos"]["patient"]["n"] == 1 and res[c]["main"]["is"]["patient"]["n"] == 1
    assert res["N1"]["main"]["oos"]["patient"]["mean"] == pytest.approx(100 * (3.56 - 2.10) - 0.32)
    assert set(res["N1"]["sensitivities"]) == {"exit 15:25", "settlement value", "wings 3", "wings 10"}
    assert (out / "report.md").exists() and (out / "n1_n2_trades.csv").exists()
    assert "N1" in (out / "report.md").read_text()
