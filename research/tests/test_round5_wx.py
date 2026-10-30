"""Tests for research/round5_wx.py (rules in research/round5_wx_prereg.md). Synthetic data only."""
import math
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH.parent))

import bd_real_quotes as bd  # noqa: E402
import round5_w as w5  # noqa: E402
import round5_wx as wx  # noqa: E402

E1300, X1540 = 13 * 60, 15 * 60 + 40
STRIKES = [float(k) for k in range(90, 111)]
HS = 0.02                                             # half spread


def prices(spot, tv, width):
    """Parity-consistent mids: intrinsic + a time value that decays away from the money."""
    out = {}
    for k in STRIKES:
        t = tv * math.exp(-abs(k - spot) / width)
        out[("C", k)] = max(spot - k, 0.0) + t
        out[("P", k)] = max(k - spot, 0.0) + t
    return out


def chain(before, after, switch=X1540 - 30, minutes=range(570, 961), hs=HS):
    rows = []
    for m in minutes:
        px = before if m < switch else after
        for (r, k), mid in px.items():
            rows.append({"ms_of_day": m * 60_000, "strike": k, "right": r,
                         "bid": max(round(mid - hs, 2), 0.0), "ask": round(mid + hs, 2)})
    return pd.DataFrame(rows)


def panel(before, after=None, **kw):
    return bd.QuotePanel(bd.normalize(chain(before, after or before, **kw)))


ENTRY = prices(100.0, 1.0, 3.0)                       # straddle at 100: 2.00, wings at 96 / 104
LATE = prices(101.0, 0.10, 1.0)                       # up a dollar into the close, little time value left


def fly_mid_credit(p, minute=E1300):
    mid = lambda r, k: sum(p.ba(r, k, minute)) / 2  # noqa: E731
    return mid("C", 100.0) + mid("P", 100.0) - mid("C", 104.0) - mid("P", 96.0)


# ---------------------------------------------------------------- fees and settlement value

def test_settlement_takes_the_given_close_and_the_index_fee_on_four_legs():
    p = panel(ENTRY, LATE)
    r = w5.settlement(p, E1300, 2.0, "mid", fee=wx.FEE_RH, s_end=101.5)
    assert r["credit"] == pytest.approx(fly_mid_credit(p))
    assert r["pnl"] == pytest.approx(100 * (r["credit"] - 1.5) - 4 * 0.54)
    assert r["max_loss"] == pytest.approx(100 * (4.0 - r["credit"]) + 4 * 0.54)
    w_default = w5.settlement(p, E1300, 2.0, "mid")              # W's own row is unchanged: parity spot, $0.04
    assert w_default["pnl"] == pytest.approx(100 * (r["credit"] - 1.0) - 4 * 0.04, abs=0.05)


def test_the_1540_exit_pays_the_index_fee_on_every_leg_traded():
    p = panel(ENTRY, LATE)
    a, b = w5.trade(p, E1300, 2.0, "patient"), w5.trade(p, E1300, 2.0, "patient", fee=wx.FEE_RH)
    legs = 4 + 2 + a["wings_sold"]
    assert b["pnl"] == pytest.approx(a["pnl"] - legs * 0.50)
    assert b["max_loss"] == pytest.approx(a["max_loss"] + 8 * 0.50)


def test_fred_close_over_ten_is_the_settlement_value_else_the_parity_spot(tmp_path):
    f = tmp_path / "sp500.csv"
    f.write_text("observation_date,SP500\n2024-03-01,5137.08\n2024-03-04,\n2024-03-05,.\n")
    fred = wx.load_fred(f)
    assert fred == {date(2024, 3, 1): 5137.08}
    p = panel(ENTRY, LATE)
    assert wx.settle_spot(p, date(2024, 3, 1), fred) == (pytest.approx(513.708), "fred")
    s, src = wx.settle_spot(p, date(2024, 3, 4), fred)
    assert src == "parity" and s == pytest.approx(101.0, abs=0.01)


# ---------------------------------------------------------------- one session

def _write(tmp_path, day, df):
    path = tmp_path / f"{day.isoformat()}.parquet"
    df.to_parquet(path, index=False)
    return str(path)


def test_session_rows_settle_at_fred_with_index_fees_and_record_the_data_check(tmp_path):
    day = date(2024, 3, 1)
    rows = wx.session_rows("XSP", day, _write(tmp_path, day, chain(ENTRY, LATE)), {day: 1015.0})
    main = [r for r in rows if r["variant"] == "settle" and r["model"] == "mid"]
    assert len(main) == 1 and main[0]["skip"] is None
    m = main[0]
    assert m["settle_src"] == "fred" and m["s_settle"] == pytest.approx(101.5)
    assert m["s_parity"] == pytest.approx(101.0, abs=0.01)
    assert m["pnl"] == pytest.approx(100 * (m["credit"] - 1.5) - 4 * 0.54)
    variants = {r["variant"] for r in rows}
    assert {"settle", "settle_fee075", "x1540", "settle_cell"} <= variants
    cells = {(r["entry"], r["w"]) for r in rows if r["variant"] == "settle_cell"}
    assert cells == {(e, w) for e in w5.ENTRIES for w in w5.WINGS}
    stress = [r for r in rows if r["variant"] == "settle_fee075" and r["model"] == "mid"][0]
    assert stress["pnl"] == pytest.approx(m["pnl"] - 4 * 0.21)
    assert m["spread_med"] == pytest.approx(0.04)


def test_an_expiry_with_no_quotes_after_13_00_is_not_a_pm_settled_session(tmp_path):
    day = date(2024, 3, 1)
    df = chain(ENTRY, ENTRY, minutes=range(570, 721))   # quotes stop at 12:00
    rows = wx.session_rows("XSP", day, _write(tmp_path, day, df), {})
    assert rows and all(r["skip"] == "no_pm_quotes" for r in rows)


def test_half_days_are_dropped(tmp_path):
    day = date(2024, 11, 29)                            # day after Thanksgiving, 13:00 ET close
    rows = wx.session_rows("XSP", day, _write(tmp_path, day, chain(ENTRY, LATE)), {})
    assert rows and all(r["skip"] == "half_day" for r in rows)


# ---------------------------------------------------------------- bars

def _t(n, mean, sd=1.0):
    return mean / (sd / math.sqrt(n))


def test_halves_split_at_the_median_trade_date():
    df = pd.DataFrame({"day": [date(2020, 1, d) for d in range(1, 6)], "ret": [1, 2, 3, -4, -5]})
    a, b = wx.halves(df)
    assert list(a["ret"]) == [1, 2, 3] and list(b["ret"]) == [-4, -5]


def test_verdict_needs_all_five_and_is_provisional_without_fresh_data():
    good = {"main_mean": 0.05, "main_t": 2.7, "taker_mean": 0.01, "half_means": [0.04, 0.06],
            "cap_mean": 0.03, "fresh_mean": 0.02, "fresh_t": 1.8}
    v = wx.verdict(good)
    assert v["pass"] and not v["provisional"] and v["t_clears_2.58"] and v["t_clears_2.33"]
    for k, bad in [("main_mean", -0.01), ("main_t", 2.6), ("taker_mean", -0.001), ("half_means", [0.04, -0.01]),
                   ("cap_mean", -0.01), ("fresh_mean", -0.01), ("fresh_t", 1.6)]:
        assert not wx.verdict({**good, k: bad})["pass"], k
    v = wx.verdict({**good, "fresh_mean": None, "fresh_t": None})
    assert not v["pass"] and v["provisional"] and v["bars_1_4"]
    assert wx.verdict({**good, "main_t": 2.6})["t_clears_2.58"]


def test_report_runs_end_to_end_and_reads_fresh_rows_from_w(tmp_path):
    fred, files = {}, {}
    for i, d in enumerate([date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6), date(2024, 3, 7)]):
        late = prices(100.0 + (0.5 if i % 2 else -0.5), 0.10, 1.0)
        files[d] = _write(tmp_path, d, chain(ENTRY, late))
        fred[d] = 1000.0 + (5 if i % 2 else -5)
    df = pd.DataFrame([r for d, f in files.items() for r in wx.session_rows("XSP", d, f, fred)])
    w = pd.DataFrame([{"und": u, "day": d, "variant": "settle", "entry": E1300, "w": 2.0, "model": "patient",
                       "skip": None, "ret": r, "pnl": 10 * r, "max_loss": 100.0}
                      for u, rr in {"QQQ": [0.1, 0.2, -0.05], "IWM": [0.05, 0.1, 0.0], "SPY": [0.1, 0.0, 0.1]}.items()
                      for d, r in zip([date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6)], rr)])
    res = wx.report(df, w, None, None)
    assert res["verdict"]["provisional"] is False
    assert res["bars"]["fresh_mean"] == pytest.approx(pd.Series([0.075, 0.15, -0.025]).mean())
    assert res["xsp_patient"]["n"] == 4
    assert set(res["settlement_source"]) == {"fred"}
    assert res["fred_vs_parity"]["n"] == 4
    assert res["xsp_vs_spy_same_day"]["n"] == 3


def test_report_runs_when_every_session_is_skipped(tmp_path):
    day = date(2024, 3, 1)
    df = pd.DataFrame(wx.session_rows("XSP", day, _write(tmp_path, day, chain(ENTRY, ENTRY, minutes=range(570, 721))),
                                      {}))
    res = wx.report(df, None, None, None)
    assert res["xsp_patient"]["n"] == 0 and not res["verdict"]["pass"] and res["verdict"]["provisional"]
    assert res["reported_only"]["skips_patient"] == {"sessions": 1, "no_pm_quotes": 1}
    assert res["date_range"] is None
