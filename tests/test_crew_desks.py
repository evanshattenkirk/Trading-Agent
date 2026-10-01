"""New crew desks (Ops, Earnings, Post-mortem): what each reports, when it attends, and that none of them can raise
size, pitch changes or add blackouts."""
import asyncio
import copy
import json
import sys
from datetime import date, time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.clock import at_ct
from agentdesk.config import load_config
from agentdesk.crew import DESKS, PREMARKET_DESKS, RESTRICT_ONLY, VOTERS_FOR_SIZE_UP, Crew
from agentdesk.exits import Contract, Position
from agentdesk.risk import RiskManager

CFG = load_config()
D = date(2026, 9, 28)          # a Monday
NEW = ("ops", "earnings", "postmortem")


class Bus:
    def __init__(self):
        self.events = []

    def emit(self, kind, ts, **kw):
        self.events.append((kind, ts, kw))


class Journal:
    def __init__(self):
        self.briefs = []

    def record_brief(self, day, ts, desk, brief):
        json.dumps(brief)                     # briefs must be JSON-serializable for the real journal
        self.briefs.append((ts, desk))


class Feed:
    def __init__(self, now, sim=True):
        self.t, self.is_sim, self.mood = now, sim, "neutral"

    def now(self):
        return self.t


def warm_sig(bars=130):
    tf = lambda: SimpleNamespace(bars=bars, last=object() if bars >= 35 else None)
    return SimpleNamespace(tf={"15m": tf(), "5m": tf(), "1m": tf()}, hist_rising=lambda tf: False)


class FakeEngine:
    def __init__(self, cfg, now, sim=True):
        self.cfg, self.feed, self.bus, self.journal = cfg, Feed(now, sim), Bus(), Journal()
        self.mode = "sim" if sim else "paper"
        self.risk = RiskManager(cfg)
        self.price, self.l2, self.l2_rh = 765.0, None, None
        self.bars = {"1m": []}
        self.closed, self.skips = [], []
        self.sig = warm_sig()
        self.agent = []

    def set_agent(self, now, activity, text=""):
        self.agent.append((now, activity, text))


def make(cfg=None, now=None, sim=True, tmp=None):
    cfg = copy.deepcopy(cfg or CFG)
    if tmp is not None:
        cfg["journal_path"] = str(tmp / "journal.db")
        cfg["crew"].setdefault("ops", {})["recorder_days_log"] = str(tmp / "days.jsonl")
        cfg["crew"]["postmortem_dir"] = str(tmp / "postmortems")
    e = FakeEngine(cfg, now or at_ct(D, time(7, 0)), sim)
    c = Crew(e, cfg)
    c.day = D
    return e, c


def run(coro):
    return asyncio.run(coro)


def walks(e, ts):
    return [kw["desk"] for _, t, kw in e.bus.events if t == ts and kw.get("phase") == "walk"]


# ----------------------------------------------------------------------------- authority
def test_new_desks_are_registered_restrict_only_and_never_size_up_voters():
    for k in NEW:
        assert k in DESKS and k in RESTRICT_ONLY and k not in VOTERS_FOR_SIZE_UP
        assert DESKS[k].uses_web is False


def test_restrict_only_briefs_are_clamped_and_cannot_pitch_or_add_blackouts():
    e, c = make()
    raw = {"headline": "x", "size_multiplier": 1.25, "events": [{"time_ct": "10:00", "name": "AAPL", "impact": "high"}],
           "proposals": [{"scope": "standing", "title": "t", "params": {"exits.stop_loss_pct": 0.1}}]}
    for k in NEW:
        b = c._finish(copy.deepcopy(raw), k)
        assert b["size_multiplier"] == 1.0 and b["proposals"] == [] and b["events"] == []
    assert c._finish(copy.deepcopy(raw), "macro")["size_multiplier"] == 1.25     # existing desks unchanged


def test_roundtable_cannot_raise_a_new_desk_vote_or_pitch_for_it():
    e, c = make(now=at_ct(D, time(8, 25)))

    async def talk(slot, desks, now):
        return {"lines": [], "votes": {"ops": 1.25, "earnings": 1.2},
                "proposals": [{"desk": "earnings", "scope": "day", "title": "t", "params": {"risk.max_trades_per_day": 3}}]}
    c._roundtable = talk
    run(c._consult("premarket", PREMARKET_DESKS, at_ct(D, time(8, 25))))
    assert c.briefs["ops"]["size_multiplier"] <= 1.0 and c.briefs["earnings"]["size_multiplier"] <= 1.0
    assert not [p for p in c.book.items if p.get("desk") == "earnings"]
    assert e.risk.st.size_mult <= 1.0


# ----------------------------------------------------------------------------- schedule
def test_ops_and_earnings_catch_up_at_0815_and_the_huddle_still_ends_before_the_open():
    e, c = make()
    t_huddle = at_ct(D, time(8, 25))

    async def day():
        await c.on_clock(at_ct(D, time(8, 15)))
        prepared = set(c.prep)
        await c.on_clock(t_huddle)
        return prepared

    prepared = run(day())
    assert {"ops", "earnings"} <= prepared
    assert walks(e, t_huddle) == PREMARKET_DESKS and {"ops", "earnings"} <= set(PREMARKET_DESKS)
    assert len(PREMARKET_DESKS) <= 7                  # huddle spots in the office
    done = [ts for _, ts, kw in e.bus.events if kw.get("phase") == "done"]
    assert max(done) < at_ct(D, time(8, 30))


def test_postmortem_joins_the_postclose_huddle_and_ops_joins_halts():
    e, c = make()
    t = at_ct(D, time(15, 5))
    run(c.on_clock(t))
    assert "postmortem" in walks(e, t)
    e2, c2 = make(now=at_ct(D, time(10, 0)))
    e2.risk.halt("SAFETY: test")
    t2 = at_ct(D, time(10, 0))
    run(c2.on_trade_closed(SimpleNamespace(), -10.0, t2))
    assert "ops" in walks(e2, t2)


def test_templated_roundtable_gives_the_new_desks_a_line():
    e, c = make()
    run(c._consult("premarket", PREMARKET_DESKS, at_ct(D, time(8, 25))))
    said = {(kw.get("who"), kw.get("to")) for _, _, kw in e.bus.events if kw.get("phase") == "say"}
    assert ("ops", "risk") in said or ("ops", "agent") in said
    assert ("earnings", "vol") in said
    e2, c2 = make()
    run(c2._consult("postclose", ["quant", "vol", "postmortem"], at_ct(D, time(15, 5))))
    said2 = {(kw.get("who"), kw.get("to")) for _, _, kw in e2.bus.events if kw.get("phase") == "say"}
    assert ("postmortem", "quant") in said2


# ----------------------------------------------------------------------------- Ops
def test_ops_all_green_in_sim():
    e, c = make()
    b = c._finish(c._ops_brief("premarket"), "ops")
    assert all(ch["ok"] for ch in b["checks"]), b["checks"]
    assert b["size_multiplier"] == 1.0 and "green" in b["headline"].lower()


def test_ops_votes_half_when_signal_history_is_cold():
    e, c = make()
    e.sig = warm_sig(bars=12)
    b = c._finish(c._ops_brief("premarket"), "ops")
    assert b["size_multiplier"] == 0.5
    assert any("15m" in n for n in b["notes"])


def test_ops_reports_a_book_that_is_not_paper_only_without_acting_on_it():
    cfg = copy.deepcopy(CFG)
    cfg["books"]["B_iron_fly"]["paper_only"] = False
    e, c = make(cfg)
    b = c._finish(c._ops_brief("premarket"), "ops")
    bad = [ch for ch in b["checks"] if not ch["ok"]]
    assert [ch["name"] for ch in bad] == ["Books paper-only"] and bad[0]["detail"] == "B_iron_fly"   # not account/fills
    assert b["size_multiplier"] == 1.0


def test_ops_recorder_check_reads_the_recorder_day_log(tmp_path):
    e, c = make(sim=False, tmp=tmp_path)
    rec = lambda: next(ch for ch in c._ops_brief("premarket")["checks"] if ch["name"] == "Recorder")
    assert rec()["ok"] is False                                                   # no log yet
    (tmp_path / "days.jsonl").write_text(json.dumps({"day": "2026-09-25", "rows": 98000}) + "\n")   # last Friday
    assert rec()["ok"] is True and "98000" in rec()["detail"]
    rh = next(ch for ch in c._ops_brief("premarket")["checks"] if ch["name"] == "Robinhood")
    assert rh["ok"] is False                                                      # not connected in this fake


# ----------------------------------------------------------------------------- Earnings
CAL = [{"symbol": "NVDA", "date": "2026-10-01", "timing": "pm"},      # T=3 -> E1
       {"symbol": "AAPL", "date": "2026-10-09", "timing": "pm"},      # T=9 -> E2
       {"symbol": "MSFT", "date": "2026-09-25", "timing": "pm"}]      # reported last Friday night -> heavyweight note


def cfg_with_calendar():
    cfg = copy.deepcopy(CFG)
    cfg["crew"]["earnings"]["calendar"] = CAL
    return cfg


def test_earnings_brief_from_the_config_calendar():
    e, c = make(cfg_with_calendar())
    b = c._finish(run(c._earnings_brief(at_ct(D, time(8, 15)))), "earnings")
    assert "NVDA" in b["headline"] and "E1" in b["headline"]
    flags = {r["symbol"]: r["flag"] for r in b["screen"]}
    assert flags["NVDA"] == "E1" and flags["AAPL"] == "E2"
    assert any("MSFT" in n for n in b["notes"])
    assert b["events"] == [] and b["size_multiplier"] == 1.0
    json.dumps(b)


def test_earnings_uses_the_robinhood_calendar_once_per_day():
    e, c = make(sim=False)
    calls = []

    class RH:
        async def call(self, tool, args):
            calls.append((tool, args))
            return {"results": [{"symbol": "NVDA", "report": {"date": "2026-10-01", "timing": "pm", "verified": True}}]}
    e.l2_rh = RH()
    b = run(c._earnings_brief(at_ct(D, time(8, 15))))
    run(c._earnings_brief(at_ct(D, time(8, 25))))
    assert calls == [("get_earnings_calendar", {"start_date": "2026-09-28", "days": 31, "filter": "high_market_cap"})]
    assert b["screen"][0]["symbol"] == "NVDA" and b["screen"][0]["flag"] == "E1"


def test_earnings_falls_back_to_config_when_the_calendar_call_fails():
    e, c = make(cfg_with_calendar(), sim=False)

    class RH:
        async def call(self, tool, args):
            raise RuntimeError("503")
    e.l2_rh = RH()
    b = run(c._earnings_brief(at_ct(D, time(8, 15))))
    assert {r["symbol"] for r in b["screen"]} == {"NVDA", "AAPL"}
    assert any("unavailable" in n for n in b["notes"])


def test_sim_seeds_a_demo_calendar_with_an_e1_and_an_e2():
    e, c = make()
    b = run(c._earnings_brief(at_ct(D, time(8, 15))))
    assert {"E1", "E2"} <= {r["flag"] for r in b["screen"]}


# ----------------------------------------------------------------------------- Post-mortem
def trade(open_hm, close_hm, qty=4, entry=2.0, pnl_pct=0.10, peak_pct=0.20, reason="scale-out"):
    p = Position(Contract("SPY", "2026-09-28", 660.0, "call"), "SWING", qty, entry, at_ct(D, open_hm))
    p.closed_ts = at_ct(D, close_hm)
    p.realized, p.fees = pnl_pct * entry * 100 * qty, 0.0
    p.peak, p.exit_reason, p.status = entry * (1 + peak_pct), reason, "closed"
    return p


def test_postmortem_clean_day():
    e, c = make()
    e.closed = [trade(time(9, 5), time(9, 40)), trade(time(11, 0), time(11, 12), pnl_pct=-0.20, reason="stop")]
    b = c._finish(c._postmortem_brief(at_ct(D, time(15, 5))), "postmortem")
    assert b["breaks"] == [] and "followed the rules" in b["headline"]
    assert b["size_multiplier"] == 1.0


def test_postmortem_flags_rule_breaks_and_round_trips():
    e, c = make()
    e.risk.add_blackout(at_ct(D, time(9, 0)), "CPI")          # blackout 08:50-09:20
    e.closed = [trade(time(9, 5), time(14, 50), qty=7, pnl_pct=-0.40, reason="stop"),
                trade(time(10, 0), time(10, 30), pnl_pct=-0.05, peak_pct=0.60, reason="trailing")]
    b = c._postmortem_brief(at_ct(D, time(15, 5)))
    kinds = {x["rule"] for x in b["breaks"]}
    assert kinds == {"blackout", "contracts", "flatten", "stop"}
    assert "rule break" in b["headline"].lower() and "engine" in b["headline"].lower()
    assert len(b["reviews"]) == 1 and "round trip" in b["reviews"][0]["why"]


def test_postmortem_ignores_a_blackout_added_after_the_trade_opened():           # 2026-10-01 sweep item 11
    e, c = make()
    e.risk.add_blackout(at_ct(D, time(10, 30)), "Fed speaker", added_ts=at_ct(D, time(11, 30)))   # 10:20-10:40
    e.closed = [trade(time(10, 25), time(10, 50))]
    b = c._postmortem_brief(at_ct(D, time(15, 5)))
    assert b["breaks"] == []
    e.risk.add_blackout(at_ct(D, time(10, 30)), "CPI", added_ts=at_ct(D, time(8, 25)))           # known in time
    b = c._postmortem_brief(at_ct(D, time(15, 5)))
    assert [x["rule"] for x in b["breaks"]] == ["blackout"] and "CPI" in b["breaks"][0]["why"]


def test_postmortem_writes_the_daily_file_outside_the_sim(tmp_path):
    e, c = make(sim=False, tmp=tmp_path)
    e.closed = [trade(time(9, 5), time(9, 40))]
    c._postmortem_brief(at_ct(D, time(15, 5)))
    text = (tmp_path / "postmortems" / "2026-09-28.md").read_text()
    assert "Post-mortem 2026-09-28" in text and "SPY" in text


def test_postmortem_writes_nothing_in_the_sim(tmp_path):
    e, c = make(tmp=tmp_path)
    c._postmortem_brief(at_ct(D, time(15, 5)))
    assert not (tmp_path / "postmortems").exists()
