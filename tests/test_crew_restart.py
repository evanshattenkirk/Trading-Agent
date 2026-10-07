"""The crew's record survives a restart honestly (review plan 2026-10-06): today's applied tweaks are applied again
after a mid-day restart, or marked expired with the reason (M10); proposals.json is written atomically and a file
that can't be read is moved aside, not silently replaced by an empty book (L2)."""
import asyncio
import copy
import json
import logging
import sys
from datetime import date, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.clock import at_ct
from agentdesk.config import load_config
from agentdesk.crew import Crew
from agentdesk.proposals import ProposalBook

CFG = load_config()
D = date(2026, 9, 28)
SKIP_SCALP = {"scope": "day", "title": "Skip SCALP entries for the rest of today",
              "params": {"strategy.enabled_setups": ["SWING"]}}
FEWER = {"scope": "day", "title": "At most 6 trades today", "params": {"risk.max_trades_per_day": 6}}
NEXT_STOP = {"scope": "trade", "title": "15% stop on the next entry", "params": {"exits.stop_loss_pct": 0.15}}


def run(coro):
    return asyncio.run(coro)


def paper_engine(tmp_path, monkeypatch, hm=(10, 0)):
    from test_safety import engine
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    e = engine()
    e.cfg["journal_path"] = str(tmp_path / "journal.db")
    e.feed.t = at_ct(D, time(*hm))
    e.crew = Crew(e, e.cfg)                         # paper: proposals.json next to the journal
    e.crew.day = D
    return e


# ----------------------------------------------------------------------------- M10
def test_todays_day_tweaks_are_applied_again_after_a_restart(tmp_path, monkeypatch):
    e1 = paper_engine(tmp_path, monkeypatch)
    for p in (SKIP_SCALP, FEWER):
        run(e1.crew._handle_proposal("quant", copy.deepcopy(p), e1.feed.t))
    assert e1.cfg["strategy"]["enabled_setups"] == ["SWING"] and e1.cfg["risk"]["max_trades_per_day"] == 6

    e2 = paper_engine(tmp_path, monkeypatch, hm=(11, 0))     # crash and restart at 11:00
    assert e2.cfg["strategy"]["enabled_setups"] == ["SWING", "SCALP"]
    run(e2.run())
    assert e2.cfg["strategy"]["enabled_setups"] == ["SWING"] and e2.cfg["risk"]["max_trades_per_day"] == 6
    assert [i["status"] for i in e2.crew.book.items] == ["applied", "applied"]
    e2._new_day(D + timedelta(days=1), at_ct(D + timedelta(days=1), time(8, 10)))   # still reverted the next day
    assert e2.cfg["strategy"]["enabled_setups"] == ["SWING", "SCALP"] and e2.cfg["risk"]["max_trades_per_day"] == 12


def test_a_tweak_that_cannot_be_applied_again_is_marked_expired_with_the_reason(tmp_path, monkeypatch):
    e1 = paper_engine(tmp_path, monkeypatch)
    for p in (FEWER, NEXT_STOP):
        run(e1.crew._handle_proposal("quant", copy.deepcopy(p), e1.feed.t))
    assert e1.trade_tweaks == {"exits.stop_loss_pct": 0.15}

    e2 = paper_engine(tmp_path, monkeypatch, hm=(11, 0))
    e2.cfg["risk"]["max_trades_per_day"] = 4                # an approved standing change lowered it since
    run(e2.run())
    fewer, stop = e2.crew.book.items
    assert fewer["status"] == "expired" and "can only be lowered" in fewer["expired_why"]
    assert stop["status"] == "expired" and "restart" in stop["expired_why"]
    assert e2.cfg["risk"]["max_trades_per_day"] == 4 and e2.trade_tweaks == {}
    saved = json.loads((tmp_path / "proposals.json").read_text())
    assert [i["status"] for i in saved] == ["expired", "expired"]


def test_yesterdays_day_tweak_is_not_applied_again(tmp_path, monkeypatch):
    e1 = paper_engine(tmp_path, monkeypatch)
    run(e1.crew._handle_proposal("quant", copy.deepcopy(SKIP_SCALP), at_ct(D - timedelta(days=1), time(10, 0))))
    e2 = paper_engine(tmp_path, monkeypatch, hm=(8, 10))
    run(e2.run())
    assert e2.cfg["strategy"]["enabled_setups"] == ["SWING", "SCALP"]
    assert e2.crew.book.items[0]["status"] == "expired"


# ----------------------------------------------------------------------------- L2
def test_proposals_json_is_replaced_atomically(tmp_path, monkeypatch):
    path = tmp_path / "proposals.json"
    b = ProposalBook(copy.deepcopy(CFG), path)
    b.submit("quant", copy.deepcopy(SKIP_SCALP), at_ct(D, time(10, 0)))
    good = path.read_text()
    import agentdesk.proposals as pm

    def no_room(src, dst):
        raise OSError("No space left on device")
    monkeypatch.setattr(pm.os, "replace", no_room)
    try:
        b.submit("quant", copy.deepcopy(FEWER), at_ct(D, time(10, 5)))
    except OSError:
        pass
    assert path.read_text() == good                         # a failed write never leaves a half-written file
    assert len(ProposalBook(copy.deepcopy(CFG), path).items) == 1


def test_an_unreadable_proposals_file_is_moved_aside_with_a_warning(tmp_path, caplog):
    path = tmp_path / "proposals.json"
    path.write_text('[{"id": "7", "status": "pending", "scope": "standing", "ti')
    with caplog.at_level(logging.WARNING, logger="agentdesk.proposals"):
        b = ProposalBook(copy.deepcopy(CFG), path)
    assert b.items == []
    (aside,) = tmp_path.glob("proposals.json.corrupt-*")
    assert aside.read_text().startswith('[{"id": "7"')
    assert any("proposals.json" in r.getMessage() and "corrupt" in r.getMessage() for r in caplog.records)
    b.submit("quant", copy.deepcopy(SKIP_SCALP), at_ct(D, time(10, 0)))
    assert aside.exists() and len(json.loads(path.read_text())) == 1
