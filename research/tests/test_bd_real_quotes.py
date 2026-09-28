"""Tests for research/bd_real_quotes.py (books B and D replayed on real 1-minute SPY 0DTE quotes)."""
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bd_real_quotes as bd  # noqa: E402


def m(hhmm: str) -> int:
    h, mi = hhmm.split(":")
    return int(h) * 60 + int(mi)


def make_panel(spot_path: dict, strikes, iv_share=0.004, width=0.02, minutes=None):
    """Synthetic quote panel: intrinsic + a time value that decays linearly to 16:00, symmetric spread."""
    minutes = minutes or list(range(m("09:30"), m("16:00") + 1))
    rows = []
    S = pd.Series(spot_path).reindex(minutes).ffill().bfill()
    for t in minutes:
        s = S[t]
        left = max(0.0, (m("16:00") - t) / 390)
        for k in strikes:
            tv = s * iv_share * math.sqrt(left) * math.exp(-((s - k) / (s * 0.004 + 1e-9)) ** 2 / 2) if left > 0 else 0.0
            for right, intrinsic in (("C", max(s - k, 0.0)), ("P", max(k - s, 0.0))):
                mid = intrinsic + tv
                rows.append({"minute": t, "right": right, "strike": float(k),
                             "bid": max(0.0, round(mid - width / 2, 4)), "ask": round(mid + width / 2, 4)})
    return bd.QuotePanel(pd.DataFrame(rows))


# ---------- loading ----------

def test_normalize_thetadata_v3_columns():
    raw = pd.DataFrame({
        "timestamp": ["2025-03-21T09:45:00.000", "2025-03-21T09:45:00.000"],
        "strike": [570.0, 570.0], "right": ["CALL", "PUT"],
        "bid": [1.10, 0.95], "ask": [1.12, 0.97], "expiration": ["2025-03-21", "2025-03-21"],
    })
    df = bd.normalize(raw)
    assert list(df.columns) == ["minute", "right", "strike", "bid", "ask"]
    assert df.minute.tolist() == [m("09:45")] * 2
    assert df.right.tolist() == ["C", "P"]


def test_normalize_thousandths_strikes_and_ms_of_day():
    raw = pd.DataFrame({"ms_of_day": [34_200_000], "strike": [570000], "right": ["C"], "bid": [1.0], "ask": [1.1]})
    df = bd.normalize(raw)
    assert df.strike.iloc[0] == 570.0 and df.minute.iloc[0] == m("09:30")


def test_normalize_tz_aware_utc_converts_to_eastern():
    raw = pd.DataFrame({"timestamp": pd.to_datetime(["2025-03-21 13:45:00"]).tz_localize("UTC"),
                        "strike": [570.0], "right": ["P"], "bid": [1.0], "ask": [1.1]})
    assert bd.normalize(raw).minute.iloc[0] == m("09:45")


# ---------- spot and fills ----------

def test_spot_from_put_call_parity():
    p = make_panel({m("09:30"): 500.3}, range(490, 511))
    assert bd.spot(p, m("09:45")) == pytest.approx(500.3, abs=0.01)


@pytest.mark.parametrize("model,side,expected", [
    ("mid", "sell", 1.05), ("mid", "buy", 1.05),
    ("patient", "sell", 1.04), ("patient", "buy", 1.06),
    ("taker", "sell", 1.00), ("taker", "buy", 1.10),
])
def test_fill_models_wide_spread(model, side, expected):
    assert bd.fill(1.00, 1.10, side, model) == pytest.approx(expected)


def test_patient_fill_never_worse_than_natural_on_penny_market():
    assert bd.fill(0.13, 0.14, "sell", "patient") == pytest.approx(0.13)
    assert bd.fill(0.13, 0.14, "buy", "patient") == pytest.approx(0.14)


# ---------- structures ----------

def test_book_b_legs_round_to_atm_with_5_wings():
    legs = bd.legs_B(500.4)
    assert sorted(legs) == sorted([("C", 500.0, -1), ("P", 500.0, -1), ("C", 505.0, 1), ("P", 495.0, 1)])


def test_book_d_strikes_follow_backtest_expected_move():
    S, vix = 600.0, 16.0
    em = 0.9 * 0.80 * vix / 100 / math.sqrt(252) * math.sqrt((78 - 1 - 5) / 78 + 15 / 390) * S
    legs = bd.legs_D(S, vix)
    kc, kp = math.ceil(S + em), math.floor(S - em)
    assert sorted(legs) == sorted([("C", kc, -1), ("P", kp, -1), ("C", kc + 2, 1), ("P", kp - 2, 1)])


# ---------- replay ----------

def flat_legs_panel(price_by_minute: dict):
    """One short leg whose mid follows price_by_minute; spread 2c. Lets the tests steer the debit exactly."""
    px = pd.Series(price_by_minute).sort_index().reindex(range(m("09:30"), m("16:00") + 1)).ffill().bfill()
    rows = [{"minute": t, "right": "C", "strike": 500.0, "bid": v - 0.01, "ask": v + 0.01} for t, v in px.items()]
    return bd.QuotePanel(pd.DataFrame(rows))


def test_replay_take_profit_at_half_credit():
    p = flat_legs_panel({m("09:30"): 2.00, m("11:00"): 0.90})
    r = bd.replay(p, [("C", 500.0, -1)], m("09:45"), m("15:30"), "mid", width=5, stop_mult=2.0)
    cr = 2.00 - bd.FEE
    assert r["why"] == "take 50%" and r["exit_minute"] == m("11:00")
    assert r["pnl"] == pytest.approx(cr - (0.90 + bd.FEE))


def test_replay_stop_when_debit_reaches_2x_credit():
    p = flat_legs_panel({m("09:30"): 1.00, m("10:30"): 2.10})
    r = bd.replay(p, [("C", 500.0, -1)], m("09:45"), m("15:30"), "mid", width=5, stop_mult=2.0)
    assert r["why"] == "stop" and r["exit_minute"] == m("10:30")
    assert r["pnl"] == pytest.approx((1.00 - bd.FEE) - (2.10 + bd.FEE))


def test_replay_time_exit_at_close_minute():
    p = flat_legs_panel({m("09:30"): 1.00, m("12:00"): 0.80})
    r = bd.replay(p, [("C", 500.0, -1)], m("09:45"), m("15:30"), "taker", width=5, stop_mult=2.0)
    assert r["why"] == "time" and r["exit_minute"] == m("15:30")
    assert r["credit"] == pytest.approx(0.99 - bd.FEE)
    assert r["pnl"] == pytest.approx((0.99 - bd.FEE) - (0.81 + bd.FEE))
    assert r["risk"] == pytest.approx(5 - r["credit"])


def test_replay_skips_when_a_leg_has_no_quote():
    p = flat_legs_panel({m("09:30"): 1.00})
    assert bd.replay(p, [("C", 501.0, -1)], m("09:45"), m("15:30"), "mid", width=5, stop_mult=2.0) is None


def test_full_iron_fly_replay_on_quiet_day_makes_money_at_mid():
    p = make_panel({m("09:30"): 500.0}, range(490, 511))
    r = bd.replay(p, bd.legs_B(bd.spot(p, m("09:45"))), m("09:45"), m("15:30"), "mid", width=5, stop_mult=2.0)
    assert r is not None and r["pnl"] > 0 and r["why"] in ("take 50%", "time")


# ---------- settlement (the HANDOFF go/no-go number) ----------

def test_straddle_vs_realized_move():
    p = make_panel({m("09:30"): 500.0, m("15:00"): 503.0}, range(490, 511))
    g = bd.straddle_vs_move(p, m("09:45"), "mid")
    assert g["strike"] == 500.0
    assert g["move"] == pytest.approx(3.0, abs=0.02)
    assert g["short_pnl"] == pytest.approx(g["straddle"] - g["move"] - 2 * bd.FEE)


def test_condor_credit_vs_settlement_payoff():
    p = make_panel({m("09:30"): 500.0, m("15:00"): 506.0}, range(485, 516))
    legs = [("C", 504.0, -1), ("P", 496.0, -1), ("C", 506.0, 1), ("P", 494.0, 1)]
    g = bd.structure_vs_settlement(p, legs, m("10:00"), "mid")
    assert g["payoff"] == pytest.approx(2.0, abs=0.02)            # settles at 506: full $2 call-wing loss
    assert g["short_pnl"] == pytest.approx(g["credit"] - g["payoff"] - 4 * bd.FEE)


# ---------- quiet filter ----------

def spy_bars(day_ranges):
    """Minute bars for several days: each day trades flat at 500 except a range spike in the chosen window."""
    rows = []
    for d, (lo, hi, spike_min) in day_ranges.items():
        for t in range(m("09:30"), m("16:00")):
            h, l = (hi, lo) if t == spike_min else (500.05, 499.95)
            rows.append({"date": pd.Timestamp(d).date(), "minute": t, "o": 500.0, "h": h, "l": l, "c": 500.0, "v": 1000.0})
    return pd.DataFrame(rows)


def test_quiet_filter_lookahead_variant_sees_10_00_to_10_30():
    days = pd.bdate_range("2025-01-01", periods=16)
    ranges = {d: (499.0, 501.0, m("09:40")) for d in days[:-1]}           # prior days: 2.0 wide in the first 30 min
    ranges[days[-1]] = (498.0, 502.0, m("10:15"))                          # today: calm to 10:00, 4.0 wide at 10:15
    bars = spy_bars(ranges)
    today = days[-1].date()
    assert bd.quiet_ok(bars, today, 500.0, window_end=m("10:00")) is True    # no lookahead: today looks calm
    assert bd.quiet_ok(bars, today, 500.0, window_end=m("10:30")) is False   # as backtested: sees the spike


def test_quiet_filter_needs_price_near_vwap():
    days = pd.bdate_range("2025-01-01", periods=16)
    bars = spy_bars({d: (499.0, 501.0, m("09:40")) for d in days})
    today = days[-1].date()
    bars.loc[(bars.date == today) & (bars.minute == m("09:40")), ["h", "l"]] = [500.05, 499.95]  # today calmer than median
    assert bd.quiet_ok(bars, today, 500.0, window_end=m("10:00")) is True
    assert bd.quiet_ok(bars, today, 501.0, window_end=m("10:00")) is False   # 0.2% from VWAP > 0.12%


def test_summary_stats():
    s = bd.summ(pd.Series([0.1, -0.05, 0.2, -0.1]))
    assert s["n"] == 4 and s["win"] == pytest.approx(50.0) and s["pf"] == pytest.approx(0.3 / 0.15)


def test_run_end_to_end_on_thetadata_shaped_files(tmp_path):
    qdir = tmp_path / "spy_0dte"; qdir.mkdir()
    for d, drift in (("2025-03-19", 0.0), ("2025-03-20", 4.0), ("2025-03-21", -1.0)):
        p = make_panel({m("09:30"): 570.0, m("13:00"): 570.0 + drift}, range(555, 586))
        rows = []
        for (r, k), (b, a) in p.q.items():
            for i, t in enumerate(p.minutes):
                rows.append({"timestamp": f"{d}T{t // 60:02d}:{t % 60:02d}:00.000", "strike": k,
                             "right": "CALL" if r == "C" else "PUT", "bid": b[i], "ask": a[i]})
        pd.DataFrame(rows).to_parquet(qdir / f"{d}.parquet")
    vix = tmp_path / "vix.csv"
    vix.write_text("DATE,OPEN,HIGH,LOW,CLOSE\n2025-03-18,20,20,20,20\n2025-03-19,20,20,20,20\n2025-03-20,20,20,20,20\n")
    res = bd.run(qdir, tmp_path / "out", None, vix)
    assert (tmp_path / "out" / "report.md").exists()
    assert res["B|mid|all"]["n"] == 3
    assert "straddle_over_realized_mid" in res


def test_closing_debit_is_capped_at_the_structure_width():
    # a quote blowout can price the close above the width; you'd never pay more than max loss at expiry
    p = flat_legs_panel({m("09:30"): 1.00, m("10:30"): 14.60})
    r = bd.replay(p, [("C", 500.0, -1)], m("09:45"), m("15:30"), "taker", width=5, stop_mult=2.0)
    assert r["why"] == "stop"
    assert r["pnl"] == pytest.approx((0.99 - bd.FEE) - (5.0 + bd.FEE))
    assert r["pnl"] >= -r["risk"] - 2 * bd.FEE


def test_last_minute_ignores_stale_rows_after_a_half_day_close():
    rows = []
    for t in range(m("09:30"), m("16:00") + 1):
        px = 1.00 + 0.01 * (t % 7) if t <= m("13:00") else 1.05       # quotes freeze after 13:00
        rows.append({"minute": t, "right": "C", "strike": 500.0, "bid": px - 0.01, "ask": px + 0.01})
    p = bd.QuotePanel(pd.DataFrame(rows))
    assert p.last_minute <= m("13:01")


def test_replay_without_tp_or_stop_holds_to_the_close():
    # value collapses (TP would fire), then blows out (stop would fire), then settles back by the close
    p = flat_legs_panel({m("09:30"): 1.00, m("11:00"): 0.30, m("12:00"): 3.00, m("15:25"): 0.80})
    r = bd.replay(p, [("C", 500.0, -1)], m("10:00"), m("15:25"), "mid", width=2, tp=None, stop_mult=None)
    assert r["why"] == "time" and r["exit_minute"] == m("15:25")
    assert r["pnl"] == pytest.approx((1.00 - bd.FEE) - (0.80 + bd.FEE))


def test_run_reports_the_d_variant_without_tp_or_stop(tmp_path):
    qdir = tmp_path / "spy_0dte"; qdir.mkdir()
    for d, drift in (("2025-03-19", 0.0), ("2025-03-20", 4.0)):
        p = make_panel({m("09:30"): 570.0, m("13:00"): 570.0 + drift}, range(555, 586))
        rows = []
        for (r, k), (b, a) in p.q.items():
            for i, t in enumerate(p.minutes):
                rows.append({"timestamp": f"{d}T{t // 60:02d}:{t % 60:02d}:00.000", "strike": k,
                             "right": "CALL" if r == "C" else "PUT", "bid": b[i], "ask": a[i]})
        pd.DataFrame(rows).to_parquet(qdir / f"{d}.parquet")
    vix = tmp_path / "vix.csv"
    vix.write_text("DATE,OPEN,HIGH,LOW,CLOSE\n2025-03-18,20,20,20,20\n2025-03-19,20,20,20,20\n")
    res = bd.run(qdir, tmp_path / "out", None, vix)
    key = f"{bd.D_HOLD}|mid|all"
    assert res[key]["n"] == 2
    assert res[key]["exits"] == {"time": 2}
