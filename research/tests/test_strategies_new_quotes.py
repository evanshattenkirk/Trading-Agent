"""Tests for research/strategies_new_quotes.py (candidates F1 and F2 replayed on real 1-minute SPY 0DTE quotes)."""
import math
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import strategies_new as sn  # noqa: E402
import strategies_new_quotes as nq  # noqa: E402
from test_bd_real_quotes import make_panel, m  # noqa: E402

VIX = 16.0


def test_entry_and_close_minutes_match_the_backtest_bars():
    assert nq.F1_ENTRY == m("13:30") and nq.F2_ENTRY == m("10:00") and nq.CLOSE_MIN == m("15:25")


def test_f1_legs_center_on_the_1330_spot_with_the_backtest_expected_move():
    S = 500.4
    em = 0.9 * sn.leg_sd(VIX, 0, 0, sn.K_1330) * S
    assert nq.legs_F1(S, VIX) == [("C", float(math.ceil(S + em)), -1), ("C", float(math.ceil(S + em) + 2), 1),
                                  ("P", float(math.floor(S - em)), -1), ("P", float(math.floor(S - em) - 2), 1)]


def test_f2_legs_are_the_put_side_at_1000():
    S = 500.4
    em = 0.9 * sn.leg_sd(VIX, 0, 0, sn.K_1000) * S
    assert nq.legs_F2(S, VIX) == [("P", float(math.floor(S - em)), -1), ("P", float(math.floor(S - em) - 2), 1)]


def test_day_rows_cover_both_candidates_and_every_fill_model():
    p = make_panel({m("09:30"): 500.0}, range(480, 521), iv_share=0.006)
    rows = nq.day_rows(p, date(2025, 3, 20), VIX, gate=False)
    got = {(r["book"], r["model"]) for r in rows}
    assert got == {(b, mo) for b in ("F1", "F2") for mo in ("mid", "patient", "taker")}
    assert all(r["credit"] >= 0.10 for r in rows)


def test_day_rows_skip_structures_below_the_minimum_credit():
    p = make_panel({m("09:30"): 500.0}, range(480, 521), iv_share=0.0005)
    assert nq.day_rows(p, date(2025, 3, 20), VIX, gate=True) == []


def test_f2_trend_rows_only_when_the_gate_is_open():
    p = make_panel({m("09:30"): 500.0}, range(480, 521), iv_share=0.006)
    on = {r["book"] for r in nq.day_rows(p, date(2025, 3, 20), VIX, gate=True)}
    off = {r["book"] for r in nq.day_rows(p, date(2025, 3, 20), VIX, gate=False)}
    assert "F2-trend" in on and "F2-trend" not in off


def test_gate_uses_closes_before_the_session_only():
    days = pd.date_range("2025-01-01", periods=60, freq="D").date
    closes = pd.Series(range(100, 160), index=days, dtype=float)
    d = days[55]
    a = nq.gate_for(closes, d)
    closes2 = closes.copy(); closes2[d] = -1e9
    assert nq.gate_for(closes2, d) == a and a is True
    assert nq.gate_for(closes, days[20]) is False                 # fewer than 50 prior closes


def test_daily_closes_take_the_last_regular_session_bar():
    bars = pd.DataFrame({"date": [date(2025, 3, 20)] * 3, "minute": [m("15:58"), m("15:59"), m("16:05")],
                         "o": 1.0, "h": 1.0, "l": 1.0, "c": [10.0, 11.0, 99.0], "v": 1.0})
    assert nq.daily_closes(bars)[date(2025, 3, 20)] == 11.0


def _write_day(folder: Path, d: str, spot: float):
    p = make_panel({m("09:30"): spot}, range(int(spot) - 20, int(spot) + 21), iv_share=0.006)
    rows = []
    for (r, k), (b, a) in p.q.items():
        for i, t in enumerate(p.minutes):
            rows.append({"timestamp": f"{d}T{t // 60:02d}:{t % 60:02d}:00.000", "strike": k,
                         "right": "CALL" if r == "C" else "PUT", "bid": b[i], "ask": a[i]})
    pd.DataFrame(rows).to_parquet(folder / f"{d}.parquet")


def test_run_end_to_end_writes_report_and_results(tmp_path):
    q = tmp_path / "quotes"; q.mkdir()
    _write_day(q, "2025-03-19", 500.0)
    _write_day(q, "2025-03-20", 505.0)
    vix = tmp_path / "vix.csv"
    pd.DataFrame({"DATE": ["2025-03-18", "2025-03-19"], "CLOSE": [VIX, VIX]}).to_csv(vix, index=False)
    res = nq.run(q, tmp_path / "out", None, vix)
    text = (tmp_path / "out" / "report.md").read_text()
    assert "## F1" in text and "## F2" in text
    assert res["F1|patient|all"]["n"] == 2
    assert (tmp_path / "out" / "trades.parquet").exists()


# ---------- F3 calendar on two chains (0DTE + next expiry) ----------

def _const_panel(strike, bid, ask, right="C"):
    rows = [{"minute": t, "right": right, "strike": float(strike), "bid": bid, "ask": ask}
            for t in range(m("09:30"), m("16:00") + 1)]
    return nq.bd.QuotePanel(pd.DataFrame(rows))


def test_calendar_debit_is_long_next_minus_short_same_day_plus_fees():
    p0 = make_panel({m("09:30"): 500.0}, range(490, 511), iv_share=0.006)
    p1 = _const_panel(500, 3.00, 3.04)
    r = nq.replay_calendar(p0, p1, 500.0, nq.F2_ENTRY, nq.CLOSE_MIN, "taker")
    b0, a0 = p0.ba("C", 500.0, nq.F2_ENTRY)
    assert r["debit"] == pytest.approx(3.04 - b0 + 2 * nq.bd.FEE)


def test_calendar_takes_profit_as_the_same_day_leg_decays():
    p0 = make_panel({m("09:30"): 500.0}, range(490, 511), iv_share=0.006)
    p1 = _const_panel(500, 3.00, 3.02)
    r = nq.replay_calendar(p0, p1, 500.0, nq.F2_ENTRY, nq.CLOSE_MIN, "patient")
    assert r["why"] == "take profit" and r["ret"] >= 0.25


def test_calendar_stops_when_spot_runs_away_from_the_strike():
    p0 = make_panel({m("09:30"): 500.0, m("10:01"): 510.0}, range(490, 521), iv_share=0.006)
    p1 = _const_panel(500, 3.00, 3.02)
    r = nq.replay_calendar(p0, p1, 500.0, nq.F2_ENTRY, nq.CLOSE_MIN, "patient")
    assert r["why"] == "stop" and r["ret"] <= -0.35


def test_run_includes_f3_when_next_expiry_quotes_are_given(tmp_path):
    q = tmp_path / "quotes"; q.mkdir(); nx = tmp_path / "next"; nx.mkdir()
    _write_day(q, "2025-03-19", 500.0)
    _write_day(nx, "2025-03-19", 500.0)
    vix = tmp_path / "vix.csv"
    pd.DataFrame({"DATE": ["2025-03-18"], "CLOSE": [VIX]}).to_csv(vix, index=False)
    res = nq.run(q, tmp_path / "out", None, vix, next_quotes=nx)
    assert res["F3|patient|all"]["n"] == 1
    assert "## F3" in (tmp_path / "out" / "report.md").read_text()
