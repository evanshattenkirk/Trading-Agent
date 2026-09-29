"""Book F1 v2 (research/strategy_f1_prereg.md): broad universe, opening-range-low stop, overextended skip, the
published-stop shadow, observe-only tags and the new data sources. The section 3 spec must trade exactly as before."""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import pytest

from f_fakes import CFG, FakeData
from test_f_host import DAY, at, entered, et, make, quote, run, scanned
from agentdesk.books import f_stocks_in_play as F
from agentdesk.books.f_report import build_report, format_report
from agentdesk.feeds import f_data as FD

V2 = {**CFG, "stop_mode": "or_low", "stop_atr_min": 0.10, "stop_atr_max": 0.50, "max_or_atr": 0.50, "top_n": 10}


# ------------------------------------------------------------------ pure rules
def test_or_low_stop_is_clamped_to_the_atr_band():
    assert F.stop_price(100.0, 2.0, V2, or_low=99.5) == pytest.approx(99.5)       # 0.25 ATR: the OR low itself
    assert F.stop_price(100.0, 2.0, V2, or_low=99.95) == pytest.approx(99.8)      # 0.025 ATR: widened to 0.10
    assert F.stop_price(100.0, 2.0, V2, or_low=98.0) == pytest.approx(99.0)       # 1.0 ATR: capped at 0.50
    assert F.paper_rule_stop(100.0, 2.0, V2) == pytest.approx(99.8)


def test_section_3_config_keeps_the_atr_stop_even_with_an_or_low():
    assert F.stop_price(100.0, 2.0, CFG, or_low=99.5) == pytest.approx(99.8)
    bar = {"o": 99.9, "h": 100.3, "l": 99.9, "c": 100.2}
    fill, stop, out = F.bar_entry_then_stop(bar, 100.0, 100.05, 2.0, CFG, or_low=99.0)
    assert stop == pytest.approx(fill - 0.2) and out is None


def _row(sym, rvol, or_high, or_low, atr=2.0, o=100.0, c=100.4):
    return F.ScanRow(sym, rvol, o, c, or_high, or_low, 1000, atr, 5e8, prev_close=98.0)


def test_overextended_opening_range_is_skipped_and_the_next_name_moves_up():
    rows = [_row("WIDE", 5.0, 101.5, 99.9), _row("OK1", 4.0, 100.5, 99.9), _row("OK2", 3.0, 100.5, 99.9)]
    res = F.rank_candidates(rows, {**V2, "top_n": 2})
    assert [r.symbol for r in res.picks] == ["OK1", "OK2"]
    assert "overextended" in rows[0].reason and not rows[0].picked
    assert [r.symbol for r in F.rank_candidates(rows, {**CFG, "top_n": 2}).picks] == ["WIDE", "OK1"]   # section 3


def test_scan_row_tags_gap_and_range():
    r = _row("A", 3.0, 100.5, 99.9)
    assert r.gap_pct == pytest.approx(2.04, abs=0.01) and r.or_atr == pytest.approx(0.3)
    assert r.to_dict()["gap_pct"] == r.gap_pct


def _daily(n=25, px=100.0, rng=2.0, vol=2_000_000):
    d0 = date(2026, 8, 3)
    return [{"d": d0 + timedelta(days=i), "o": px, "h": px + rng / 2, "l": px - rng / 2, "c": px, "v": vol} for i in range(n)]


def test_broad_universe_ranks_every_name_by_dollar_volume_with_a_cap_and_keeps_extras():
    u = {**V2["universe"], "min_avg_volume": 1_000_000, "min_dollar_vol_20d": 25_000_000, "extra": ["NVDA"]}
    cfg = {**V2, "universe": u}
    daily = {"BIG": _daily(vol=9e6), "MID": _daily(vol=5e6), "SMALL": _daily(vol=3e6), "THIN": _daily(vol=500_000),
             "NVDA": _daily(vol=1.5e6)}
    uni = F.universe(daily, set(), cfg, broad=True, cap=2)
    assert set(uni) == {"BIG", "MID", "NVDA"}                 # top 2 by dollar volume + the extra; THIN fails 1M
    assert set(F.universe(daily, set(), cfg, broad=True, cap=None)) == {"BIG", "MID", "SMALL", "NVDA"}


# ------------------------------------------------------------------ host
def test_published_stop_is_a_shadow_the_trade_keeps_its_or_low_stop():
    eng, data, host, p = entered()
    assert p.stop == pytest.approx(99.9) and p.shadow_stop == pytest.approx(p.entry - 0.2)
    quote(data, "NVDA", p.shadow_stop - 0.01, p.shadow_stop, et(9, 40))      # below the shadow, above the OR low
    at(host, eng, et(9, 40))
    assert host.book.open and p.shadow_hit
    quote(data, "NVDA", 101.0, 101.01, et(15, 55))
    at(host, eng, et(15, 55))
    c = host.book.closed[0]
    assert c.pnl > 0 and c.shadow_pnl == pytest.approx((c.shadow_stop - c.entry) * c.qty, abs=0.01)
    row = host.fj.trades()[0]
    assert row["shadow_hit"] == 1 and row["shadow_pnl"] == pytest.approx(c.shadow_pnl)
    assert json.loads(row["tags"])["or_atr"] == pytest.approx(0.3)
    ps = build_report(host.fj)["published_stop"]
    assert ps["trades"] == 1 and ps["shadow_stopped"] == 1 and ps["pnl"] > 0 > ps["shadow_pnl"]
    assert "published 0.10 x ATR stop" in format_report(build_report(host.fj))


def test_entry_records_observe_only_tags():
    eng, data, host, p = entered()
    assert set(p.tags) >= {"gap_pct", "or_atr", "rvol5", "min_after_scan", "spy_vs_vwap_bp", "catalyst"}
    assert p.tags["rvol5"] == pytest.approx(3.0) and p.tags["min_after_scan"] == pytest.approx(1.4, abs=0.1)


class BroadData(FakeData):
    """FakeData with a broad US universe source."""

    def __init__(self, day, broad):
        super().__init__(day)
        self.broad = broad

    async def us_daily(self, day):
        self.calls.append("us_daily")
        return {s: [b for b in self.daily[s] if b["d"] < day] for s in self.broad if s in self.daily}


def test_broad_universe_is_used_when_available():
    data = BroadData(DAY, broad={"NVDA", "XYZ"})
    data.add("XYZ", o=50.0, c=50.3, or_high=50.4, or_low=49.9, vol5=5000, hist_vol=1000, atr=1.0, px=50.0, sp=False)
    eng, data, host = make(("NVDA",), data=data)
    run(host.start())
    at(host, eng, et(8, 30, 1))
    assert "us_daily" in data.calls and "sp500" not in data.calls
    assert set(host.uni) == {"NVDA", "XYZ"}                   # XYZ is not in the S&P list
    at(host, eng, et(9, 35, 5))
    assert set(host.armed) == {"NVDA", "XYZ"}


def test_broad_universe_missing_falls_back_to_the_sp_list():
    data = BroadData(DAY, broad=set())
    eng, data, host = make(("NVDA",), data=data)
    run(host.start())
    at(host, eng, et(8, 30, 1))
    assert "sp500" in data.calls and set(host.uni) == {"NVDA"}
    assert any("S&P universe" in e["msg"] for e in eng.bus.of("log"))


def test_journal_from_before_v2_gains_the_new_columns():
    import sqlite3
    from agentdesk.books.f_journal import FJournal
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE f_trades (id TEXT PRIMARY KEY, session TEXT, mode TEXT, symbol TEXT, qty INTEGER, "
               "entry REAL, stop REAL, atr REAL, or_high REAL, rvol5 REAL, rank INTEGER, ai INTEGER, opened_ts REAL, "
               "status TEXT, closed_ts REAL, exit_px REAL, exit_reason TEXT, pnl REAL, risk REAL, r REAL, news TEXT, fills TEXT)")
    FJournal(db)
    cols = {r[1] for r in db.execute("PRAGMA table_info(f_trades)")}
    assert {"shadow_stop", "shadow_hit", "shadow_pnl", "tags"} <= cols


# ------------------------------------------------------------------ data sources
NASDAQ = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust|G|N|N|100|Y|N
ZXZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N
ABCDW|ABCD Corp - Warrant|G|N|N|100|N|N
TSM|Taiwan Semiconductor Manufacturing Company Ltd. American Depositary Shares|Q|N|N|100|N|N
File Creation Time: 0929202611:00|||||||"""
OTHER = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
BRK.B|Berkshire Hathaway Inc. Class B|N|BRK.B|N|100|N|BRK.B
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
JPM$C|JPMorgan Chase Preferred|N|JPMpC|N|100|N|JPM-C
File Creation Time: 0929202611:00|||||||"""


def test_symbol_directory_keeps_common_stocks_and_adrs_only():
    assert FD.parse_symbol_dir(NASDAQ) == {"AAPL", "TSM"}
    assert FD.parse_symbol_dir(OTHER) == {"BRK.B"}
    assert FD.parse_symbol_dir("") == set()


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeHTTP:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def get(self, url, params=None):
        self.calls.append(dict(params))
        return self.replies.pop(0)


def test_alpaca_bars_follows_pages_and_retries_a_throttle(monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(FD.asyncio, "sleep", no_sleep)
    b = lambda t: {"t": t, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}
    http = FakeHTTP([FakeResp(429, {}),
                     FakeResp(200, {"bars": {"AAA": [b("2026-09-28T04:00:00Z")]}, "next_page_token": "p2"}),
                     FakeResp(200, {"bars": {"AAA": [b("2026-09-29T04:00:00Z")], "BBB": [b("2026-09-29T04:00:00Z")]},
                                    "next_page_token": None})])
    out = run(FD.alpaca_bars(["BBB", "AAA"], "1Day", "s", "e", http=http))
    assert len(out["AAA"]) == 2 and len(out["BBB"]) == 1
    assert http.calls[-1]["page_token"] == "p2" and http.calls[0]["feed"] == "sip"


def test_rvol_base_caches_by_source_so_volumes_never_mix(tmp_path):
    class Stub(FD.RobinhoodEquityData):
        async def minute_bars(self, symbols, day, start, end):
            return {s: [{"t": 570, "o": 1, "h": 1, "l": 1, "c": 1, "v": 7}] for s in symbols}
    rh_side = Stub(None, {"cache_dir": str(tmp_path)})
    sip_side = Stub(None, {"cache_dir": str(tmp_path), "bars_source": "alpaca_sip"})
    d = date(2026, 9, 28)
    run(rh_side.or_volumes(["AAA"], [d]))
    run(sip_side.or_volumes(["AAA"], [d]))
    assert (tmp_path / "or" / f"{d}.json").exists() and (tmp_path / "or_sip" / f"{d}.json").exists()


def test_us_daily_without_alpaca_keys_returns_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    d = FD.RobinhoodEquityData(None, {"cache_dir": str(tmp_path), "universe": {"min_price": 10}})

    async def syms():
        return ["AAPL", "MSFT"]
    d.us_symbols = syms
    assert run(d.us_daily(DAY)) == {}


def test_sim_data_has_a_broad_universe_source():
    class Feed:
        def now(self):
            return F.at_et(DAY, F.time(9, 0))
    sim = FD.SimEquityData(Feed())
    daily = run(sim.us_daily(DAY))
    assert len(daily) == len(FD.SIM_SP) + len(FD.SIM_OTHER)
