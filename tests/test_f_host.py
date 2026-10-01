"""Book F in the engine: FHost scan, entries, stops, exits, limits and safety (BOOK_F_HANDOFF sections 3, 4, 6)."""
from __future__ import annotations

import asyncio
import copy
from datetime import date, time

import pytest

from books_fakes import FakeEngine, FakeQuotes
from f_fakes import FakeData, FakeRH
from agentdesk.books import f_stocks_in_play as F
from agentdesk.books.f_host import FHost, build_f
from agentdesk.books.f_journal import FJournal
from agentdesk.config import load_config

DAY = date(2026, 10, 1)            # Thursday
BASE = load_config()


def run(c):
    return asyncio.run(c)


def et(h, m, s=0, day=DAY):
    return F.at_et(day, time(h, m, s))


def make(names=("NVDA",), cfg_patch=None, data=None, news=None, reds=()):
    cfg = copy.deepcopy(BASE)
    cfg["books"]["F1_stocks_in_play"].update(cfg_patch or {})
    eng = FakeEngine(FakeQuotes(), cfg)
    data = data or FakeData(DAY)
    for i, s in enumerate(names):
        data.add(s, o=100.0, c=100.4, or_high=100.5, or_low=99.9, vol5=3000 + 100 * i, hist_vol=1000, atr=2.0)
    for s in reds:
        data.add(s, o=100.4, c=100.0, or_high=100.5, or_low=99.9, vol5=4000, hist_vol=1000, atr=2.0)
    host = FHost(eng, cfg, data, news=news)
    return eng, data, host


def at(host, eng, ts):
    eng.feed.t = ts
    run(host.on_second(ts))


def scanned(names=("NVDA",), **kw):
    eng, data, host = make(names, **kw)
    run(host.start())
    at(host, eng, et(8, 30, 1))           # 07:30 CT: universe and RVOL history
    at(host, eng, et(9, 35, 5))           # the scan
    return eng, data, host


def quote(data, sym, bid, ask, ts, last=None):
    data.set_quote(sym, bid, ask, ts, last)


# ------------------------------------------------------------------ scan
def test_scan_arms_green_picks_logs_every_row_and_shadows_red():
    eng, data, host = scanned(("NVDA", "MU"), reds=("AMD",))
    assert set(host.armed) == {"NVDA", "MU"}
    rows = host.fj.scans(str(DAY))
    assert {r["symbol"] for r in rows} == {"NVDA", "MU", "AMD"}
    assert set(host.shadows) == {"AMD"}
    assert eng.bus.of("f_scan")


def test_macro_event_between_0935_and_1030_skips_the_scan():
    eng, data, host = make()
    eng.risk.add_blackout(et(10, 0), "CPI (test)")
    run(host.start())
    at(host, eng, et(8, 30, 1))
    at(host, eng, et(9, 35, 5))
    assert host.armed == {} and "CPI" in host.book.skips[-1]["why"]


def test_macro_event_later_in_the_day_does_not_skip():
    eng, data, host = make()
    eng.risk.add_blackout(et(13, 0), "Fed speaker (test)")
    run(host.start())
    at(host, eng, et(8, 30, 1))
    at(host, eng, et(9, 35, 5))
    assert set(host.armed) == {"NVDA"}


# ------------------------------------------------------------------ entries
def test_entry_only_above_the_or_high():
    eng, data, host = scanned()
    quote(data, "NVDA", 100.49, 100.50, et(9, 36), last=100.50)
    at(host, eng, et(9, 36))
    assert not host.book.open
    quote(data, "NVDA", 100.51, 100.52, et(9, 36, 2), last=100.52)
    at(host, eng, et(9, 36, 2))
    [p] = host.book.open
    assert p.entry == pytest.approx(100.52 * 1.0001, abs=1e-4)
    assert p.qty == 9                                   # $1,000 / 100.55 limit (risk cap would allow 125)
    assert p.stop == pytest.approx(99.9, abs=1e-4)             # F1 v2: the OR low (0.32 x ATR below the fill)
    assert p.shadow_stop == pytest.approx(p.entry - 0.2, abs=1e-4)   # the published 0.10 x ATR stop, shadow only
    assert "NVDA" not in host.armed


def test_one_entry_per_name_per_day():
    eng, data, host = scanned()
    quote(data, "NVDA", 100.51, 100.52, et(9, 36), last=100.52)
    at(host, eng, et(9, 36))
    quote(data, "NVDA", 99.85, 99.86, et(9, 37), last=99.85)             # stopped out below the OR low
    at(host, eng, et(9, 37))
    assert not host.book.open and host.book.trades == 1
    quote(data, "NVDA", 100.8, 100.81, et(9, 38), last=100.8)            # breaks out again
    at(host, eng, et(9, 38))
    assert not host.book.open and host.book.trades == 1


def test_armed_names_expire_at_1030_et():
    eng, data, host = scanned()
    quote(data, "NVDA", 100.51, 100.52, et(10, 30), last=100.52)
    at(host, eng, et(10, 30))
    assert not host.book.open and host.armed == {}
    assert host.status["NVDA"] == "expired"


def test_stale_or_missing_quotes_fail_closed():
    eng, data, host = scanned()
    at(host, eng, et(9, 36))                                              # no quote at all
    quote(data, "NVDA", 100.51, 100.52, et(9, 36, 4), last=100.52)       # 6 s old at 09:36:10
    at(host, eng, et(9, 36, 10))
    assert not host.book.open and "NVDA" in host.armed


def test_the_account_open_risk_cap_blocks_an_entry():
    eng, data, host = scanned()
    host.account.c["open_risk_cap"] = 1.0
    quote(data, "NVDA", 100.51, 100.52, et(9, 36), last=100.52)
    at(host, eng, et(9, 36))
    assert not host.book.open and "open-risk cap" in host.book.skips[-1]["why"]


def test_at_most_max_positions_open():
    names = ("NVDA", "MU", "AMD")
    eng, data, host = scanned(names, cfg_patch={"max_positions": 2})
    for s in names:
        quote(data, s, 100.51, 100.52, et(9, 36), last=100.52)
    at(host, eng, et(9, 36))
    assert len(host.book.open) == 2


# ------------------------------------------------------------------ stops and exits
def entered(**kw):
    eng, data, host = scanned(**kw)
    quote(data, "NVDA", 100.51, 100.52, et(9, 36, 30), last=100.52)
    at(host, eng, et(9, 36, 30))
    return eng, data, host, host.book.open[0]


def test_quote_stop_fills_at_min_of_stop_and_bid():
    eng, data, host, p = entered()
    quote(data, "NVDA", p.stop - 0.05, p.stop - 0.04, et(9, 40))
    at(host, eng, et(9, 40))
    assert not host.book.open
    c = host.book.closed[0]
    assert c.exit_reason == "stop" and c.exit_px == pytest.approx((p.stop - 0.05) * 0.9999, abs=1e-4)
    row = host.fj.trades()[0]
    assert row["r"] == pytest.approx(c.r_multiple) and row["r"] < -1


def test_bar_gapping_through_the_stop_fills_at_the_bar_open():
    eng, data, host, p = entered()
    quote(data, "NVDA", p.entry, p.entry + 0.01, et(9, 38))
    data.add_bar("NVDA", 577, o=p.stop - 0.3, h=p.stop - 0.1, l=p.stop - 0.4, c=p.stop - 0.2)     # 09:37 bar
    at(host, eng, et(9, 38))
    c = host.book.closed[0]
    assert c.exit_reason == "stop (bar)" and c.exit_px == pytest.approx((p.stop - 0.3) * 0.9999, abs=1e-4)


def test_entry_and_stop_in_the_same_bar_assume_the_stop_hit():
    eng, data, host, p = entered()                      # entered 09:36:30
    quote(data, "NVDA", p.entry, p.entry + 0.01, et(9, 37))
    data.add_bar("NVDA", 576, o=100.4, h=100.6, l=p.stop - 0.01, c=100.5)                         # the 09:36 bar
    at(host, eng, et(9, 37))
    c = host.book.closed[0]
    assert c.exit_reason == "stop (bar)" and c.exit_px == pytest.approx(p.stop * 0.9999, abs=1e-4)


def test_exit_at_1555_et():
    eng, data, host, p = entered()
    quote(data, "NVDA", 101.0, 101.01, et(15, 54, 59))
    at(host, eng, et(15, 54, 59))
    assert host.book.open
    quote(data, "NVDA", 101.0, 101.01, et(15, 55))
    at(host, eng, et(15, 55))
    assert host.book.closed[0].exit_reason == "exit 15:55 ET"


def test_exit_at_1255_et_on_half_days():
    eng, data, host, p = entered()
    eng.cfg["calendar"]["early_close"] = [DAY]
    quote(data, "NVDA", 101.0, 101.01, et(12, 55))
    at(host, eng, et(12, 55))
    assert host.book.closed[0].exit_reason == "exit 12:55 ET (half day)"


def test_restart_after_the_close_with_an_open_position_halts_f():
    eng, data, host, p = entered()
    db = host.fj.db
    eng2, data2, host2 = make()
    host2.fj = FJournal(db)
    eng2.feed.t = et(16, 30)
    run(host2.start())
    assert host2.book.halted and "NVDA" in host2.book.halt_reason
    assert not eng2.risk.st.halted                      # F only


def test_restart_overnight_halts_f():
    eng, data, host, p = entered()
    eng2, data2, host2 = make(data=FakeData(date(2026, 10, 2)))
    host2.fj = FJournal(host.fj.db)
    eng2.feed.t = F.at_et(date(2026, 10, 2), time(8, 0))
    run(host2.start())
    assert host2.book.halted


def test_restart_during_the_session_restores_the_position():
    eng, data, host, p = entered()
    eng2, data2, host2 = make()
    host2.fj = FJournal(host.fj.db)
    eng2.feed.t = et(11, 0)
    run(host2.start())
    assert not host2.book.halted and [q.symbol for q in host2.book.open] == ["NVDA"]
    assert host2.book.open[0].stop == pytest.approx(p.stop)


# ------------------------------------------------------------------ limits and kill
def test_daily_loss_halts_f_only_and_flattens_it():
    names = ("NVDA", "MU")
    eng, data, host = scanned(names, cfg_patch={"daily_loss": 1})
    for s in names:
        quote(data, s, 100.51, 100.52, et(9, 36), last=100.52)
    at(host, eng, et(9, 36))
    assert len(host.book.open) == 2
    quote(data, "NVDA", 99.85, 99.86, et(9, 40))                      # NVDA stops: about -$6 <= -$1
    quote(data, "MU", 100.6, 100.61, et(9, 40))
    at(host, eng, et(9, 40))
    assert host.book.halted and "daily loss" in host.book.halt_reason
    assert not host.book.open                                          # MU flattened too
    assert not eng.risk.st.halted and not host.account.halted          # A-E untouched


def test_kill_switch_flattens_f():
    eng, data, host, p = entered()
    eng.risk.halt("KILL switch", flatten=True)
    quote(data, "NVDA", 100.6, 100.61, et(9, 40))
    eng.feed.t = et(9, 40)
    run(host.kill(et(9, 40)))
    assert not host.book.open and host.book.closed[0].exit_reason.startswith("KILL")


def test_three_errors_in_a_row_halt_f_only():
    eng, data, host, p = entered()
    data.fail_quotes = True
    for s in (1, 2, 3):
        at(host, eng, et(9, 40, s))
    assert host.book.halted and not eng.risk.st.halted


# ------------------------------------------------------------------ news tag is observe-only
class StaticNews:
    def __init__(self, tag):
        self.tag = tag

    def start(self, now, picks, runners, done):
        done({r.symbol: dict(self.tag) for r in picks + runners}, False)


def test_news_tag_never_changes_an_order():
    def trade(tag):
        eng, data, host = make(("NVDA", "MU"), news=StaticNews(tag))
        run(host.start())
        at(host, eng, et(8, 30, 1))
        at(host, eng, et(9, 35, 5))
        for s in ("NVDA", "MU"):
            quote(data, s, 100.51, 100.52, et(9, 36), last=100.52)
        at(host, eng, et(9, 36))
        return [(p.symbol, p.qty, round(p.entry, 4), round(p.stop, 4)) for p in host.book.open], host
    fully, h1 = trade({"catalyst": "earnings", "priced_in": "fully", "confidence": 0.95})
    none, h2 = trade({"catalyst": "none_found", "priced_in": "early", "confidence": 0.1})
    assert fully == none and len(fully) == 2
    assert h1.book.open[0].news["priced_in"] == "fully"
    assert h1.fj.scans(str(DAY))[0]["news"]


# ------------------------------------------------------------------ build, modes and proposals
def test_paper_mode_never_calls_robinhood_order_tools():
    cfg = copy.deepcopy(BASE)
    eng = FakeEngine(FakeQuotes(), cfg)
    rh = FakeRH()
    host = build_f(eng, cfg, rh=rh, mode="paper", provider="alpaca", feed=eng.feed, data=FakeData(DAY))
    assert type(host.broker).__name__ == "PaperEquityBroker"
    host.data.add("NVDA", o=100.0, c=100.4, or_high=100.5, or_low=99.9, vol5=3000, hist_vol=1000, atr=2.0)
    run(host.start())
    at(host, eng, et(8, 30, 1))
    at(host, eng, et(9, 35, 5))
    quote(host.data, "NVDA", 100.51, 100.52, et(9, 36), last=100.52)
    at(host, eng, et(9, 36))
    assert host.book.open
    assert not [c for c in rh.calls if "order" in c[0]]


def test_shadow_and_live_modes_only_review_equity_orders():
    for mode in ("shadow", "live"):
        cfg = copy.deepcopy(BASE)
        eng = FakeEngine(FakeQuotes(), cfg)
        rh = FakeRH()
        host = build_f(eng, cfg, rh=rh, mode=mode, provider="alpaca", feed=eng.feed, data=FakeData(DAY))
        assert host.broker.live is False                 # F is paper_only: never a real order
        host.data.add("NVDA", o=100.0, c=100.4, or_high=100.5, or_low=99.9, vol5=3000, hist_vol=1000, atr=2.0)
        run(host.start())
        at(host, eng, et(8, 30, 1))
        at(host, eng, et(9, 35, 5))
        quote(host.data, "NVDA", 100.51, 100.52, et(9, 36), last=100.52)
        at(host, eng, et(9, 36))
        tools = [c[0] for c in rh.calls]
        assert "review_equity_order" in tools and "place_equity_order" not in tools


def test_unexpected_equity_position_halts_every_book_in_shadow():
    cfg = copy.deepcopy(BASE)
    eng = FakeEngine(FakeQuotes(), cfg)
    rh = FakeRH(positions=[{"symbol": "AAPL", "quantity": "3"}])
    host = build_f(eng, cfg, rh=rh, mode="shadow", provider="alpaca", feed=eng.feed, data=FakeData(DAY))
    eng.feed.t = et(8, 0)
    run(host.start())
    assert host.account.halted and eng.risk.st.halted and "AAPL" in eng.risk.st.halt_reason


def test_unexpected_equity_halt_is_rechecked_at_restart_not_restored(tmp_path):    # 2026-10-01 sweep
    from agentdesk.risk import RiskManager, RiskStore
    cfg = copy.deepcopy(BASE)
    eng = FakeEngine(FakeQuotes(), cfg)
    eng.risk = RiskManager(cfg, RiskStore(tmp_path / "risk.json"))
    eng.risk.restore(str(DAY))
    rh = FakeRH(positions=[{"symbol": "AAPL", "quantity": "3"}])
    host = build_f(eng, cfg, rh=rh, mode="shadow", provider="alpaca", feed=eng.feed, data=FakeData(DAY))
    eng.feed.t = et(8, 0)
    run(host.start())
    assert eng.risk.st.halted
    again = RiskManager(cfg, RiskStore(tmp_path / "risk.json"))      # Evan sold the shares and restarted
    again.restore(str(DAY))
    assert not again.st.halted


def test_build_refuses_f_without_paper_only_and_skips_when_disabled():
    cfg = copy.deepcopy(BASE)
    eng = FakeEngine(FakeQuotes(), cfg)
    cfg["books"]["F1_stocks_in_play"]["paper_only"] = False
    with pytest.raises(SystemExit):
        build_f(eng, cfg, rh=None, mode="sim", provider="sim", feed=eng.feed, data=FakeData(DAY))
    cfg["books"]["F1_stocks_in_play"].update(paper_only=True, enabled=False)
    assert build_f(eng, cfg, rh=None, mode="sim", provider="sim", feed=eng.feed, data=FakeData(DAY)) is None


def test_f2_stays_disabled_and_f_is_paper_only_in_config():
    b = BASE["books"]
    assert b["F0_momentum_hold"]["enabled"] is False
    assert b["F1_stocks_in_play"]["paper_only"] is True
    assert b["F1_stocks_in_play"]["shorts"] == "log_only" and b["F1_stocks_in_play"]["news_tag"] == "observe"


def test_crew_proposals_cannot_touch_f_risk_fields():
    from agentdesk.proposals import TWEAKS, ProposalBook
    assert not any(k.startswith("books.") for k in TWEAKS)
    cfg = copy.deepcopy(BASE)
    pb = ProposalBook(cfg, None)
    for scope in ("trade", "day", "standing"):
        for key, val in (("books.F1_stocks_in_play.risk_per_trade", 50), ("books.F1_stocks_in_play.max_notional", 5000),
                         ("books.F1_stocks_in_play.daily_loss", 500), ("books.F1_stocks_in_play.max_positions", 10),
                         ("books.F0_momentum_hold.enabled", True), ("books.F1_stocks_in_play.paper_only", False)):
            item = pb.submit("quant", {"scope": scope, "title": f"{scope} {key}", "params": {key: val}}, 0.0)
            assert item["status"].startswith("rejected") and not item["params"]
    assert cfg["books"] == BASE["books"]


def test_open_risk_is_shared_between_f_and_the_other_books():
    from agentdesk.books.group import HostGroup
    from agentdesk.books.host import BookHost
    eng, data, host, p = entered()
    bh = BookHost(eng, eng.cfg, books=[])
    g = HostGroup(bh, host)
    assert host.account is bh.account
    assert bh.open_risk() == pytest.approx(p.risk)
    assert g.positions() == [p]


def test_f1_opens_and_closes_refresh_the_book_strip():
    from agentdesk.books.group import HostGroup
    from agentdesk.books.host import BookHost
    eng, data, host = scanned()
    HostGroup(BookHost(eng, eng.cfg, books=[]), host)
    quote(data, "NVDA", 100.51, 100.52, et(9, 36, 30), last=100.52)
    at(host, eng, et(9, 36, 30))
    p = host.book.open[0]
    quote(data, "NVDA", p.stop - 0.05, p.stop - 0.04, et(9, 40))
    at(host, eng, et(9, 40))
    strips = [s["books"] for s in eng.bus.of("books")]
    assert len(strips) == 2 and [b["book"] for b in strips[0]] == ["A", "F1"]
    assert strips[0][1]["trades"] == 1 and strips[1][1]["day_pnl"] < 0


class Boom:
    """A host whose hooks raise: stands in for an F failure outside FHost's own guard."""
    book = None

    async def on_second(self, now):
        raise RuntimeError("boom")

    async def run(self, coro, inline):
        try:
            await coro
        except RuntimeError:
            self.failed = True


def test_group_runs_each_host_under_its_own_guard():
    from agentdesk.books.group import HostGroup
    from agentdesk.books.host import BookHost
    eng, data, host = make()
    bh = BookHost(eng, eng.cfg, books=[])
    g = HostGroup(bh, host)
    g.hosts[1] = boom = Boom()                # F's slot raises; B/C/D's error count must not move
    run(g.run(g.on_second(et(9, 40)), True))
    assert boom.failed and bh._errors == 0 and not bh.account.halted


def test_fhost_run_counts_its_own_errors_and_runs_in_the_background():
    eng, data, host = make()

    async def fail():
        raise RuntimeError("boom")

    run(host.run(fail(), True))
    assert host.book.errors == 1

    async def bg():
        seen = []

        async def slow():
            await asyncio.sleep(0)
            seen.append(1)
        await host.run(slow(), False)
        assert not seen                       # returned before the hook ran
        await asyncio.gather(*host._tasks)
        return seen
    assert run(bg()) == [1]


def test_group_with_f_alone_runs_hooks():
    from agentdesk.books.group import HostGroup
    eng, data, host = make()
    g = HostGroup.of(None, host)
    eng.feed.t = et(8, 30, 1)
    run(g.start())
    run(g.run(g.on_second(et(8, 30, 1)), True))
    assert host.prepared
