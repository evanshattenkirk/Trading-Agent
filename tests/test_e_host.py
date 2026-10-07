"""EHost: entries from the earnings screen, paper fills, exits, overnight holds and restarts."""
import asyncio
import json
import sys
from datetime import date, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from books_fakes import FakeEngine, FakeQuotes
from e_fakes import FakeChains

from agentdesk.books.e_host import EHost
from agentdesk.books.e_journal import EJournal
from agentdesk.clock import at_ct
from agentdesk.config import load_config

MON, TUE, THU = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 8)
FRI1, FRI2, NEXT_MON = date(2026, 10, 9), date(2026, 10, 16), date(2026, 10, 12)
AMD_EV = {"symbol": "AMD", "date": THU, "timing": "pm", "verified": True}          # Monday is T-3
XOM_EV = {"symbol": "XOM", "date": FRI2, "timing": "am", "verified": True}         # Monday is T-9


def at(d, h, m, s=0):
    return at_ct(d, time(h, m, s))


class Vix:
    def __init__(self, v):
        self.v = v

    async def prior_close(self, day):
        return self.v


def market(ch):
    ch.px.update(AMD=100.2, XOM=115.3, NVDA=180.4)
    for exp in (FRI1, FRI2):
        ch.chain("AMD", exp, [95, 100, 105])
        ch.chain("XOM", exp, [110, 115, 120])
        ch.chain("NVDA", exp, [175, 180, 185])
    ch.set("AMD", FRI1, 100, "call", 2.40, 2.50, 0.62)
    ch.set("AMD", FRI1, 100, "put", 2.20, 2.30, 0.60)
    ch.set("XOM", FRI1, 115, "call", 1.00, 1.04, 0.22)
    ch.set("XOM", FRI2, 115, "call", 2.50, 2.60, 0.30)
    ch.set("XOM", FRI2, 115, "put", 2.40, 2.50, 0.31)


def make(cal=(AMD_EV, XOM_EV), vix=18.0, cfg=None, engine=None, chains=None, **over):
    cfg = cfg or load_config()
    cfg["books"]["E_earnings_iv"] = {**cfg["books"]["E_earnings_iv"], **over}
    eng = engine or FakeEngine(FakeQuotes(), cfg)
    ch = chains or FakeChains()
    if chains is None:
        market(ch)
    rows = list(cal)

    async def calendar(today):
        return list(rows)
    h = EHost(eng, cfg, ch, vix=Vix(vix), calendar_fn=calendar)
    h.cal_rows = rows
    return h, eng, ch


def tick(h, ch, t):
    ch.now = t
    h.e.feed.t = t
    asyncio.run(h.on_second(t))


def by_symbol(h):
    return {p.meta["symbol"]: p for p in h.book.open}


def test_entries_wait_for_1445_then_open_e1_and_e2_at_paper_fills():
    h, eng, ch = make()
    tick(h, ch, at(MON, 14, 44))
    assert h.book.open == []
    tick(h, ch, at(MON, 14, 45))
    got = by_symbol(h)
    amd, xom = got["AMD"], got["XOM"]
    assert amd.setup == "E1 STRADDLE" and amd.entry == 4.72 and amd.qty == 1 and not amd.credit
    assert [c.expiry for c in amd.contracts] == ["2026-10-09"] * 2 and amd.max_loss == 472.0
    assert xom.setup == "E2 CALENDAR" and xom.entry == 1.55 and [l.side for l in xom.legs] == ["sell", "buy"]
    assert xom.meta["short_expiry"] == "2026-10-09" and xom.meta["exit_day"] == "2026-10-15"
    assert amd.meta["exit_day"] == "2026-10-08" and amd.target == 5.66 and amd.stop == 3.30
    assert amd.fees == 0.08 and amd.watchdog_exempt and amd.overnight
    assert [d["outcome"] for d in h.ej.decisions()] == ["opened", "opened"]
    assert {p.id for p in h.ej.open_positions()[0]} == {amd.id, xom.id}
    assert "IV filter: 0/4 cycles" in amd.entry_reasons


def test_the_book_strip_shows_es_open_limit_not_a_default_trade_cap():          # review 2026-10-06
    h, eng, ch = make()
    assert h.book.to_dict()["max_trades"] == h.c["max_open"] == 3      # was Book's default of 1, which E never uses


def test_e_never_uses_the_engine_broker():
    h, eng, ch = make()

    async def boom(*a, **k):
        raise AssertionError("engine broker used")
    eng.broker.submit_combo = eng.broker.submit = boom
    tick(h, ch, at(MON, 14, 45))
    assert len(h.book.open) == 2 and h.broker is not eng.broker


def test_take_profit_and_stop():
    h, eng, ch = make()
    tick(h, ch, at(MON, 14, 45))
    ch.set("AMD", FRI1, 100, "call", 3.35, 3.45, 0.7)          # straddle mid 5.65: not yet +20%
    tick(h, ch, at(TUE, 9, 0))
    assert "AMD" in by_symbol(h)
    ch.set("AMD", FRI1, 100, "call", 3.40, 3.50, 0.7)          # mid 5.70 >= 5.664
    tick(h, ch, at(TUE, 9, 0, 20))
    assert "AMD" not in by_symbol(h)
    tr = [t for t in eng.journal.trades() if t["book"] == "E"]
    assert tr[0]["exit_reason"] == "take profit +20%" and tr[0]["pnl"] > 0
    ch.set("XOM", FRI2, 115, "call", 2.08, 2.12, 0.3)          # calendar mid 1.08 <= 1.085
    tick(h, ch, at(TUE, 9, 1))
    assert h.book.open == [] and "stop -30%" in eng.journal.trades()[0]["exit_reason"]


def test_quotes_are_polled_every_poll_sec():
    h, eng, ch = make()
    tick(h, ch, at(MON, 14, 45))
    n = ch.calls.count("quotes")
    for s in range(1, 15):
        tick(h, ch, at(TUE, 9, 0, s))
    assert ch.calls.count("quotes") == n + 1


def test_exit_day_close_for_the_pm_straddle_and_short_expiry_for_the_calendar():
    h, eng, ch = make()
    tick(h, ch, at(MON, 14, 45))
    tick(h, ch, at(THU, 14, 44))
    assert set(by_symbol(h)) == {"AMD", "XOM"}
    tick(h, ch, at(THU, 14, 45))
    assert set(by_symbol(h)) == {"XOM"}
    assert "T-0" in eng.journal.trades()[0]["exit_reason"]
    tick(h, ch, at(FRI1, 14, 14))
    assert set(by_symbol(h)) == {"XOM"}
    tick(h, ch, at(FRI1, 14, 15))
    assert h.book.open == [] and "short leg expires today" in eng.journal.trades()[0]["exit_reason"]


def test_shutdown_keeps_e_positions_but_flatten_and_kill_sell_them():
    h, eng, ch = make()
    tick(h, ch, at(MON, 14, 45))
    asyncio.run(h.flatten("shutdown", at(MON, 15, 9)))
    assert len(h.book.open) == 2
    asyncio.run(h.flatten("manual flatten", at(TUE, 9, 0)))
    assert h.book.open == []
    h2, eng2, ch2 = make()
    tick(h2, ch2, at(MON, 14, 45))
    asyncio.run(h2.kill(at(MON, 14, 50)))
    assert h2.book.open == [] and h2.account.halted
    tick(h2, ch2, at(TUE, 14, 45))
    assert h2.book.open == []


def test_restart_restores_positions_before_any_new_entry():
    h, eng, ch = make()
    tick(h, ch, at(MON, 14, 45))
    eng2 = FakeEngine(FakeQuotes(), eng.cfg)
    eng2.journal = eng.journal
    eng2.feed.t = at(TUE, 8, 10)
    h2, _, _ = make(engine=eng2, chains=ch)
    asyncio.run(h2.start())
    assert set(by_symbol(h2)) == {"AMD", "XOM"} and h2.open_risk() == 627.0
    tick(h2, ch, at(TUE, 14, 45))
    assert len(h2.book.open) == 2                         # nothing traded twice


def test_restart_after_the_report_closes_and_flags_the_trade():
    h, eng, ch = make()
    tick(h, ch, at(MON, 14, 45))
    eng2 = FakeEngine(FakeQuotes(), eng.cfg)
    eng2.journal = eng.journal
    eng2.feed.t = at(FRI1, 8, 10)
    h2, _, _ = make(engine=eng2, chains=ch)
    asyncio.run(h2.start())
    tick(h2, ch, at(FRI1, 8, 10))
    assert "AMD" in by_symbol(h2)                         # nothing trades before the open
    tick(h2, ch, at(FRI1, 8, 30))
    assert "AMD" not in by_symbol(h2)
    t = [x for x in eng.journal.trades() if "AMD" in x["contract"]][0]
    assert "held through" in t["exit_reason"]
    assert any(d.get("level") == "error" and "held through" in d.get("msg", "") for d in eng2.bus.of("log"))


def test_expired_short_leg_closes_at_last_mark():                               # review focus 2
    h, eng, ch = make(cal=(XOM_EV,))
    tick(h, ch, at(MON, 14, 45))
    ch.drop("XOM", FRI1, 115, "call")                     # expired: no quote any more
    tick(h, ch, at(NEXT_MON, 8, 30))
    assert h.book.open == []
    t = eng.journal.trades()[0]
    assert "expired while the engine was down" in t["exit_reason"] and "last mark" in t["exit_reason"]


def test_report_moved_earlier_moves_the_exit():                                 # review focus 3
    h, eng, ch = make()
    tick(h, ch, at(MON, 14, 45))
    h.cal_rows[:] = [{**AMD_EV, "date": TUE}, dict(AMD_EV, date=date(2027, 1, 27)), XOM_EV]
    tick(h, ch, at(TUE, 8, 31))
    assert by_symbol(h)["AMD"].meta["exit_day"] == "2026-10-06"
    tick(h, ch, at(TUE, 14, 45))
    assert "AMD" not in by_symbol(h)


def test_limits_sector_and_max_open():
    nv = {"symbol": "NVDA", "date": THU, "timing": "pm", "verified": True}
    h, eng, ch = make(cal=(AMD_EV, nv))
    ch.set("NVDA", FRI1, 180, "call", 2.00, 2.08, 0.5)
    ch.set("NVDA", FRI1, 180, "put", 2.00, 2.08, 0.5)
    tick(h, ch, at(MON, 14, 45))
    assert set(by_symbol(h)) == {"AMD"}
    assert "tech sector already open" in [d for d in h.ej.decisions() if d["symbol"] == "NVDA"][0]["reason"]
    h2, _, ch2 = make(max_open=1)
    tick(h2, ch2, at(MON, 14, 45))
    assert len(h2.book.open) == 1 and "max 1" in h2.ej.decisions()[-1]["reason"]


def test_filters_vix_spread_cost_and_iv():
    h, _, ch = make(vix=31.0)
    tick(h, ch, at(MON, 14, 45))
    assert h.book.open == [] and all("VIX 31.0 > 30" in d["reason"] for d in h.ej.decisions())
    h, _, ch = make(cal=(AMD_EV,))
    ch.set("AMD", FRI1, 100, "put", 2.10, 2.40, 0.6)                 # 13% of mid
    tick(h, ch, at(MON, 14, 45))
    assert h.book.open == [] and "spread" in h.ej.decisions()[0]["reason"]
    h, _, ch = make(cal=(AMD_EV,))
    ch.set("AMD", FRI1, 100, "call", 3.00, 3.10, 0.6)                 # straddle 5.37: $539 > $500
    tick(h, ch, at(MON, 14, 45))
    assert h.book.open == [] and "> $500 max debit" in h.ej.decisions()[0]["reason"]
    h, eng, ch = make(cal=(AMD_EV,))
    eng.journal.record_iv([{"day": f"2026-0{m}-01", "ts": 0, "symbol": "AMD", "kind": "earn", "atm_iv": iv, "T": 3,
                            "earnings_date": f"2026-0{m}-04"} for m, iv in ((1, 0.4), (4, 0.45), (7, 0.5), (8, 0.55))])
    tick(h, ch, at(MON, 14, 45))
    assert h.book.open == [] and "percentile" in h.ej.decisions()[0]["reason"]


def test_missing_iv_trades_with_a_note():                                      # review focus 4
    h, _, ch = make(cal=(AMD_EV,))
    ch.set("AMD", FRI1, 100, "call", 2.40, 2.50, None)
    ch.set("AMD", FRI1, 100, "put", 2.20, 2.30, None)
    tick(h, ch, at(MON, 14, 45))
    assert "IV filter: no IV today" in h.book.open[0].entry_reasons


def test_e2_retries_inside_its_window_and_trades_once():
    h, _, ch = make(cal=(XOM_EV,))
    ch.set("XOM", FRI1, 115, "call", 0.90, 1.14, 0.22)                 # too wide on Monday (T-9)
    tick(h, ch, at(MON, 14, 45))
    assert h.book.open == []
    ch.set("XOM", FRI1, 115, "call", 1.00, 1.04, 0.22)
    tick(h, ch, at(TUE, 14, 45))                                        # T-8
    assert len(h.book.open) == 1
    asyncio.run(h.flatten("manual flatten", at(TUE, 14, 50)))
    tick(h, ch, at(date(2026, 10, 7), 14, 45))                          # T-7: outside the window anyway
    assert h.book.open == [] and [d["outcome"] for d in h.ej.decisions()] == ["skipped", "opened"]


def test_open_risk_cap_counts_other_books():
    h, _, ch = make(cal=(AMD_EV,))
    h.other_risk = lambda: h.account.c["open_risk_cap"] - 400.0            # the $472 straddle no longer fits
    tick(h, ch, at(MON, 14, 45))
    assert h.book.open == [] and "open-risk cap" in h.ej.decisions()[0]["reason"]


def test_paused_or_halted_blocks_entries():
    h, eng, ch = make(cal=(AMD_EV,))
    eng.risk.st.paused = True
    tick(h, ch, at(MON, 14, 45))
    assert h.book.open == [] and h.ej.decisions()[0]["reason"] == "paused"


def test_three_errors_in_a_row_halt_book_e():
    """A failure that blocks a due exit still counts (review 2026-10-06 M5): a take profit whose order keeps
    failing halts E after three polls."""
    h, eng, ch = make(cal=(AMD_EV,))
    tick(h, ch, at(MON, 14, 45))

    async def broken(*a, **k):
        raise RuntimeError("order path broke")
    h.exec.work = broken
    ch.set("AMD", FRI1, 100, "call", 3.40, 3.50, 0.7)          # take profit is due
    for s in (0, 15, 30):
        ch.now = eng.feed.t = at(TUE, 9, 0, s)
        asyncio.run(h.run(h.on_second(eng.feed.t), True))
    assert h.book.halted


def test_a_quote_outage_with_nothing_due_never_halts_book_e():                   # review 2026-10-06 M5
    h, eng, ch = make()
    tick(h, ch, at(MON, 14, 45))
    ch.fail.add("quotes")
    for s in (0, 15, 30, 45):
        ch.now = eng.feed.t = at(TUE, 9, 0, s)
        asyncio.run(h.run(h.on_second(eng.feed.t), True))
    assert ch.calls.count("quotes") >= 5                    # it kept asking, every poll_sec
    assert not h.book.halted and h.book.errors == 0 and len(h.book.open) == 2
    down = [d for d in eng.bus.of("log") if "option quotes unavailable" in d["msg"]]
    assert len(down) == 1 and down[0]["level"] == "warn"    # one line per outage, not one per poll
    ch.fail.discard("quotes")
    tick(h, ch, at(TUE, 9, 1))
    assert any("option quotes back" in d["msg"] for d in eng.bus.of("log"))


def test_flatten_after_a_failed_quote_read_closes_at_the_last_mark():            # review 2026-10-06 M5
    h, eng, ch = make()
    tick(h, ch, at(MON, 14, 45))
    marks = {p.id: p.mark for p in h.book.open}
    sent = []
    real = h.broker.submit_combo

    async def spy(*a, **k):
        sent.append(a)
        return await real(*a, **k)
    h.broker.submit_combo = spy
    ch.fail.add("quotes")                                   # the fresh read fails; quote() would still answer
    ch.now = at(TUE, 9, 0)
    asyncio.run(h.flatten("manual flatten", at(TUE, 9, 0)))
    assert h.book.open == [] and sent == []                 # never a paper fill at stale cached prices
    for t in eng.journal.trades():
        assert t["exit_reason"].startswith("manual flatten") and "quotes unavailable" in t["exit_reason"]
        assert json.loads(t["fills"])[-1]["px"] in marks.values()
    h2, eng2, ch2 = make()
    tick(h2, ch2, at(MON, 14, 45))
    ch2.now = at(TUE, 9, 0)                                 # fresh quotes: the normal paper fill
    asyncio.run(h2.kill(at(TUE, 9, 0)))
    assert h2.book.open == [] and all("last mark" not in t["exit_reason"] for t in eng2.journal.trades())
    h3, eng3, ch3 = make()
    tick(h3, ch3, at(MON, 14, 45))                          # quotes answer, but with yesterday's prices
    asyncio.run(h3.kill(at(TUE, 9, 0)))
    assert h3.book.open == [] and all("quotes unavailable" in t["exit_reason"] for t in eng3.journal.trades())


def test_one_names_data_error_does_not_cancel_the_other_entries():             # final review 1
    h, eng, ch = make()
    real = ch.expirations

    async def flaky(sym):
        if sym == "AMD":
            raise RuntimeError("get_option_chains AMD: no expiration_dates")
        return await real(sym)
    ch.expirations = flaky
    tick(h, ch, at(MON, 14, 45))
    assert set(by_symbol(h)) == {"XOM"}
    amd = [d for d in h.ej.decisions() if d["symbol"] == "AMD"][0]
    assert amd["outcome"] == "error" and "no expiration_dates" in amd["reason"]


def test_a_failed_calendar_read_retries_instead_of_losing_the_day():           # final review 1
    h, eng, ch = make(cal=(AMD_EV,))
    fail = [True]
    orig = h.calendar_fn

    async def cal(today):
        if fail[0]:
            return None                      # Robinhood calendar unavailable
        return await orig(today)
    h.calendar_fn = cal
    tick(h, ch, at(MON, 14, 45))
    assert h.book.open == []
    fail[0] = False
    tick(h, ch, at(MON, 14, 45, 30))         # inside the retry gap: nothing yet
    assert h.book.open == []
    tick(h, ch, at(MON, 14, 46))
    assert set(by_symbol(h)) == {"AMD"}


def test_forced_exit_with_failing_quotes_is_throttled_then_closes_at_last_mark():   # final review 3
    h, eng, ch = make(cal=(AMD_EV,))
    tick(h, ch, at(MON, 14, 45))
    ch.fail.add("quotes")
    n0 = ch.calls.count("quotes")
    for s in range(0, 45):
        tick(h, ch, at(THU, 14, 45, s))
    assert ch.calls.count("quotes") - n0 == 3            # one try per poll_sec, not one per second
    assert h.book.open == [] and not h.book.halted
    t = eng.journal.trades()[0]
    assert "T-0" in t["exit_reason"] and "last mark" in t["exit_reason"]


def test_failed_report_date_check_is_retried_the_same_day():                    # 2026-10-01 sweep
    h, eng, ch = make()
    tick(h, ch, at(MON, 14, 45))
    real = h.calendar_fn
    fail = [True]

    async def cal(today):
        return None if fail[0] else await real(today)
    h.calendar_fn = cal
    h.cal_rows[:] = [{**AMD_EV, "date": TUE}, XOM_EV]       # AMD's report moved to today (Tuesday) after the close
    tick(h, ch, at(TUE, 8, 31))                              # the 08:31 check can't read the calendar
    assert by_symbol(h)["AMD"].meta["exit_day"] != "2026-10-06"
    fail[0] = False
    tick(h, ch, at(TUE, 8, 31, 30))                          # inside the retry gap
    assert by_symbol(h)["AMD"].meta["exit_day"] != "2026-10-06"
    tick(h, ch, at(TUE, 8, 32))
    assert by_symbol(h)["AMD"].meta["exit_day"] == "2026-10-06"


def test_no_prior_vix_close_skips_entries():                                      # 2026-10-01 sweep item 4
    h, eng, ch = make(vix=None)
    tick(h, ch, at(MON, 14, 45))
    assert h.book.open == []
    assert {d["reason"] for d in h.ej.decisions()} == {"no prior VIX close"}


def test_yesterdays_vix_is_not_used_today():                                      # 2026-10-01 sweep item 4
    h, eng, ch = make(vix=18.0)
    tick(h, ch, at(MON, 9, 0))
    assert h.vix_prev == 18.0
    h.vix.v = None                                                                # Tuesday's read fails
    h.cal_rows[:] = [{**AMD_EV, "date": FRI1}]                                    # Tuesday is T-3 for a Friday report
    tick(h, ch, at(TUE, 14, 45))
    assert not h.book.open
    assert [d["reason"] for d in h.ej.decisions() if d["day"] == str(TUE)] == ["no prior VIX close"]
