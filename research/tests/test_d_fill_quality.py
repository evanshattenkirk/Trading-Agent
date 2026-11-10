"""Tests for research/d_fill_quality.py (book D fill-quality study, spec 2026-11-09)."""
from __future__ import annotations

import math
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
               ("10:00:05", 1.00, 1, -0.01, 1.01, 50, 500)])   # negative bid: ignored
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


# ---------------------------------------------------------------- task 3: pooling and the verdict

import numpy as np  # noqa: E402


def test_width_bucket():
    assert [fq.width_bucket(w) for w in (1, 2, 3, 7)] == [1, 2, 3, 3]


def test_pool_costs_means_by_key():
    ev = pd.DataFrame([
        {"window": "entry", "side": "sell", "width_c": 1, "rule": "queue", "hold_s": 60, "cost_c": 0.5},
        {"window": "entry", "side": "sell", "width_c": 1, "rule": "queue", "hold_s": 60, "cost_c": -0.5},
        {"window": "entry", "side": "sell", "width_c": 1, "rule": "queue", "hold_s": 60, "cost_c": 0.5},
        {"window": "entry", "side": "sell", "width_c": 4, "rule": "queue", "hold_s": 60, "cost_c": 1.0},
        {"window": "entry", "side": "sell", "width_c": 5, "rule": "queue", "hold_s": 60, "cost_c": float("nan")},
        {"window": "entry", "side": "sell", "width_c": 1, "rule": "strict", "hold_s": 60, "cost_c": 9.0},
        {"window": "close", "side": "buy", "width_c": 2, "rule": "queue", "hold_s": 15, "cost_c": 9.0},
    ])
    c = fq.pool_costs(ev, "queue", 60)
    assert c == {("entry", "sell", 1): pytest.approx(1 / 6), ("entry", "sell", 3): pytest.approx(1.0)}


def trades(mean_pct, n=400, sd_pct=2.0, years=(2016, 2026), seed=1):
    rng = np.random.default_rng(seed)
    x = rng.normal(0, sd_pct / 100, n)
    x = x - x.mean() + mean_pct / 100
    yrs = np.linspace(years[0], years[1], n).round().astype(int)
    return pd.DataFrame({"year": yrs, "ret": x})


def test_verdict_pass():
    v = fq.verdict(trades(1.0), trades(0.5, seed=2))
    assert v["pass"] is True and v["checks"] == [True, True, True] and v["t"] > 2.33
    assert v["avg"] == pytest.approx(1.0)


def test_verdict_fails_one_half():
    p = trades(1.0)
    p.loc[p.year <= 2021, "ret"] -= 0.03
    v = fq.verdict(p, trades(0.5))
    assert v["pass"] is False and v["checks"][1] is False and v["avg_2016_2021"] < 0


def test_verdict_fails_queue2():
    v = fq.verdict(trades(1.0), trades(-0.2))
    assert v["pass"] is False and v["checks"] == [True, True, False] and v["avg_queue2"] < 0


# ---------------------------------------------------------------- task 4: tick normalisation and D's legs

import datetime as dt  # noqa: E402


def test_normalize_ticks_columns_and_tz():
    raw = pd.DataFrame({
        "symbol": ["SPY", "SPY"], "expiration": ["2026-11-06"] * 2, "strike": [757.0, 757.0], "right": ["CALL"] * 2,
        "trade_timestamp": pd.to_datetime(["2026-11-06 09:45:07.100", "2026-11-06 09:45:04.897"]).tz_localize("America/New_York"),
        "quote_timestamp": pd.to_datetime(["2026-11-06 09:45:07.09", "2026-11-06 09:45:04.86"]).tz_localize("America/New_York"),
        "condition": [18, 18], "size": [1, 5], "exchange": [73, 43], "price": [0.15, 0.14],
        "bid_size": [2045, 1625], "bid": [0.14, 0.14], "ask_size": [585, 607], "ask": [0.15, 0.15]})
    t = fq.normalize_ticks(raw.assign(trade_timestamp=raw.trade_timestamp.dt.tz_convert("UTC")))
    assert list(t.columns) == fq.TAPE_COLS
    assert str(t.ts.dt.tz) == "America/New_York" and t.ts.is_monotonic_increasing
    assert t.iloc[0].price == 0.14 and t.iloc[0].ts.hour == 9


def test_d_legs_for_day():
    rows = []
    for t in range(mm("09:30"), mm("16:00") + 1):
        for k in range(590, 611):
            c, p_ = max(600.2 - k, 0) + 0.5, max(k - 600.2, 0) + 0.5
            rows += [{"minute": t, "right": "C", "strike": float(k), "bid": c - 0.01, "ask": c + 0.01},
                     {"minute": t, "right": "P", "strike": float(k), "bid": p_ - 0.01, "ask": p_ + 0.01}]
    p = bd.QuotePanel(pd.DataFrame(rows))
    vix = pd.Series([16.0], index=[dt.date(2024, 6, 11)])
    legs = fq.d_legs_for_day(p, dt.date(2024, 6, 12), vix)
    assert legs == bd.legs_D(bd.spot(p, bd.D_ENTRY), 16.0)
    assert sorted(q for _, _, q in legs) == [-1, -1, 1, 1]
    assert fq.d_legs_for_day(p, dt.date(2024, 6, 11), vix) is None          # no prior VIX close


# ---------------------------------------------------------------- task 5: measure and score end to end

import json  # noqa: E402


def write_session(qdir: Path, tdir: Path, day: dt.date, vix: pd.Series):
    """A 1-minute quote file shaped like ThetaData's (ms_of_day), plus a tick tape for each D leg and window."""
    rows = []
    for t in range(mm("09:30"), mm("16:00") + 1):
        left = (mm("16:00") - t) / 390
        for k in range(580, 621):
            tv = 3.0 * left * math.exp(-((600.2 - k) / 6) ** 2 / 2) + 0.03
            c, p_ = max(600.2 - k, 0) + tv, max(k - 600.2, 0) + tv
            for r, v in (("C", c), ("P", p_)):
                rows.append({"ms_of_day": t * 60_000, "strike": float(k), "right": r,
                             "bid": round(v - 0.005, 3), "ask": round(v + 0.005, 3)})
    raw = pd.DataFrame(rows)
    raw.to_parquet(qdir / f"{day.isoformat()}.parquet")
    p = bd.QuotePanel(bd.normalize(raw))
    legs = fq.d_legs_for_day(p, day, vix)
    for r, k, q in legs:
        for w, (a, b) in fq.WINDOWS.items():
            m0 = mm(a[:5]) + 1
            ba = p.ba(r, k, m0)
            bid, ask = round(ba[0], 2), round(ba[0], 2) + 0.01
            base = pd.Timestamp(f"{day} {a}", tz=fq.ET)
            tp = pd.DataFrame([{"ts": base + pd.Timedelta(seconds=s), "price": px, "size": 5.0, "bid": bid,
                                "ask": ask, "bid_size": 3.0, "ask_size": 3.0}
                               for s, px in ((30, bid), (80, ask), (95, bid), (200, ask))], columns=fq.TAPE_COLS)
            tp.to_parquet(fq.tick_path(tdir, day, r, k, w))
    return legs


def test_measure_and_score_end_to_end(tmp_path):
    qdir, tdir, fdir, rdir = (tmp_path / x for x in ("q", "t", "f", "r"))
    for d_ in (qdir, tdir, fdir, rdir):
        d_.mkdir()
    pd.DataFrame({"DATE": ["06/10/2024", "06/11/2024", "06/12/2024"], "CLOSE": [15.0, 16.0, 14.0]}).to_csv(
        tmp_path / "vix.csv", index=False)
    vix = bd.load_vix(tmp_path / "vix.csv")
    for day in (dt.date(2024, 6, 11), dt.date(2024, 6, 12)):
        write_session(qdir, tdir, day, vix)
    ev = fq.measure(tdir, fdir)
    assert (fdir / "events.parquet").exists()
    assert len(ev) == 2 * 4 * 2 * len(fq.RULES) * len(fq.HOLDS)
    assert set(ev.side[(ev.window == "entry")]) == {"buy", "sell"}
    assert ev.cost_c.notna().all()
    res = fq.score(qdir, tmp_path / "vix.csv", fdir / "events.parquet", rdir)
    js = json.loads((rdir / "d_fill_quality_results.json").read_text())
    assert (rdir / "d_fill_quality.md").exists()
    assert "pass" in js["verdict"] and len(js["uniform"]) == 7
    assert {"mid", "patient", "taker"} <= set(js["reference"])
    assert res["verdict"]["pass"] == js["verdict"]["pass"]


def test_empty_tape_crosses_at_the_one_minute_natural(tmp_path):
    """A leg with no trades in its window can't fill passively: with --quotes it walks to the natural of the
    1-minute quote at the decision minute (cost = +half the spread), and without quotes it is dropped."""
    qdir, tdir = tmp_path / "q", tmp_path / "t"
    qdir.mkdir(), tdir.mkdir()
    pd.DataFrame({"DATE": ["06/11/2024"], "CLOSE": [16.0]}).to_csv(tmp_path / "vix.csv", index=False)
    day = dt.date(2024, 6, 12)
    legs = write_session(qdir, tdir, day, bd.load_vix(tmp_path / "vix.csv"))
    r, k, _ = legs[0]
    pd.DataFrame(columns=fq.TAPE_COLS).to_parquet(fq.tick_path(tdir, day, r, k, "entry"))
    ev = fq.measure(tdir, tmp_path / "f", quotes=qdir)
    e = ev[(ev.right == r) & (ev.strike == k) & (ev.window == "entry")]
    assert (e.reason == "no_trades_natural").all() and e.cost_c.tolist() == pytest.approx([0.5] * len(e))
    assert (e.width_c == 1).all() and not e.passive.any()
    ev2 = fq.measure(tdir, tmp_path / "f2")
    assert ev2[(ev2.right == r) & (ev2.strike == k) & (ev2.window == "entry")].cost_c.isna().all()


def test_simulate_uses_an_initial_quote_when_the_tape_starts_after_t0():
    tp = tape([("10:00:30", 1.01, 6, 1.00, 1.01, 50, 4),
               ("10:00:40", 1.01, 1, 1.00, 1.01, 50, 4)])
    assert fq.simulate(tp, T0, "sell", 60, "queue", DL).reason == "no_quote"
    o = fq.simulate(tp, T0, "sell", 60, "queue", DL, init=(1.00, 1.01, 50.0, 4.0))
    assert (o.price, o.ts, o.reason) == (1.01, T("10:00:30"), "queue") and o.mid0 == pytest.approx(1.005)


def test_measure_seeds_late_tapes_from_the_one_minute_quote(tmp_path):
    qdir, tdir = tmp_path / "q", tmp_path / "t"
    qdir.mkdir(), tdir.mkdir()
    pd.DataFrame({"DATE": ["06/11/2024"], "CLOSE": [16.0]}).to_csv(tmp_path / "vix.csv", index=False)
    day = dt.date(2024, 6, 12)
    legs = write_session(qdir, tdir, day, bd.load_vix(tmp_path / "vix.csv"))
    r, k, _ = legs[0]
    f = fq.tick_path(tdir, day, r, k, "entry")
    tp = pd.read_parquet(f)
    tp[tp.ts > pd.Timestamp(f"{day} 10:00:00", tz=fq.ET)].to_parquet(f)       # first trade after the decision
    ev = fq.measure(tdir, tmp_path / "f", quotes=qdir)
    e = ev[(ev.right == r) & (ev.strike == k) & (ev.window == "entry")]
    assert e.cost_c.notna().all() and not (e.reason == "no_quote").any()


def test_zero_bid_is_a_real_quote():
    """Far out-of-the-money 0DTE legs quote 0.00 x 0.01: a buy can only pay the ask, a sell rests at 0.01."""
    tp = tape([("09:59:59", 0.01, 1, 0.00, 0.01, 0, 900), ("10:05:00", 0.01, 1, 0.00, 0.01, 0, 900)])
    o = fq.simulate(tp, T0, "buy", 60, "queue", DL)
    assert o.mid0 == pytest.approx(0.005) and o.width_c == 1
    assert (o.price, o.reason) == (0.01, "walked_to_natural") and fq.cost_cents(o, "buy") == pytest.approx(0.5)


def test_leg_sides_skips_a_right_missing_a_strike():
    """A right with only one strike on disk can't be told short from wing, so its legs are skipped (None)."""
    from d_fill_quality import _leg_sides
    files = [("C", 560.0, "entry"), ("C", 562.0, "entry"), ("P", 548.0, "close")]
    sides = _leg_sides(files)
    assert sides[("C", 560.0)] is True and sides[("C", 562.0)] is False
    assert sides[("P", 548.0)] is None
