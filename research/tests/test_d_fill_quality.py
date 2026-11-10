"""Tests for research/d_fill_quality.py (book D fill-quality study, spec 2026-11-09)."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import d_fill_quality as fq  # noqa: E402

DAY = "2024-06-12"


def T(hms: str) -> pd.Timestamp:
    return pd.Timestamp(f"{DAY} {hms}", tz="America/New_York")


def tape(rows):
    """rows: (hms, price, size, bid, ask, bid_size, ask_size)."""
    return pd.DataFrame([{"ts": T(r[0]), "price": r[1], "size": r[2], "bid": r[3], "ask": r[4],
                          "bid_size": r[5], "ask_size": r[6]} for r in rows], columns=fq.TAPE_COLS)


T0, DL = T("10:00:00"), T("10:10:00")


# ---------------------------------------------------------------- task 1: the walk and the fill rules

def test_start_price():
    assert fq.start_price(1.00, 1.01, "sell") == 1.01
    assert fq.start_price(1.00, 1.01, "buy") == 1.00
    assert fq.start_price(1.00, 1.02, "sell") == 1.01
    assert fq.start_price(1.00, 1.02, "buy") == 1.01
    assert fq.start_price(1.00, 1.03, "sell") == 1.02
    assert fq.start_price(1.00, 1.03, "buy") == 1.01


def test_queue_fill_after_size_clears():
    tp = tape([("09:59:59", 1.00, 1, 1.00, 1.01, 50, 10),
               ("10:00:10", 1.01, 6, 1.00, 1.01, 50, 10),
               ("10:00:20", 1.01, 5, 1.00, 1.01, 50, 10)])
    o = fq.simulate(tp, T0, "sell", 60, "queue", DL)
    assert (o.price, o.ts, o.reason, o.passive) == (1.01, T("10:00:20"), "queue", True)
    o2 = fq.simulate(tp, T0, "sell", 60, "queue2", DL)
    assert o2.reason != "queue" and o2.ts > T("10:00:20")


def test_trade_through_fills_at_limit():
    tp = tape([("09:59:59", 1.00, 1, 1.00, 1.01, 50, 500),
               ("10:00:05", 1.02, 1, 1.00, 1.01, 50, 500)])
    o = fq.simulate(tp, T0, "sell", 60, "queue", DL)
    assert (o.price, o.reason) == (1.01, "trade_through")


def test_cross_fills():
    tp = tape([("09:59:59", 1.00, 1, 1.00, 1.01, 50, 500),
               ("10:00:05", 1.02, 1, 1.01, 1.02, 50, 500)])
    o = fq.simulate(tp, T0, "sell", 60, "strict", DL)
    assert o.price == 1.01 and o.reason in ("cross", "trade_through") and o.passive
    tp2 = tape([("09:59:59", 1.00, 1, 1.00, 1.01, 50, 500),
                ("10:00:05", 1.01, 1, 1.01, 1.02, 50, 500)])
    o2 = fq.simulate(tp2, T0, "sell", 60, "strict", DL)
    assert (o2.price, o2.reason) == (1.01, "cross")


def test_walk_reaches_natural():
    tp = tape([("09:59:59", 1.00, 1, 1.00, 1.01, 50, 500),
               ("10:05:00", 1.00, 1, 1.00, 1.01, 50, 500)])
    o = fq.simulate(tp, T0, "sell", 60, "queue", DL)
    assert (o.price, o.ts, o.reason, o.passive) == (1.00, T("10:01:00"), "walked_to_natural", False)
    assert fq.cost_cents(o, "sell") == pytest.approx(0.5)


def test_optimistic_vs_strict():
    tp = tape([("09:59:59", 1.00, 1, 1.00, 1.01, 50, 500),
               ("10:00:05", 1.01, 1, 1.00, 1.01, 50, 500)])
    assert fq.simulate(tp, T0, "sell", 300, "optimistic", DL).ts == T("10:00:05")
    for rule in ("strict", "queue"):
        assert fq.simulate(tp, T0, "sell", 300, rule, DL).ts > T("10:00:05")


def test_deadline_forces_natural():
    rows = [("09:59:59", 1.00, 1, 1.00, 1.01, 50, 50)]
    for i in range(1, 61):                                 # ask rises 1c every 10 s for 10 minutes
        s = 1.00 + i * 0.01
        rows.append((str((T0 + pd.Timedelta(seconds=10 * i)).time()), s, 1, round(s, 2), round(s + 0.01, 2), 50, 50))
    o = fq.simulate(tape(rows), T0, "buy", 15, "queue", DL)
    assert o.reason == "deadline" and o.ts == DL and o.price == pytest.approx(1.61) and not o.passive


def test_bad_quotes_ignored_and_no_quote():
    tp = tape([("09:59:58", 1.00, 1, 1.00, 1.01, 50, 500),
               ("09:59:59", 1.00, 1, 1.02, 1.01, 50, 500),     # crossed: ignored
               ("10:00:05", 1.00, 1, 0.00, 1.01, 50, 500)])    # zero bid: ignored
    o = fq.simulate(tp, T0, "sell", 60, "strict", DL)
    assert o.mid0 == pytest.approx(1.005) and o.width_c == 1 and o.ts == T("10:01:00")
    bad = tape([("09:59:59", 1.00, 1, 1.01, 1.01, 50, 500), ("10:00:05", 1.00, 1, 1.00, 1.01, 50, 500)])
    o2 = fq.simulate(bad, T0, "sell", 60, "queue", DL)
    assert o2.price is None and o2.reason == "no_quote" and fq.cost_cents(o2, "sell") is None


def test_cost_signs():
    buy = fq.Order(1.00, T0, 1.005, 1, True, "queue")
    sell = fq.Order(1.00, T0, 1.005, 1, False, "walked_to_natural")
    assert fq.cost_cents(buy, "buy") == pytest.approx(-0.5)
    assert fq.cost_cents(sell, "sell") == pytest.approx(0.5)


# ---------------------------------------------------------------- task 2: fill models in the D replay

import bd_real_quotes as bd  # noqa: E402


def mm(hm: str) -> int:
    h, m_ = hm.split(":")
    return int(h) * 60 + int(m_)


def panel(price_by_minute: dict, half_spread: float = 0.01):
    px = pd.Series(price_by_minute).sort_index().reindex(range(mm("09:30"), mm("16:00") + 1)).ffill().bfill()
    rows = [{"minute": t, "right": "C", "strike": 500.0, "bid": round(v - half_spread, 4),
             "ask": round(v + half_spread, 4)} for t, v in px.items()]
    return bd.QuotePanel(pd.DataFrame(rows))


def test_measured_model_clamps():
    f = fq.measured_model({("entry", "sell", 1): -0.5})
    assert f(1.00, 1.01, "sell", True) == pytest.approx(1.01)
    f2 = fq.measured_model({("entry", "sell", 1): 2.0})
    assert f2(1.00, 1.01, "sell", True) == pytest.approx(1.00)
    f3 = fq.measured_model({("close", "buy", 1): -3.0})
    assert f3(1.00, 1.01, "buy", False) == pytest.approx(1.00)        # never better than the passive side


def test_measured_model_fallback():
    f = fq.measured_model({("entry", "buy", 3): 0.25})
    assert f(1.00, 1.02, "buy", True) == pytest.approx(1.0125)        # 2c key missing -> 3c key
    g = fq.measured_model({})
    assert g(1.00, 1.02, "buy", True) == pytest.approx(1.02)          # nothing -> taker
    assert g(1.00, 1.02, "sell", False) == pytest.approx(1.00)


def test_uniform_zero_is_mid():
    assert fq.uniform_model(0)(1.00, 1.02, "sell", True) == pytest.approx(1.01)
    assert fq.uniform_model(0.5)(1.00, 1.02, "buy", False) == pytest.approx(1.015)


def test_callable_model_matches_string_models():
    p = panel({mm("09:30"): 1.00, mm("11:00"): 0.45})
    legs = [("C", 500.0, -1)]
    a = bd.replay(p, legs, mm("10:00"), mm("15:25"), fq.uniform_model(0), width=2)
    b = bd.replay(p, legs, mm("10:00"), mm("15:25"), "mid", width=2)
    assert a == b
    taker = lambda bid, ask, side, opening: bid if side == "sell" else ask  # noqa: E731
    assert bd.replay(p, legs, mm("10:00"), mm("15:25"), taker, width=2) == \
        bd.replay(p, legs, mm("10:00"), mm("15:25"), "taker", width=2)


def test_replay_d_stop_pays_taker():
    p = panel({mm("09:30"): 0.50, mm("11:00"): 1.20}, half_spread=0.02)
    legs = [("C", 500.0, -1)]
    r = fq.replay_d(p, legs, fq.uniform_model(0))
    assert r["why"] == "stop" and r["exit_minute"] == mm("11:00")
    cr = 0.50 - bd.FEE
    assert r["credit"] == pytest.approx(cr)
    assert r["pnl"] == pytest.approx(cr - (1.22 + bd.FEE))
    assert r["ret"] == pytest.approx(r["pnl"] / (2 - cr))
