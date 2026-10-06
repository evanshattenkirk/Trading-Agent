"""Tests for X1 (research/x1_earnings_fly_prereg.md): research/fetch_earnings_edgar.py, research/fetch_thetadata_x1.py
and research/x1_earnings_fly.py."""
import csv
import math
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import bd_real_quotes as bd  # noqa: E402
import fetch_earnings_edgar as E  # noqa: E402
import fetch_thetadata_x1 as F  # noqa: E402
import x1_earnings_fly as X  # noqa: E402

ENTRY, EXIT = 15 * 60 + 45, 10 * 60


# ---------------------------------------------------------------- EDGAR report times
def test_ticker_ciks_and_columnar_rows():
    js = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"}, "1": {"cik_str": 1, "ticker": "ZZZ"}}
    assert E.ticker_ciks(js, ["aapl", "MSFT"]) == {"AAPL": 320193}
    rows = E.columns_to_rows({"form": ["8-K", "4"], "items": ["2.02,9.01", ""], "cik": "x"})
    assert rows == [{"form": "8-K", "items": "2.02,9.01"}, {"form": "4", "items": ""}]


def test_results_filings_keep_only_8k_item_202():
    rows = [{"form": "8-K", "items": "2.02,9.01"}, {"form": "8-K/A", "items": "2.02"},
            {"form": "8-K", "items": "5.02,12.02"}, {"form": "10-Q", "items": "2.02"}]
    assert E.results_filings(rows) == [rows[0]]


def test_accepted_time_readings():
    assert E.accepted_et("2024-08-01T20:31:05.000Z", "utc") == datetime(2024, 8, 1, 16, 31, 5)   # EDT
    assert E.accepted_et("2024-01-25T21:30:00.000Z", "utc") == datetime(2024, 1, 25, 16, 30)     # EST
    assert E.accepted_et("2024-08-01T16:31:05.000Z", "et") == datetime(2024, 8, 1, 16, 31, 5)


def test_classify_sessions():
    # Thursday after the close -> reports into Friday; entry Thursday
    c = E.classify(datetime(2024, 8, 1, 16, 31))
    assert (c["timing"], c["report_session"], c["entry_session"]) == ("amc", date(2024, 8, 2), date(2024, 8, 1))
    # Friday before the open -> reports into Friday; entry Thursday
    c = E.classify(datetime(2024, 7, 12, 6, 50))
    assert (c["timing"], c["report_session"], c["entry_session"]) == ("bmo", date(2024, 7, 12), date(2024, 7, 11))
    # Tuesday before the open after Monday's holiday (2024-01-15 MLK) -> entry the previous Friday
    c = E.classify(datetime(2024, 1, 16, 7, 0))
    assert c["entry_session"] == date(2024, 1, 12)
    # during market hours
    assert E.classify(datetime(2024, 8, 1, 12, 0))["timing"] == "dmh"
    # Saturday filing -> Monday report, Friday entry
    c = E.classify(datetime(2024, 8, 3, 9, 0))
    assert (c["timing"], c["report_session"], c["entry_session"]) == ("amc", date(2024, 8, 5), date(2024, 8, 2))


def test_space_drops_close_filings_and_flags_during_market():
    ev = [{"accepted_et": datetime(2024, 1, 25, 16, 30), "timing": "amc"},
          {"accepted_et": datetime(2024, 2, 20, 16, 30), "timing": "amc"},       # 26 days later: dropped
          {"accepted_et": datetime(2024, 4, 25, 12, 0), "timing": "dmh"},
          {"accepted_et": datetime(2024, 7, 25, 16, 30), "timing": "amc"}]
    st = [e["status"] for e in E.space(ev)]
    assert st[0] == "kept" and st[1].startswith("dropped") and st[2].startswith("skipped") and st[3] == "kept"


def test_pick_zone_uses_both_anchors():
    def evs(sym, timing, n=5):
        return [{"symbol": sym, "timing": timing, "status": "kept"} for _ in range(n)]
    right = evs("AAPL", "amc") + evs("JPM", "bmo")
    wrong = evs("AAPL", "dmh") + evs("JPM", "bmo")       # ET clocks read as UTC: 16:30 -> 12:30
    assert E.pick_zone({"utc": wrong, "et": right}) == "et"
    assert E.pick_zone({"utc": wrong, "et": wrong}) is None


# ---------------------------------------------------------------- quote fetch planning
def test_root_maps_meta_to_fb_before_the_rename():
    assert F.root("META", date(2022, 4, 27)) == "FB"
    assert F.root("META", date(2022, 7, 27)) == "META"
    assert F.root("AAPL", date(2019, 1, 1)) == "AAPL"


def test_plan_picks_first_expiry_on_or_after_exit():
    ev = [{"symbol": "AAPL", "entry": date(2024, 8, 1), "exit": date(2024, 8, 2)},
          {"symbol": "KO", "entry": date(2024, 8, 1), "exit": date(2024, 8, 2)}]
    listed = {"AAPL": {date(2024, 7, 26), date(2024, 8, 2), date(2024, 8, 9)}, "KO": {date(2024, 7, 26)}}
    jobs, skipped = F.plan(ev, lambda r: listed[r])
    assert jobs[0]["expiry"] == date(2024, 8, 2) and jobs[0]["root"] == "AAPL"
    assert skipped[0][0]["symbol"] == "KO"


# ---------------------------------------------------------------- the trade
def tv(s, k, atm):
    return atm * math.exp(-((s - k) / 6.0) ** 2 / 2)


def panel(spot=100.0, atm=4.0, half=0.02, strikes=range(80, 121), skip=()):
    """Synthetic quotes: intrinsic + bell-shaped time value (`atm` at the money)."""
    rows = []
    for t in range(9 * 60 + 30, 16 * 60 + 1):
        for k in strikes:
            for r, intr in (("C", max(spot - k, 0.0)), ("P", max(k - spot, 0.0))):
                if (r, k) in skip:
                    continue
                m = intr + tv(spot, k, atm)
                rows.append({"minute": t, "right": r, "strike": float(k), "bid": round(max(m - half, 0.0), 4),
                             "ask": round(m + half, 4)})
    return bd.QuotePanel(pd.DataFrame(rows))


PRE = panel(atm=4.0)
POST = panel(atm=1.0)                 # implied vol crushed, stock unchanged
EV = {"symbol": "AAPL", "entry": "2024-08-01", "exit": "2024-08-02", "vix_day": "2024-07-31"}


def test_leg_prices_by_model():
    q = (1.00, 1.10)
    assert X.leg_px(q, "sell", "mid") == pytest.approx(1.05)
    assert X.leg_px(q, "sell", "mid1") == pytest.approx(1.04) and X.leg_px(q, "buy", "mid1") == pytest.approx(1.06)
    assert X.leg_px((1.00, 1.01), "sell", "mid1") == pytest.approx(1.00)          # never past the bid
    assert X.leg_px(q, "buy", "mid_frac", 0.35) == pytest.approx(1.05 + 0.35 * 0.05)
    assert X.leg_px(q, "sell", "taker") == 1.00 and X.leg_px(q, "buy", "taker") == 1.10


def test_build_places_wings_one_and_a_half_straddles_out():
    st, why = X.build(PRE, X.CFG)
    assert why is None and st["k"] == 100
    assert st["straddle"] == pytest.approx(8.0, abs=1e-3)
    assert (st["cw"], st["pw"]) == (112, 88) and st["width"] == 12


def test_build_skips():
    wide = panel(half=0.60)
    assert X.build(wide, X.CFG)[1].startswith("ATM call: spread")
    short = panel(strikes=range(95, 106))
    assert X.build(short, {**X.CFG, "wing_x": 1.5})[0]["cw"] == 105               # nearest listed strike
    nowing = panel(strikes=range(100, 106))
    assert X.build(nowing, X.CFG)[1] == "chain too short"
    cheap = panel(atm=0.4)
    assert X.build(cheap, {**X.CFG, "wing_x": 5.0, "min_credit_width": 0.9})[1] == "credit below 25% of width"


def test_replay_profits_from_the_crush_and_charges_costs():
    got = X.replay(EV, PRE, POST, 15.0, X.CFG)
    rows = {r["model"]: r for r in got["rows"]}
    assert rows["mid"]["pnl"] > rows["mid1"]["pnl"] > rows["taker"]["pnl"]
    gap = rows["mid"]["pnl"] - rows["taker"]["pnl"]
    assert gap == pytest.approx(8 * 0.02 * 100, abs=0.5)                          # 4 legs x 2 sides x half-spread
    mid = rows["mid"]
    assert mid["pnl"] == pytest.approx((mid["credit"] - mid["debit"]) * 100 - 0.32, abs=0.01)
    assert mid["ret"] == pytest.approx(mid["pnl"] / ((12 - mid["credit"]) * 100))


def test_replay_loses_on_a_big_move_but_no_more_than_max_loss():
    moved = panel(spot=125.0, atm=1.0, strikes=range(80, 131))
    rows = X.replay(EV, PRE, moved, 15.0, X.CFG)["rows"]
    assert all(-1.05 < r["ret"] < 0 for r in rows)


def test_replay_vix_filter_and_missing_exit():
    assert X.replay(EV, PRE, POST, 31.0, X.CFG) == {"skipped": "prior VIX close above 30"}
    assert X.replay(EV, PRE, POST, None, X.CFG) == {"skipped": "no prior VIX close"}
    assert "rows" in X.replay(EV, PRE, POST, 31.0, {**X.CFG, "use_vix": False})
    assert X.replay(EV, PRE, None, 15.0, X.CFG) == {"skipped": "no exit quote"}
    gone = panel(atm=1.0, skip={("C", 112)})
    assert X.replay(EV, PRE, gone, 15.0, X.CFG) == {"skipped": "no exit quote"}


# ---------------------------------------------------------------- statistics and the bar
def rows_of(rets, start="2018-01-02"):
    out = []
    for i, r in enumerate(rets):
        d = pd.Timestamp(start) + pd.Timedelta(days=7 * i)
        out.append({"entry": d.date().isoformat(), "pnl": r * 100, "ret": r})
    return out


def test_passes_needs_pf_t_and_both_halves():
    good = [0.2, 0.1, -0.05] * 140
    blk = X.block(rows_of(good))
    assert X.passes(blk)
    bad_half = X.block(rows_of(good[:200]) + [{"entry": "2023-01-02", "pnl": -50, "ret": -0.5}])
    assert not X.passes(bad_half)                   # no positive 2022-2026 half
    noisy = X.block(rows_of([0.6, -0.55] * 50))
    assert not X.passes(noisy)


def test_sanity_ratio():
    daily = {"AAPL": {"2024-07-31": (100, 100), "2024-08-01": (100, 100), "2024-08-02": (110, 110),
                      "2024-08-05": (110.5, 110)}}
    ev = [{"symbol": "AAPL", "entry": "2024-08-01", "exit": "2024-08-02"}]
    s = X.sanity(ev, daily)
    assert s["events"] == 1 and s["valid"] and s["ratio"] == pytest.approx(0.10 / ((0 + 0.5 / 110) / 2), rel=0.01)


def test_run_end_to_end(tmp_path):
    with open(tmp_path / "earnings.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=E.FIELDS)
        w.writeheader()
        w.writerow({"symbol": "AAPL", "accepted_et": "2024-08-01T16:31:00", "timing": "amc",
                    "report_session": "2024-08-02", "entry_session": "2024-08-01", "status": "kept"})
        w.writerow({"symbol": "MSFT", "accepted_et": "2024-07-30T16:05:00", "timing": "amc",
                    "report_session": "2024-07-31", "entry_session": "2024-07-30", "status": "kept"})
    q = tmp_path / "q"
    q.mkdir()
    frames = []
    for d, p in (("2024-08-01", 4.0), ("2024-08-02", 1.0)):
        for t in (ENTRY, EXIT):
            for k in range(80, 121):
                for r, intr in (("C", max(100 - k, 0)), ("P", max(k - 100, 0))):
                    m = intr + tv(100, k, p)
                    frames.append({"ms_of_day": t * 60_000, "right": r, "strike": float(k),
                                   "bid": max(m - 0.02, 0), "ask": m + 0.02, "session": d})
    pd.DataFrame(frames).to_parquet(q / "AAPL_2024-08-02.parquet")
    (tmp_path / "vix.csv").write_text("DATE,OPEN,HIGH,LOW,CLOSE\n07/31/2024,16,16,16,16.4\n")
    res = X.run(tmp_path / "earnings.csv", q, tmp_path / "vix.csv", tmp_path / "none.csv", tmp_path)
    assert res["X1"]["models"]["taker"]["full"]["trades"] == 1
    assert res["X1"]["skipped"]["no quotes on disk"] == 1
    assert (tmp_path / "x1_earnings_fly.md").read_text().startswith("# X1")
