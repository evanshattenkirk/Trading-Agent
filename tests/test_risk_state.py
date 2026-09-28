"""Day risk state survives a restart, and open-position losses count toward the daily loss limit."""
import asyncio
import copy
import json
import sys
from datetime import date, time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.bus import Bus
from agentdesk.clock import at_ct
from agentdesk.config import load_config
from agentdesk.engine import Engine
from agentdesk.exits import Contract, ExitPlan, Position
from agentdesk.feeds.base import Quote
from agentdesk.journal import Journal
from agentdesk.risk import RiskManager, RiskStore

CFG = load_config()
DAY = "2026-09-28"
NOW = at_ct(date(2026, 9, 28), time(9, 30))
LIMIT = CFG["risk"]["max_daily_loss"]


def manager(tmp_path, day=DAY, **kw) -> RiskManager:
    r = RiskManager(copy.deepcopy(CFG), store=RiskStore(tmp_path / "risk_state.json"))
    for k, v in kw.items():
        setattr(r, k, v)
    r.restore(day)
    return r


# --------------------------------------------------------------------------- persistence
def test_restart_keeps_days_pnl_trades_cooldown_and_halt(tmp_path):
    r = manager(tmp_path)
    r.on_realized(-150.0)
    r.on_trade_closed(-150.0, NOW)
    r.on_realized(-90.0)
    r.on_trade_closed(-90.0, NOW + 600)          # second loss in a row starts the cooldown
    r.halt("KILL switch", flatten=True)
    r2 = manager(tmp_path)
    assert r2.st.day_pnl == -240.0
    assert r2.st.trades == 2 and r2.st.losses == 2 and r2.st.loss_streak == 2
    assert r2.st.cooldown_until == r.st.cooldown_until > NOW
    assert r2.st.halted and r2.st.halt_reason == "KILL switch"
    assert not r2.can_enter(NOW + 7200, 0)[0]


def test_a_new_day_starts_clean(tmp_path):
    r = manager(tmp_path)
    r.on_realized(-300.0)
    r.halt("KILL switch")
    r2 = manager(tmp_path, day="2026-09-29")
    assert r2.st.day_pnl == 0 and not r2.st.halted
    assert json.loads((tmp_path / "risk_state.json").read_text())["day"] == "2026-09-29"


def test_reset_day_persists_the_new_day(tmp_path):
    r = manager(tmp_path)
    r.on_realized(-300.0)
    r.reset_day("2026-09-29")
    assert manager(tmp_path, day="2026-09-29").st.day_pnl == 0


def test_pause_and_crew_size_cut_survive_restart(tmp_path):
    r = manager(tmp_path)
    r.set_paused(True)
    r.set_size_mult(0.5)
    r2 = manager(tmp_path)
    assert r2.st.paused and r2.st.size_mult == 0.5


def test_startup_check_halts_are_not_sticky(tmp_path):
    """"Positions already open at startup" is re-checked on every start, so it must not survive a restart."""
    r = manager(tmp_path)
    r.halt("1 option position(s) already open", sticky=False)
    r.on_realized(-10.0)                          # any later save must not make it sticky
    assert not manager(tmp_path).st.halted


def test_clear_halt_keeps_pnl_and_the_loss_limit_still_applies(tmp_path):
    r = manager(tmp_path)
    r.halt("KILL switch", flatten=True)
    r2 = manager(tmp_path, clear_halt_on_restore=True)
    assert not r2.st.halted and not r2.st.flatten_all
    assert r2.can_enter(NOW, 0)[0]
    r2.on_realized(-LIMIT)
    r3 = manager(tmp_path, clear_halt_on_restore=True)   # clearing again can't get around the dollar limit
    ok, why = r3.can_enter(NOW, 0)
    assert not ok and "daily loss" in why


def test_unreadable_state_file_fails_closed(tmp_path):
    (tmp_path / "risk_state.json").write_text("{not json")
    r = manager(tmp_path)
    assert r.st.halted and "risk state" in r.st.halt_reason


def test_no_store_means_no_files(tmp_path):
    r = RiskManager(copy.deepcopy(CFG))
    r.restore(DAY)
    r.on_realized(-10.0)
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- unrealized P&L
def pos(entry=2.00, qty=2, bid=2.00):
    p = Position(Contract("SPY", DAY, 660.0, "call"), "SWING", qty, entry, NOW)
    p.bid = bid
    return p


def test_open_loss_counts_toward_the_daily_limit_at_the_bid(tmp_path):
    r = manager(tmp_path)
    r.on_realized(-300.0)
    assert not r.check_open_loss([pos(bid=1.60)])         # -300 - 80 = -380: inside the $400 limit
    assert not r.st.halted
    assert r.check_open_loss([pos(bid=1.40)])             # -300 - 120 = -420: breached
    assert r.st.halted and r.st.flatten_all
    assert "open" in r.st.halt_reason
    assert manager(tmp_path).st.halted                   # and it survives a restart


def test_can_enter_includes_open_pnl(tmp_path):
    r = manager(tmp_path)
    r.on_realized(-350.0)
    assert r.can_enter(NOW, 0, open_pnl=-40.0)[0]
    ok, why = r.can_enter(NOW, 0, open_pnl=-60.0)
    assert not ok and "daily loss" in why


def test_open_gain_never_offsets_a_realized_breach(tmp_path):
    r = manager(tmp_path)
    r.on_realized(-LIMIT - 1)
    assert not r.can_enter(NOW, 0, open_pnl=+500.0)[0]


# --------------------------------------------------------------------------- engine wiring
class Feed:
    is_sim, name = False, "fake"
    now = staticmethod(lambda: NOW)


class Quotes:
    def __init__(self):
        self.q = None

    async def quote(self, contract):
        return self.q


class Broker:
    live, name = False, "paper"

    def __init__(self):
        self.sells = []

    async def submit(self, contract, side, qty, limit, now):
        from agentdesk.brokers.base import OrderResult
        self.sells.append((side, qty, limit))
        return OrderResult("filled", qty, limit, "x", "")

    async def cancel_all(self):
        pass


def test_engine_flattens_when_open_loss_breaches_the_limit():
    quotes, broker = Quotes(), Broker()
    e = Engine(copy.deepcopy(CFG), Feed(), quotes, broker, Bus(), Journal(None), "paper")
    e.inline = True
    e.risk.on_realized(-300.0)
    p = Position(Contract("SPY", DAY, 660.0, "call"), "SWING", 5, 2.00, NOW)
    e.open.append((p, ExitPlan(e.cfg["exits"], p)))
    quotes.q = Quote(1.78, 1.82, NOW)           # mid 1.80 is above the -20% stop (1.60); bid loss -110 -> -410
    asyncio.run(e.manage(NOW))
    assert e.risk.st.halted and "open" in e.risk.st.halt_reason
    assert broker.sells and broker.sells[0][:2] == ("sell", 5)
    assert not e.open
