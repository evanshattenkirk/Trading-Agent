"""Tests for research/f2_real_quotes.py, research/fetch_thetadata_equity.py and research/nyse_calendar.py (book F2
replayed on real single-name option quotes)."""
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import bd_real_quotes as bd  # noqa: E402
import f2_real_quotes as R  # noqa: E402
import fetch_thetadata_equity as T  # noqa: E402
import nyse_calendar as N  # noqa: E402

CFG = {"rvol5_min": 2.0, "max_armed_c": 3, "entry_cutoff_et": "10:30", "up_pct_p": 3.0, "vol_ratio_p": 1.8,
       "max_new_p": 2, "hold_days_c": 3, "exit_et_c": "15:30", "exit_et_p": "15:40", "dte": [5, 12],
       "width_straddle_x": 1.0, "min_steps": 2, "max_debit_width": 0.60, "max_leg_spread_pct": 0.08,
       "tick_exempt": 0.05, "fill_frac": 0.35, "take_profit_x": 2.0, "take_profit_width": 0.80, "stop_x": 0.50}
MON, TUE, WED = "2024-03-04", "2024-03-05", "2024-03-06"


def m(hhmm: str) -> int:
    h, mi = hhmm.split(":")
    return int(h) * 60 + int(mi)


def tv(s: float, k: float) -> float:
    return 2.0 * math.exp(-((s - k) / 4.0) ** 2 / 2)


def panel(spot, strikes=range(90, 111), half=0.02):
    """Synthetic quotes: intrinsic + a bell-shaped time value (2.00 at the money), 4c wide. `spot` is a number or a
    function of the minute."""
    f = spot if callable(spot) else (lambda t: spot)
    rows = []
    for t in range(m("09:30"), m("16:00") + 1):
        s = f(t)
        for k in strikes:
            for right, intrinsic in (("C", max(s - k, 0.0)), ("P", max(k - s, 0.0))):
                mid = intrinsic + tv(s, k)
                rows.append({"minute": t, "right": right, "strike": float(k), "bid": round(mid - half, 4),
                             "ask": round(mid + half, 4)})
    return bd.QuotePanel(pd.DataFrame(rows))


FLAT = panel(100.0)


def sig(setup="C", day=MON, minute=None, spot=100.0, or_low=99.0):
    return {"day": day, "symbol": "NVDA", "setup": setup, "minute": minute or (m("09:45") if setup == "C" else m("15:40")),
            "spot": spot, "or_low": or_low if setup == "C" else None}


# ---------------------------------------------------------------- pricing and structure
def test_spread_px_models():
    ql, qs = (1.98, 2.02), (1.19, 1.23)
    assert R.spread_px(ql, qs, "mid", True) == pytest.approx(0.79)
    assert R.spread_px(ql, qs, "taker", True) == pytest.approx(2.02 - 1.19)
    assert R.spread_px(ql, qs, "taker", False) == pytest.approx(1.98 - 1.23)
    assert R.spread_px(ql, qs, "mid_frac", True, 0.35) == pytest.approx(0.79 + 0.35 * 0.04)


def test_build_call_spread_short_strike_one_straddle_out():
    got, why = R.build(FLAT, 100.0, "C", m("09:45"), CFG)
    assert why is None and got["right"] == "C" and got["long"] == 100 and got["short"] == 104
    assert got["straddle"] == pytest.approx(4.0)
    mid = tv(100, 100) - tv(100, 104)
    assert got["debit"]["mid"] == pytest.approx(mid, abs=1e-3)                  # quotes are rounded to 4 places
    assert got["debit"]["mid_frac"] == pytest.approx(mid + 0.35 * 0.04, abs=1e-3)


def test_build_put_spread_and_leg_checks():
    got, why = R.build(FLAT, 100.2, "P", m("15:40"), CFG)
    assert why is None and got["right"] == "P" and got["long"] == 100 and got["short"] == 96
    wide = panel(100.0, half=0.40)
    got, why = R.build(wide, 100.0, "C", m("09:45"), CFG)
    assert got is None and why.startswith("ATM call 100: spread")


# ---------------------------------------------------------------- replay exits
def test_replay_take_profit_on_a_run_up():
    up = panel(lambda t: 100.0 + min(4.0, max(0.0, (t - m("10:00")) / 30)))
    r = R.replay(sig(), {MON: FLAT, TUE: up, WED: FLAT}, {}, WED, CFG, "mid_frac")
    assert r["exit_why"] == "take profit" and r["exit_at"].startswith(TUE) and r["pnl"] > 0


def test_replay_stop_on_a_sell_off():
    down = panel(lambda t: 100.0 - min(8.0, max(0.0, (t - m("10:00")) / 10)))
    r = R.replay(sig(), {MON: FLAT, TUE: down, WED: FLAT}, {}, WED, CFG, "taker")
    assert r["exit_why"] == "stop" and r["exit_at"].startswith(TUE) and r["pnl"] < 0


def test_replay_time_exit_and_half_day_exit():
    r = R.replay(sig(), {MON: FLAT, TUE: FLAT, WED: FLAT}, {}, WED, CFG, "mid_frac")
    assert r["exit_why"] == "time exit" and r["exit_at"] == f"{WED} 15:30"
    entry, out = r["entry"], r["exit"]
    assert r["pnl"] == pytest.approx((out - entry) * 100 - 0.16, abs=0.01) and r["pnl"] < 0
    r = R.replay(sig(), {MON: FLAT, TUE: FLAT, WED: FLAT}, {}, WED, CFG, "mid", half_day=True)
    assert r["exit_at"] == f"{WED} 12:45"
    r = R.replay(sig("P"), {MON: FLAT, TUE: FLAT}, {}, TUE, CFG, "mid")
    assert r["exit_why"] == "time exit" and r["exit_at"] == f"{TUE} 15:40" and r["pnl"] == pytest.approx(-0.16)


def test_replay_thesis_stop_uses_the_stock_on_day_one_only():
    bars = {MON: [{"t": m("10:10"), "o": 99.5, "h": 99.6, "l": 98.9, "c": 99.0, "v": 1}],
            TUE: [{"t": m("10:10"), "o": 99.5, "h": 99.6, "l": 98.0, "c": 98.5, "v": 1}]}
    r = R.replay(sig(), {MON: FLAT, TUE: FLAT, WED: FLAT}, bars, WED, CFG, "mid")
    assert r["exit_why"] == "thesis stop" and r["exit_at"] == f"{MON} 10:10"
    r = R.replay(sig(), {MON: FLAT, TUE: FLAT, WED: FLAT}, {TUE: bars[TUE]}, WED, CFG, "mid")
    assert r["exit_why"] == "time exit"


def test_replay_skips_what_the_book_would_skip():
    assert R.replay(sig(spot=100.0), {MON: panel(100.0, strikes=range(99, 102))}, {}, WED, CFG, "mid") == \
        {"skipped": "chain too short"}


# ---------------------------------------------------------------- signals
def _bars(o, c, vol, n=5):
    """The first n minutes from 09:30, moving in a straight line from o to c."""
    step = (c - o) / n
    return [{"t": 570 + i, "o": o + step * i, "h": max(o + step * i, o + step * (i + 1)),
             "l": min(o + step * i, o + step * (i + 1)), "c": o + step * (i + 1), "v": vol / n} for i in range(n)]


def test_c_signals_green_rvol_and_breakout_before_the_cutoff():
    d = date(2024, 3, 4)
    bars = {"NVDA": _bars(100.0, 101.0, 5000) + [{"t": 580, "o": 101.0, "h": 101.5, "l": 100.9, "c": 101.4, "v": 900}],
            "AMD": _bars(100.0, 99.0, 9000) + [{"t": 580, "o": 99.0, "h": 101.5, "l": 98.9, "c": 101.4, "v": 900}],
            "MU": _bars(50.0, 51.0, 3000) + [{"t": 640, "o": 51.0, "h": 52.0, "l": 50.9, "c": 51.9, "v": 900}]}
    hist = [{"NVDA": 1000, "AMD": 1000, "MU": 1000}] * 14
    got = R.c_signals(d, bars, hist, ["NVDA", "AMD", "MU"], CFG)
    assert [(g["symbol"], g["minute"]) for g in got] == [("NVDA", 580)]      # AMD red; MU breaks out after 10:30
    assert got[0]["spot"] == pytest.approx(101.0) and got[0]["rvol5"] == pytest.approx(5.0)
    assert R.c_signals(d, bars, hist[:13], ["NVDA"], CFG) == []              # needs 14 sessions of history


def test_p_signals_up_day_on_heavy_volume():
    d = date(2024, 3, 4)
    daily = {"NVDA": [{"d": d - timedelta(days=30 - i), "o": 100, "h": 101, "l": 99, "c": 100.0, "v": 1e6}
                      for i in range(25)]}
    daily["AMD"] = daily["NVDA"]
    today = [{"t": t, "o": 104.0, "h": 104.1, "l": 103.9, "c": 104.0, "v": 2e6 / 370} for t in range(570, 940)]
    got = R.p_signals(d, {"NVDA": today, "AMD": today[:10]}, daily, ["NVDA", "AMD"], CFG)
    assert [(g["symbol"], g["setup"], g["minute"]) for g in got] == [("NVDA", "P", m("15:40"))]
    assert got[0]["chg_pct"] == pytest.approx(4.0) and got[0]["vol_ratio"] == pytest.approx(2.0)


# ---------------------------------------------------------------- stats
def test_summary_and_pass_bar():
    rows = [{"day": "2019-05-01", "pnl": 30.0, "ret": 0.3}, {"day": "2023-05-01", "pnl": -10.0, "ret": -0.1}]
    s = R.summary(rows)
    assert s["trades"] == 2 and s["pf"] == pytest.approx(3.0) and s["mean_ret"] == pytest.approx(0.1)
    assert R.passes({"pf": 1.2, "t": 2.1}, {"mean_ret": 0.01}, {"mean_ret": 0.02})
    assert not R.passes({"pf": 1.2, "t": 2.1}, {"mean_ret": 0.01}, {"mean_ret": -0.02})


# ---------------------------------------------------------------- fetcher
def test_plan_picks_the_books_expiry_and_the_hold_sessions():
    d0 = date(2024, 3, 4)
    exps = {"NVDA": {d0 + timedelta(days=4), d0 + timedelta(days=11), d0 + timedelta(days=18)}, "ORCL": {d0 + timedelta(days=30)}}
    sigs = [sig(), {**sig("P", day="2024-03-08", spot=104.0), "symbol": "NVDA"}, {**sig(), "symbol": "ORCL"}]
    jobs, skipped = T.plan(sigs, lambda s: exps[s], CFG)
    assert [(j["setup"], j["expiry"], j["sessions"]) for j in jobs] == [
        ("C", date(2024, 3, 15), [date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6)]),
        ("P", date(2024, 3, 15), [date(2024, 3, 8), date(2024, 3, 11)])]
    assert [(s["symbol"], why) for s, why in skipped] == [("ORCL", "no expiry 5-12 days out")]


def test_plan_counts_holidays_in_the_hold():
    thu = "2024-03-28"                          # Good Friday 2024-03-29 is closed: C exits Tuesday, P Monday
    exps = {date(2024, 4, 5)}
    jobs, _ = T.plan([sig(day=thu), sig("P", day=thu)], lambda s: exps, CFG)
    assert [j["sessions"][-1] for j in jobs] == [date(2024, 4, 2), date(2024, 4, 1)]


def test_trim_converts_thousandths_and_keeps_the_band():
    df = pd.DataFrame({"strike": [50000, 76000, 100000, 124000, 130000], "right": ["C"] * 5})
    got = T.trim(df, 100.0)
    assert got["strike"].tolist() == [76.0, 100.0, 124.0]
    assert T.trim(pd.DataFrame({"strike": [14.0, 15.0, 30.0]}), 15.0)["strike"].tolist() == [14.0, 15.0]


class NoDataFoundError(Exception):
    pass


class FakeTheta:
    def __init__(self, script):
        self.script, self.calls = script, []

    def option_history_quote(self, **kw):
        self.calls.append(kw)
        out = self.script[kw["date"]]
        if isinstance(out, list):
            out = out.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _q(strikes):
    return pd.DataFrame({"strike": strikes, "right": ["C"] * len(strikes), "bid": 1.0, "ask": 1.1,
                         "timestamp": ["2024-03-04T09:45:00"] * len(strikes)})


def test_fetch_job_tags_sessions_retries_transient_and_skips_empty_days():
    job = {"symbol": "NVDA", "day": MON, "setup": "C", "spot": 100.0, "expiry": date(2024, 3, 15),
           "sessions": [date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6)]}
    client = FakeTheta({date(2024, 3, 4): _q([60.0, 100.0]), date(2024, 3, 5): [ConnectionError("reset"), _q([101.0])],
                        date(2024, 3, 6): NoDataFoundError()})
    slept = []
    df = T.fetch_job(client, job, sleep=slept.append)
    assert df["session"].tolist() == ["2024-03-04", "2024-03-05"] and df["strike"].tolist() == [100.0, 101.0]
    assert slept == [5.0] and len(client.calls) == 4
    assert client.calls[0]["expiration"] == date(2024, 3, 15) and client.calls[0]["strike"] == "*"


def test_fetch_job_without_entry_day_quotes_is_no_data():
    job = {"symbol": "NVDA", "day": MON, "setup": "C", "spot": 100.0, "expiry": date(2024, 3, 15),
           "sessions": [date(2024, 3, 4), date(2024, 3, 5)]}
    client = FakeTheta({date(2024, 3, 4): NoDataFoundError(), date(2024, 3, 5): _q([100.0])})
    assert T.fetch_job(client, job, sleep=lambda s: None) is None and len(client.calls) == 1
    with pytest.raises(ValueError):
        T.fetch_job(FakeTheta({date(2024, 3, 4): ValueError("bad request")}), job, sleep=lambda s: None)


def test_saved_file_round_trips_into_replay_panels(tmp_path):
    pytest.importorskip("pyarrow")
    rows = []
    for d in (MON, TUE):
        for k in (98.0, 100.0, 102.0, 104.0):
            for r in ("CALL", "PUT"):
                rows.append({"timestamp": f"{d}T09:45:00", "strike": k, "right": r, "bid": 1.0, "ask": 1.04,
                             "session": d})
    path = tmp_path / "NVDA_2024-03-04_C.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    panels = R.load_panels(path)
    assert sorted(panels) == [MON, TUE] and panels[MON].strikes("C") == [98.0, 100.0, 102.0, 104.0]
    assert panels[TUE].ba("P", 100.0, m("10:00")) == (1.0, 1.04)


# ---------------------------------------------------------------- calendar
def test_nyse_calendar_closures_and_early_closes():
    h24 = N.holidays(2024)
    assert {date(2024, 1, 1), date(2024, 3, 29), date(2024, 6, 19), date(2024, 7, 4), date(2024, 11, 28),
            date(2024, 12, 25)} <= h24 and len(h24) == 10
    assert N.half_days(2024) == {date(2024, 7, 3), date(2024, 11, 29), date(2024, 12, 24)}
    assert N.half_days(2021) == {date(2021, 11, 26)}                    # Dec 24 2021 was the Christmas closure
    assert len(N.holidays(2022)) == 9                                   # a Saturday New Year's Day isn't moved back
    assert N.half_days(2026) == {date(2026, 11, 27), date(2026, 12, 24)}  # July 3 2026 is the July 4 closure
    assert date(2018, 12, 5) in N.holidays(2018) and date(2025, 1, 9) in N.holidays(2025)
    assert N.holidays_between(2024, 2025) == N.holidays(2024) | N.holidays(2025)
