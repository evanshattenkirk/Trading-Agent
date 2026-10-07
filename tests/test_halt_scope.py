"""Book A's own halts stay on book A (Evan, 2026-10-01, sweep items 1 and 2): A's daily-loss limit flattens and halts
A only, so B-G, E, F1 and F2 keep their positions and keep trading; after a restart only an account-wide halt (kill,
safety, a startup account check) halts the other books. A stale-quote safety trip halts A and the 0DTE SPY books
B/C/D/G only; E, F1 and F2 keep running (Evan approved D3, 2026-10-07)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.exits import Contract, Position
from agentdesk.risk import RiskManager, RiskStore

from books_fakes import ct_ts
from test_books_host import setup, tick

C = Contract("SPY", "2026-09-28", 660.0, "call")


def a_loses(risk, at=-401.0):
    risk.st.day_pnl = at
    return risk.check_open_loss([])


def test_a_daily_loss_flattens_a_only_and_books_keep_trading():
    eng, fq, host = setup(intents=("B", "D"))
    tick(host, fq, ct_ts(8, 45))
    assert len(host.positions()) == 2
    assert a_loses(eng.risk)
    assert eng.risk.must_flatten(ct_ts(8, 46))            # A still flattens its own positions
    tick(host, fq, ct_ts(8, 46))
    assert len(host.positions()) == 2                     # B and D keep theirs
    ok, why = host._gate(host.books[0], ct_ts(8, 46))
    assert "halted" not in why                            # and can still open new risk


def test_safety_after_an_a_halt_still_flattens_every_book():
    eng, fq, host = setup(intents=("B",))
    tick(host, fq, ct_ts(8, 45))
    a_loses(eng.risk)
    eng.risk.halt("SAFETY: 3 broker/API errors in a row", flatten=True)     # account-wide (a stale quote is not: D3)
    assert eng.risk.account_flatten()
    tick(host, fq, ct_ts(8, 46))
    assert not host.positions()


def test_halt_scope_survives_a_restart(tmp_path):
    from agentdesk.config import load_config
    cfg = load_config()
    r = RiskManager(cfg, RiskStore(tmp_path / "r.json"))
    r.restore("2026-09-28")
    a_loses(r)
    again = RiskManager(cfg, RiskStore(tmp_path / "r.json"))
    again.restore("2026-09-28")
    assert again.st.halted and again.st.halt_scope == "A" and not again.account_flatten()


# ----------------------------------------------------------------------------- restart (item 2)
class Books:
    def __init__(self):
        self.halts = []

    def halt_all(self, reason, flatten=False):
        self.halts.append(reason)

    async def start(self):
        pass

    def positions(self):
        return []


def restarted(tmp_path, halt):
    from test_safety import engine
    e = engine()
    e.feed.is_sim = False
    e.risk = RiskManager(e.cfg, RiskStore(tmp_path / "r.json"))
    day = str(e.day or "2026-09-28")
    e.risk.restore(day)
    halt(e.risk)
    e.risk = RiskManager(e.cfg, RiskStore(tmp_path / "r.json"))
    e.books = Books()
    asyncio.run(e.run())
    return e


def test_restart_after_an_a_only_halt_leaves_the_other_books_alone(tmp_path):
    e = restarted(tmp_path, lambda r: r.halt("profit lock: gave back 50% of +$300", scope="A"))
    assert e.risk.st.halted and e.books.halts == []


def test_restart_after_an_account_halt_halts_the_books(tmp_path):
    e = restarted(tmp_path, lambda r: r.halt("KILL switch", flatten=True))
    assert e.books.halts == ["KILL switch"]


# ----------------------------------------------------------------------------- stale-quote trip scope (D3)
class OtherHost:
    """An E/F1/F2-style host next to BookHost in the HostGroup (shared AccountRisk, its own book)."""

    def __init__(self, letter):
        from agentdesk.books.book import Book
        from test_books_host import Scripted
        self.book = Book(f"{letter}_test", {}, Scripted())
        self.pos, self.account = [], None

    def positions(self):
        return list(self.pos)

    def open_risk(self):
        return 0.0

    def halt_all(self, reason, flatten=False):
        self.account.halt(reason, flatten)

    async def run(self, coro, inline):
        await coro

    async def on_second(self, now):
        pass

    async def on_bar(self, bar):
        pass


def engine_with_group():
    from agentdesk.books.group import HostGroup
    from test_books_engine import engine_with_books, step
    e, fq, feed = engine_with_books()
    step(e, fq, feed, ct_ts(8, 45))                       # book B opens its fly
    bookhost = e.books
    f1, e_host = OtherHost("F1"), OtherHost("E")
    e.books = HostGroup(bookhost, fhost=f1, ehost=e_host)
    return e, fq, feed, bookhost, f1, e_host


def test_a_stale_quote_halts_a_and_the_0dte_books_only():          # Evan approved D3, 2026-10-07
    from agentdesk.exits import ExitPlan
    from test_books_engine import step
    e, fq, feed, bookhost, f1, e_host = engine_with_group()
    pos = Position(C, "SWING", 1, 1.00, ct_ts(8, 45))
    e.open.append((pos, ExitPlan(e.cfg["exits"], pos)))

    async def go():
        e._watchdog(ct_ts(8, 45) + 11)
    asyncio.run(go())
    assert e.risk.st.halted and e.risk.st.flatten_all and "no fresh quote" in e.risk.st.halt_reason
    assert e.risk.must_flatten(ct_ts(8, 46))              # book A sells
    assert not e.risk.account_flatten() and not bookhost.account.halted     # E, F1 and F2 keep trading
    assert bookhost.books[0].halted and not f1.book.halted and not e_host.book.halted
    step(e, fq, feed, ct_ts(8, 45) + 12)
    assert not bookhost.positions()                       # B's fly is sold on the next tick


def test_a_stale_f1_position_also_halts_f1_itself():
    e, fq, feed, bookhost, f1, e_host = engine_with_group()
    f1.pos = [type("P", (), {"label": "F1 NVDA", "last_quote_ts": ct_ts(8, 45), "watchdog_exempt": False})()]
    for p in bookhost.positions():
        p.last_quote_ts = ct_ts(8, 45) + 20

    async def go():
        e._watchdog(ct_ts(8, 45) + 11)
    asyncio.run(go())
    assert e.risk.st.halted and "F1 NVDA" in e.risk.st.halt_reason
    assert f1.book.halted and not e_host.book.halted and not e.risk.account_flatten()


def test_other_safety_trips_stay_account_wide():
    e, fq, feed, bookhost, f1, e_host = engine_with_group()

    async def go():
        e._trip("3 broker/API errors in a row (last: manage: 503)", ct_ts(8, 46))
    asyncio.run(go())
    assert e.risk.account_flatten() and bookhost.account.halted and bookhost.account.flatten
