"""Crew suggestions are cleared once their reason has passed (Evan, 2026-10-01): a tweak for today ends with the day,
a change pitched because of an event (FOMC, CPI...) expires after that event, any other standing change expires after
crew.proposal_ttl_days, and an expired suggestion can no longer be approved."""
import asyncio
import copy
import json
import sys
from datetime import date, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.clock import at_ct
from agentdesk.config import load_config
from agentdesk.proposals import ProposalBook
from agentdesk.risk import RiskManager

from test_crew_desks import make

CFG = load_config()
D = date(2026, 9, 30)           # a Wednesday
FOMC = {"name": "FOMC rate decision", "end": at_ct(D, time(13, 20))}
TRAIL = {"exits.swing.runner_trail_pct": 0.20}


def book(tmp_path=None):
    return ProposalBook(copy.deepcopy(CFG), tmp_path / "p.json" if tmp_path else None)


def standing(title="Tighter runner trail", rationale="", params=TRAIL, **kw):
    return {"scope": "standing", "title": title, "rationale": rationale, "params": dict(params), **kw}


def test_day_tweak_ends_with_its_day():
    b = book()
    item = b.submit("quant", {"scope": "day", "title": "Skip SCALP", "params": {"strategy.enabled_setups": ["SWING"]}},
                    at_ct(D, time(10, 0)))
    assert item["status"] == "applied"
    assert b.expire(at_ct(D, time(15, 0))) == []
    gone = b.expire(at_ct(D + timedelta(days=1), time(8, 0)))
    assert [g["id"] for g in gone] == [item["id"]] and item["status"] == "expired"


def test_change_pitched_for_an_event_expires_after_the_event():
    b = book()
    item = b.submit("macro", standing(rationale="Runners whipsaw around the Fed decision at 13:00 CT"),
                    at_ct(D, time(8, 25)), events=[FOMC, {"name": "Crude inventories", "end": at_ct(D, time(9, 50))}])
    assert item["status"] == "pending" and item["expires_ts"] == FOMC["end"]
    assert b.expire(at_ct(D, time(13, 0))) == [] and b.pending()
    b.expire(at_ct(D, time(13, 21)))
    assert b.pending() == [] and item["status"] == "expired" and "FOMC" in item["expired_why"]


def test_event_already_past_keeps_the_pitch_until_the_end_of_the_day():
    b = book()
    item = b.submit("quant", standing(rationale="CPI blew out this morning; stops too wide"), at_ct(D, time(9, 0)),
                    events=[{"name": "CPI", "end": at_ct(D, time(7, 50))}])
    assert at_ct(D, time(15, 5)) < item["expires_ts"] < at_ct(D + timedelta(days=1), time(0, 0, 1))


def test_event_later_this_week_from_the_calendar():
    b = book()
    nfp = {"name": "Nonfarm payrolls", "end": at_ct(D + timedelta(days=2), time(7, 50))}
    item = b.submit("macro", standing(rationale="Tighter trail into Friday's payrolls report"), at_ct(D, time(15, 5)),
                    events=[FOMC, nfp])
    assert item["expires_ts"] == nfp["end"]


def test_standing_change_with_no_event_expires_after_the_ttl():
    b = book()
    now = at_ct(D, time(15, 5))
    item = b.submit("quant", standing(rationale="2 runners gave back most of their gains"), now, events=[FOMC])
    ttl = CFG["crew"]["proposal_ttl_days"]["standing"]
    assert item["expires_ts"] == now + ttl * 86400
    assert b.expire(now + ttl * 86400 - 60) == []
    assert len(b.expire(now + ttl * 86400 + 1)) == 1


def test_desk_can_say_when_its_reason_passes():
    b = book()
    by_day = b.submit("macro", standing(title="a", until="2026-10-01"), at_ct(D, time(8, 25)))
    by_time = b.submit("macro", standing(title="b", params={"exits.stop_loss_pct": 0.15}, until="2026-09-30 13:30"),
                       at_ct(D, time(8, 25)))
    assert by_day["expires_ts"] == at_ct(date(2026, 10, 1), time(23, 59, 59))
    assert by_time["expires_ts"] == at_ct(D, time(13, 30))


def test_desk_until_cannot_outlast_the_ttl():
    b = book()
    now = at_ct(D, time(8, 25))
    item = b.submit("macro", standing(until="2027-06-01"), now)
    assert item["expires_ts"] == now + CFG["crew"]["proposal_ttl_days"]["standing"] * 86400


def test_new_strategy_pitch_has_no_default_expiry():
    b = book()
    now = at_ct(D, time(15, 5))
    item = b.submit("vol", {"scope": "new_strategy", "title": "Fed-day strangle", "spec": "rules",
                            "rationale": "Vol around the FOMC is mispriced"}, now, events=[FOMC])
    assert item["expires_ts"] is None
    assert b.expire(now + 90 * 86400) == [] and b.pending() == [item]


def test_same_standing_change_pitched_again_refreshes_the_pending_one():
    b = book()
    first = b.submit("quant", standing(rationale="day 1"), at_ct(D, time(15, 5)))
    again = b.submit("quant", standing(rationale="day 2"), at_ct(D + timedelta(days=1), time(15, 5)))
    assert again["id"] == first["id"] and len(b.pending()) == 1
    assert first["rationale"] == "day 2" and first["expires_ts"] > at_ct(D + timedelta(days=5), time(15, 5))


def test_items_saved_before_expiry_existed_still_expire(tmp_path):
    old = at_ct(D - timedelta(days=7), time(15, 5))
    (tmp_path / "p.json").write_text(json.dumps([
        {"id": "1", "ts": old, "desk": "quant", "scope": "standing", "title": "t", "rationale": "", "params": TRAIL,
         "spec": "", "evidence": "", "status": "pending"},
        {"id": "2", "ts": at_ct(D - timedelta(days=1), time(10, 0)), "desk": "quant", "scope": "day", "title": "d",
         "rationale": "", "params": {"strategy.enabled_setups": ["SWING"]}, "spec": "", "evidence": "", "status": "applied"},
        {"id": "3", "ts": old, "desk": "quant", "scope": "standing", "title": "kept", "rationale": "", "params": TRAIL,
         "spec": "", "evidence": "", "status": "approved"}]))
    b = book(tmp_path)
    gone = b.expire(at_ct(D, time(8, 10)))
    assert sorted(g["id"] for g in gone) == ["1", "2"]
    assert b.items[2]["status"] == "approved"                       # an approved change is permanent
    assert [i["status"] for i in json.loads((tmp_path / "p.json").read_text())][:2] == ["expired", "expired"]


# ----------------------------------------------------------------------------- crew wiring
def test_crew_passes_todays_blackouts_and_expires_on_the_clock():
    e, c = make(now=at_ct(D, time(8, 25)))
    c.day = D
    e.risk.add_blackout(at_ct(D, time(13, 0)), "FOMC rate decision")
    asyncio.run(c._handle_proposal("macro", standing(rationale="FOMC at 13:00 CT"), at_ct(D, time(8, 25))))
    item = c.book.pending()[0]
    assert item["expires_ts"] == at_ct(D, time(13, 0)) + CFG["risk"]["event_blackout"]["after_min"] * 60
    e.bus.events.clear()
    c.expire_proposals(at_ct(D, time(13, 21)))
    assert c.book.pending() == []
    assert [kw["item"]["status"] for k, _, kw in e.bus.events if k == "proposal"] == ["expired"]


def test_crew_reads_future_events_from_the_weekly_calendar():
    e, c = make(now=at_ct(D, time(15, 5)))
    c._calendar = {"week": "2026-09-28", "events": [
        {"date": "2026-10-02", "time_ct": "07:30", "name": "Nonfarm payrolls", "impact": "high"},
        {"date": "2026-09-29", "time_ct": "09:00", "name": "JOLTS", "impact": "medium"}]}
    names = {ev["name"] for ev in c.known_events(at_ct(D, time(15, 5)))}
    assert "Nonfarm payrolls" in names and "JOLTS" not in names


def test_state_hides_expired_suggestions():
    e, c = make(now=at_ct(D, time(8, 25)))
    c.book.submit("quant", {"scope": "day", "title": "x", "params": {"strategy.enabled_setups": ["SWING"]}},
                  at_ct(D - timedelta(days=1), time(10, 0)))
    c.expire_proposals(at_ct(D, time(8, 25)))
    assert c.state()["proposals"] == []


# ----------------------------------------------------------------------------- approving
def _engine_with_crew(auto_apply=True):
    from test_safety import engine
    e = engine()
    e.cfg["crew"]["auto_apply_tweaks"] = auto_apply
    e.feed.is_sim = True                         # no proposals.json on disk
    from agentdesk.crew import Crew
    e.crew = Crew(e, e.cfg)
    e.crew.day = D
    return e


def test_expired_suggestion_cannot_be_approved():
    e = _engine_with_crew()
    e.feed.t = at_ct(D, time(8, 25))
    item = e.crew.book.submit("macro", standing(rationale="FOMC today"), e.feed.t, events=[FOMC])
    e.feed.t = at_ct(D, time(14, 0))
    assert e.decide_proposal(item["id"], True) is None
    assert item["status"] == "expired"
    assert e.cfg["exits"]["swing"]["runner_trail_pct"] != 0.20


def test_approving_a_pending_day_tweak_applies_it():
    e = _engine_with_crew(auto_apply=False)
    e.feed.t = at_ct(D, time(9, 0))
    item = e.crew.book.submit("quant", {"scope": "day", "title": "Skip SCALP",
                                        "params": {"strategy.enabled_setups": ["SWING"]}}, e.feed.t)
    assert item["status"] == "pending"
    got = e.decide_proposal(item["id"], True)
    assert got["status"] == "approved"
    assert e.cfg["strategy"]["enabled_setups"] == ["SWING"]


# ----------------------------------------------------------------------------- blackouts across days
def test_reset_day_drops_yesterdays_blackouts():
    r = RiskManager(copy.deepcopy(CFG))
    r.add_blackout(at_ct(D, time(13, 0)), "Initial jobless claims")
    r.add_blackout(at_ct(D + timedelta(days=1), time(7, 30)), "Initial jobless claims")
    r.reset_day(str(D + timedelta(days=1)))
    assert [b.start for b in r.st.blackouts] == [at_ct(D + timedelta(days=1), time(7, 20))]
