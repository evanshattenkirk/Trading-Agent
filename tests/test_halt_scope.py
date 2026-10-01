"""Book A's own halts stay on book A (Evan, 2026-10-01, sweep items 1 and 2): A's daily-loss limit flattens and halts
A only, so B-G, E, F1 and F2 keep their positions and keep trading; after a restart only an account-wide halt (kill,
safety, a startup account check) halts the other books."""
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
    eng.risk.halt("SAFETY: stale quote", flatten=True)
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
