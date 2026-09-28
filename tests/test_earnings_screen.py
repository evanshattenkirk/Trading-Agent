"""Book E's earnings screen (HANDOFF 7E): trading-day counting and the E1 / E2 / exit windows."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.earnings import heavyweights_overnight, parse_calendar, screen, trading_days_between

MON = date(2026, 10, 5)
NO_HOLIDAYS: set = set()


def row(sym, d, timing="pm", verified=True, actual=None):
    # shape verified against Robinhood get_earnings_calendar on 2026-09-28
    return {"symbol": sym, "year": 2026, "quarter": 3, "eps": {"estimate": "1.0", "actual": actual},
            "report": {"date": d, "timing": timing, "verified": verified}}


def test_parse_calendar_reads_the_robinhood_shape():
    cal = parse_calendar({"results": [row("AAPL", "2026-10-29", "pm"), row("XOM", "2026-10-30", "am", verified=False),
                                      {"symbol": "BAD"}]})
    assert cal == [
        {"symbol": "AAPL", "date": date(2026, 10, 29), "timing": "pm", "verified": True},
        {"symbol": "XOM", "date": date(2026, 10, 30), "timing": "am", "verified": False},
    ]


def test_parse_calendar_accepts_config_entries_and_empty():
    assert parse_calendar(None) == []
    cal = parse_calendar([{"symbol": "nvda", "date": "2026-10-07", "timing": "PM"}])
    assert cal == [{"symbol": "NVDA", "date": date(2026, 10, 7), "timing": "pm", "verified": True}]


def test_trading_days_skip_weekends_and_holidays():
    assert trading_days_between(MON, MON, NO_HOLIDAYS) == 0
    assert trading_days_between(MON, date(2026, 10, 9), NO_HOLIDAYS) == 4          # Fri
    assert trading_days_between(MON, date(2026, 10, 12), NO_HOLIDAYS) == 5         # next Mon
    assert trading_days_between(MON, date(2026, 10, 12), {date(2026, 10, 8)}) == 4
    assert trading_days_between(MON, date(2026, 10, 2), NO_HOLIDAYS) == -1         # last Fri


def test_screen_flags_e2_e1_and_exit_windows():
    cal = parse_calendar({"results": [
        row("AAPL", "2026-10-19", "pm"),   # T=10 -> E2 window
        row("MSFT", "2026-10-15", "pm"),   # T=8  -> E2 window
        row("META", "2026-10-14", "pm"),   # T=7  -> between windows
        row("NVDA", "2026-10-08", "pm"),   # T=3  -> E1 entry
        row("XOM", "2026-10-06", "am"),    # T=1, reports before tomorrow's open -> exit today
        row("JPM", "2026-10-05", "pm"),    # T=0, reports after today's close -> exit today
        row("KO", "2026-10-05", "am"),     # reported this morning -> gone
        row("ZZZZ", "2026-10-08", "pm"),   # not in the universe
    ]})
    out = {r["symbol"]: r for r in screen(cal, MON, ["AAPL", "MSFT", "META", "NVDA", "XOM", "JPM", "KO"], NO_HOLIDAYS)}
    assert out["AAPL"]["T"] == 10 and out["AAPL"]["flag"] == "E2"
    assert out["MSFT"]["flag"] == "E2"
    assert out["META"]["flag"] is None
    assert out["NVDA"]["flag"] == "E1"
    assert out["XOM"]["flag"] == "exit"
    assert out["JPM"]["flag"] == "exit"
    assert "KO" not in out and "ZZZZ" not in out
    assert [r["T"] for r in screen(cal, MON, ["AAPL", "NVDA", "JPM"], NO_HOLIDAYS)] == [0, 3, 10]   # soonest first


def test_screen_unknown_timing_exits_a_day_early_and_unverified_is_tentative():
    cal = parse_calendar({"results": [row("ORCL", "2026-10-06", "", verified=False)]})
    (r,) = screen(cal, MON, ["ORCL"], NO_HOLIDAYS)
    assert r["flag"] == "exit" and r["tentative"] is True


def test_screen_ignores_names_past_the_horizon():
    cal = parse_calendar({"results": [row("AAPL", "2026-11-30", "pm")]})
    assert screen(cal, MON, ["AAPL"], NO_HOLIDAYS) == []


def test_heavyweights_overnight_catches_last_night_and_this_morning():
    cal = parse_calendar({"results": [row("NVDA", "2026-10-02", "pm"), row("JPM", "2026-10-05", "am"),
                                      row("AAPL", "2026-10-05", "pm"), row("XOM", "2026-10-02", "pm")]})
    got = heavyweights_overnight(cal, MON, ["NVDA", "JPM", "AAPL"], NO_HOLIDAYS)
    assert [r["symbol"] for r in got] == ["NVDA", "JPM"]
