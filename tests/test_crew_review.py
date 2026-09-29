"""Crew review changes (Evan approved 2026-09-29, docs/superpowers/specs/2026-09-29-crew-review-changes.md):
crew effects logged, roundtable scope, Quant rules first, cost tally, weekly event calendar, Vol from Robinhood data,
Fed and Rates folded into Macro, votes routed by book, Sonnet 5.5."""
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
from agentdesk.crew import INFO_ONLY, PREMARKET_DESKS, VOTERS_FOR_SIZE_UP, Crew
from agentdesk.exits import Contract, Position
from agentdesk.journal import Journal
from agentdesk.risk import RiskManager, RiskStore

from test_crew_desks import D, make

CFG = load_config()


def run(coro):
    return asyncio.run(coro)


class FakeMessages:
    """Replies in order; the last reply repeats. Records every request."""
    def __init__(self, *texts, usage=None):
        self.texts, self.calls, self.usage = list(texts), [], usage

    async def create(self, **kw):
        self.calls.append(copy.deepcopy(kw))
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                               usage=self.usage, model=kw.get("model"))


class FakeVix:
    def __init__(self, current=16.0, prev=15.5):
        self.cur, self.prev = current, prev

    async def current(self):
        return self.cur

    async def prior_close(self, day):
        return self.prev


def online(monkeypatch, *texts, now=None, usage=None, tmp=None):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    e, c = make(now=now or at_ct(D, time(8, 15)), sim=False, tmp=tmp)
    assert not c.offline
    c._client = SimpleNamespace(messages=FakeMessages(*texts, usage=usage))
    c.vix = FakeVix()
    return e, c


def web_calls(c):
    return [k for k in c._client.messages.calls if k.get("tools")]


def user_text(kw) -> str:
    return kw["messages"][-1]["content"]


# ----------------------------------------------------------------------------- 2. roundtable scope
def test_roundtable_only_revises_desks_in_that_huddle():
    e, c = make(now=at_ct(D, time(8, 25)))
    run(c._consult("premarket", PREMARKET_DESKS, at_ct(D, time(8, 25))))
    before = c.briefs["ops"]["size_multiplier"]

    async def talk(slot, desks, now):
        return {"lines": [], "votes": {"ops": 0.5, "macro": 0.9}, "proposals": []}
    c._roundtable = talk
    run(c._consult("midday", ["vol", "macro"], at_ct(D, time(11, 30))))
    assert c.briefs["ops"]["size_multiplier"] == before          # ops wasn't at the midday huddle
    assert c.briefs["macro"]["size_multiplier"] == 0.9


# ----------------------------------------------------------------------------- 3. Quant rules first
def test_quant_llm_adds_notes_but_keeps_the_rule_based_vote_and_pitches(monkeypatch):
    reply = json.dumps({"headline": "LLM says cut", "size_multiplier": 0.5, "cooldown_minutes": 30,
                        "notes": ["LLM: entries late in the move"],
                        "proposals": [{"scope": "day", "title": "t", "params": {"exits.stop_loss_pct": 0.1}}]})
    e, c = online(monkeypatch, reply, now=at_ct(D, time(10, 0)))
    b = run(c._brief("quant", "loss-review", e.feed.now()))
    assert b["size_multiplier"] == 1.0 and b["cooldown_minutes"] == 0 and b.get("proposals", []) == []
    assert b["headline"] == "No trades yet today."
    assert "LLM: entries late in the move" in b["notes"]


# ----------------------------------------------------------------------------- 4. cost tally
def test_usage_counts_output_web_searches_and_cost_per_desk(monkeypatch):
    usage = SimpleNamespace(input_tokens=10000, output_tokens=1000, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0, server_tool_use=SimpleNamespace(web_search_requests=2))
    e, c = online(monkeypatch, '{"headline": "Quiet day"}', usage=usage)
    run(c._llm("macro", e.feed.now()))
    u = c.cache_usage
    assert u["calls"] == 1 and u["input"] == 10000 and u["output"] == 1000 and u["web_searches"] == 2
    # Sonnet 5.5: $2/M in, $10/M out, $10 per 1,000 searches -> 0.02 + 0.01 + 0.02
    assert abs(u["est_cost_usd"] - 0.05) < 1e-9
    assert u["by_desk"]["macro"]["calls"] == 1 and abs(u["by_desk"]["macro"]["est_cost_usd"] - 0.05) < 1e-9


# ----------------------------------------------------------------------------- 8. Fed and Rates fold into Macro
def test_fed_and_rates_are_info_only_and_size_up_voters_are_macro_and_vol():
    assert set(INFO_ONLY) == {"fed", "rates"}
    assert VOTERS_FOR_SIZE_UP == ("macro", "vol")
    e, c = make()
    raw = {"headline": "x", "size_multiplier": 0.5, "events": [{"time_ct": "10:00", "name": "Auction", "impact": "high"}],
           "proposals": [{"scope": "day", "title": "t", "params": {"exits.stop_loss_pct": 0.1}}]}
    for k in INFO_ONLY:
        b = c._finish(copy.deepcopy(raw), k)
        assert b["size_multiplier"] == 1.0 and b["events"] == [] and b["proposals"] == []


def test_fed_and_rates_briefs_come_from_macro_without_their_own_call(monkeypatch):
    e, c = online(monkeypatch, '{"headline": "unused"}')
    c.briefs["macro"] = {"headline": "JOLTS 09:00", "day": str(D), "ts": e.feed.now(), "size_multiplier": 1.0,
                         "fed": {"bias": "hawkish", "headline": "Waller at 12:00 CT", "notes": ["2Y pricing one cut"]},
                         "rates": {"headline": "10Y +4bp to 4.21%", "notes": ["7Y auction 12:00"]}}
    fed = run(c._brief("fed", "premarket", e.feed.now()))
    rates = run(c._brief("rates", "premarket", e.feed.now()))
    assert c._client.messages.calls == []
    assert fed["headline"] == "Waller at 12:00 CT" and fed["bias"] == "bearish" and fed["size_multiplier"] == 1.0
    assert rates["headline"] == "10Y +4bp to 4.21%" and rates["size_multiplier"] == 1.0


def test_macro_prompt_covers_the_fed_and_rates(monkeypatch):
    e, c = online(monkeypatch, '{"headline": "Quiet day"}')
    run(c._llm("macro", e.feed.now()))
    system = c._client.messages.calls[0]["system"][0]["text"]
    assert '"fed"' in system and '"rates"' in system and "hawkish" in system


# ----------------------------------------------------------------------------- 7. Vol from Robinhood data
def test_vol_premarket_makes_one_web_call_for_vix1d_and_passes_robinhood_numbers(monkeypatch):
    reply = json.dumps({"headline": "VIX1D 14.1 vs VIX 16.0", "bias": "bullish", "confidence": 0.65,
                        "size_multiplier": 1.1, "vix1d_flag": False})
    e, c = online(monkeypatch, reply)
    b = run(c._brief("vol", "premarket", e.feed.now()))
    assert len(web_calls(c)) == 1
    assert "VIX 16.00" in user_text(c._client.messages.calls[0])
    assert b["vix1d_flag"] is False and b["size_multiplier"] == 1.1 and b["vix"] == 16.0 and b["em"] > 0


def test_vol_later_reads_are_deterministic_and_keep_the_premarket_vote_and_flag(monkeypatch):
    e, c = online(monkeypatch, '{"headline": "unused"}', now=at_ct(D, time(11, 30)))
    c.briefs["vol"] = {"headline": "am", "day": str(D), "ts": at_ct(D, time(8, 25)), "bias": "bullish",
                       "confidence": 0.7, "size_multiplier": 1.15, "vix1d_flag": True}
    b = run(c._brief("vol", "midday", e.feed.now()))
    assert c._client.messages.calls == []
    assert b["size_multiplier"] == 1.15 and b["confidence"] == 0.7 and b["vix1d_flag"] is True
    b = run(c._brief("vol", "postclose", at_ct(D, time(15, 5))))
    assert c._client.messages.calls == []                                   # 5. no 15:05 web call


def test_vol_cuts_to_75_when_vix_is_above_28(monkeypatch):
    e, c = online(monkeypatch, '{"headline": "unused"}', now=at_ct(D, time(11, 30)))
    c.vix = FakeVix(current=31.2)
    c.briefs["vol"] = {"headline": "am", "day": str(D), "ts": at_ct(D, time(8, 25)), "size_multiplier": 1.15,
                       "confidence": 0.7, "bias": "bullish"}
    b = run(c._brief("vol", "midday", e.feed.now()))
    assert b["size_multiplier"] == 0.75 and "31.2" in b["headline"]


def test_expected_move_comes_from_the_recorded_atm_straddle_during_the_session(monkeypatch):
    now = at_ct(D, time(11, 30))
    e, c = online(monkeypatch, '{"headline": "unused"}', now=now)
    e.journal = Journal(None)
    e.price = 765.2
    e.journal.record_quotes([(now - 20, str(D), 765.0, "call", 1.40, 1.44, 765.2),
                             (now - 20, str(D), 765.0, "put", 1.20, 1.24, 765.2),
                             (now - 20, str(D), 766.0, "call", 0.90, 0.94, 765.2)])
    b = run(c._brief("vol", "midday", now))
    assert abs(b["em"] - 2.64) < 1e-9 and "straddle" in b["headline"].lower()


# ----------------------------------------------------------------------------- 6. weekly event calendar
WEEK = json.dumps({"events": [
    {"date": "2026-09-28", "time_ct": "09:00", "name": "Pending home sales", "impact": "medium"},
    {"date": "2026-09-29", "time_ct": "09:00", "name": "JOLTS", "impact": "high"},
    {"date": "2026-09-30", "time_ct": "07:15", "name": "ADP", "impact": "high"}]})


def test_calendar_is_fetched_once_a_week_and_saved(monkeypatch, tmp_path):
    e, c = online(monkeypatch, WEEK, tmp=tmp_path)
    c.cfg["calendar_path"] = str(tmp_path / "econ_calendar.json")
    run(c._ensure_calendar(at_ct(D, time(8, 15))))
    saved = json.loads((tmp_path / "econ_calendar.json").read_text())
    assert saved["week"] == "2026-09-28" and len(saved["events"]) == 3
    assert len(web_calls(c)) == 1
    tue = date(2026, 9, 29)
    c.day = tue
    run(c._ensure_calendar(at_ct(tue, time(8, 15))))
    assert len(web_calls(c)) == 1                        # same week: read from the file
    assert [b.name for b in e.risk.st.blackouts] == ["JOLTS"]


def test_calendar_check_logs_disagreements_and_blacks_out_both(monkeypatch, tmp_path):
    tue = date(2026, 9, 29)
    now = at_ct(tue, time(8, 25))
    e, c = online(monkeypatch, WEEK, now=now, tmp=tmp_path)
    e.journal = Journal(None)
    c.day = tue
    c.cfg["calendar_path"] = str(tmp_path / "econ_calendar.json")
    run(c._ensure_calendar(now))
    c.briefs["macro"] = {"headline": "x", "day": str(tue), "ts": now, "size_multiplier": 1.0,
                         "events": [{"time_ct": "13:00", "name": "Fed speaker Waller", "impact": "high"}]}
    c._apply(now)
    names = {b.name for b in e.risk.st.blackouts}
    assert names == {"JOLTS", "Fed speaker Waller"}
    rows = [r for r in e.journal.crew_log(str(tue)) if r["kind"] == "calendar_check"]
    assert rows and rows[-1]["detail"]["only_calendar"] == ["JOLTS 09:00"]
    assert rows[-1]["detail"]["only_macro"] == ["Fed speaker Waller 13:00"]


# ----------------------------------------------------------------------------- 9. votes by book
def test_votes_reach_only_the_books_their_topic_affects():
    e, c = make()
    day = str(D)
    c.briefs = {"macro": {"day": day, "size_multiplier": 0.5}, "vol": {"day": day, "size_multiplier": 1.0},
                "quant": {"day": day, "size_multiplier": 1.0}}
    c._apply(e.feed.now())
    r = e.risk
    assert r.book_mult("A") == 0.5 and r.book_mult("C") == 0.5
    assert r.book_mult("B") == 1.0 and r.book_mult("D") == 1.0 and r.book_mult("E") == 1.0
    assert r.st.size_mult == 0.5                        # book A's multiplier, as before
    c.briefs["macro"]["size_multiplier"], c.briefs["vol"]["size_multiplier"] = 1.0, 0.75
    c._apply(e.feed.now())
    assert r.book_mult("B") == 0.75 and r.book_mult("A") == 0.75 and r.book_mult("E") == 1.0 and r.book_mult("F") == 1.0
    assert c.cut_by("B") == ["vol"] and c.cut_by("E") == []


def test_a_cut_routed_away_from_book_a_leaves_a_alone():
    cfg = copy.deepcopy(CFG)
    cfg["crew"]["vote_books"] = {**cfg["crew"]["vote_books"], "macro": ["C"]}
    e, c = make(cfg=cfg)
    c.briefs = {"macro": {"day": str(D), "size_multiplier": 0.5}}
    c._apply(e.feed.now())
    assert e.risk.book_mult("A") == 1.0 and e.risk.book_mult("C") == 0.5
    assert c.cut_by("A") == []


def test_unlisted_desks_still_cut_book_a_and_info_only_desks_reach_nothing():
    e, c = make()
    day = str(D)
    c.briefs = {"risk": {"day": day, "size_multiplier": 0.75}, "fed": {"day": day, "size_multiplier": 0.5}}
    c._apply(e.feed.now())
    assert e.risk.book_mult("A") == 0.75 and e.risk.book_mult("B") == 1.0
    assert c.cut_by("A") == ["risk"]


def test_desk_prompts_describe_every_book_and_the_books_a_vote_reaches(monkeypatch):
    e, c = online(monkeypatch, '{"headline": "Quiet day"}')
    run(c._llm("macro", e.feed.now()))
    text = user_text(c._client.messages.calls[0])
    assert "B iron fly" in text and "E pre-earnings" in text
    assert "Your size vote applies to books A, C" in text


def test_book_multipliers_survive_a_restart_and_can_only_restrict(tmp_path):
    store = RiskStore(tmp_path / "risk.json")
    r = RiskManager(CFG, store)
    r.restore("2026-09-28")
    r.set_book_mults({"A": 0.5, "C": 0.75})
    r2 = RiskManager(CFG, RiskStore(tmp_path / "risk.json"))
    import json as _j
    d = _j.loads((tmp_path / "risk.json").read_text())
    d["book_mults"]["C"] = 1.3
    (tmp_path / "risk.json").write_text(_j.dumps(d))
    r2.restore(d["day"])
    assert r2.book_mult("A") == 0.5 and r2.book_mult("C") == 1.0


# ----------------------------------------------------------------------------- 1. crew effects logged
def test_each_huddle_logs_its_directive_with_revised_votes():
    e, c = make(now=at_ct(D, time(8, 25)))
    e.journal = Journal(None)

    async def talk(slot, desks, now):
        return {"lines": [], "votes": {"macro": 0.8}, "proposals": []}
    c._roundtable = talk
    run(c._consult("premarket", PREMARKET_DESKS, at_ct(D, time(8, 25))))
    rows = [r for r in e.journal.crew_log(str(D)) if r["kind"] == "directive"]
    d = rows[-1]["detail"]
    assert d["slot"] == "premarket" and d["revised"] == {"macro": 0.8}
    assert d["book_mults"]["A"] == 0.8 and d["votes"]["macro"] == 0.8


def test_blocks_are_logged_once_per_book_and_reason_with_the_desk_that_sourced_them():
    e, c = make(now=at_ct(D, time(8, 25)))
    e.journal = Journal(None)
    c.briefs["macro"] = {"day": str(D), "size_multiplier": 1.0, "ts": e.feed.now(),
                         "events": [{"time_ct": "09:00", "name": "JOLTS", "impact": "high"}]}
    c._apply(e.feed.now())
    for _ in range(3):
        c.note_block("B", "blackout: JOLTS", at_ct(D, time(8, 55)))
    c.note_block("B", "Vol desk: VIX1D more than 3 points above VIX", at_ct(D, time(8, 45)))
    rows = [r for r in e.journal.crew_log(str(D)) if r["kind"] == "block"]
    assert [(r["book"], r["detail"]["desk"]) for r in rows] == [("B", "macro"), ("B", "vol")]


def test_entry_effect_records_quantity_at_1x_cutting_desks_and_tweaks():
    e, c = make()
    c.briefs = {"macro": {"day": str(D), "size_multiplier": 0.5}}
    c._apply(e.feed.now())
    fx = c.effect("A", qty=2, qty_1x=5, up=1.0, tweaks={"exits.stop_loss_pct": 0.15})
    assert fx == {"mult": 0.5, "qty": 2, "qty_1x": 5, "up": 1.0, "cut_by": ["macro"],
                  "tweaks": {"exits.stop_loss_pct": 0.15}}


def test_journal_saves_the_crew_effect_with_the_trade():
    j = Journal(None)
    p = Position(Contract("SPY", "2026-09-28", 765.0, "call"), "SWING", 2, 1.0, 0.0)
    p.closed_ts, p.realized = 60.0, 40.0
    p.crew = {"mult": 0.5, "qty": 2, "qty_1x": 5}
    j.record_trade("2026-09-28", "paper", p)
    row = j.db.execute("SELECT crew FROM trades").fetchone()[0]
    assert json.loads(row)["qty_1x"] == 5


def test_postclose_logs_the_days_usage():
    e, c = make(now=at_ct(D, time(15, 5)))
    e.journal = Journal(None)
    c.cache_usage["est_cost_usd"] = 0.42
    run(c._consult("postclose", ["quant", "vol", "postmortem"], at_ct(D, time(15, 5))))
    rows = [r for r in e.journal.crew_log(str(D)) if r["kind"] == "usage"]
    assert rows and rows[-1]["detail"]["est_cost_usd"] == 0.42


# ----------------------------------------------------------------------------- 11. model
def test_crew_runs_sonnet_5_5_and_keeps_haiku_for_quant_and_the_roundtable():
    assert CFG["crew"]["model"] == "claude-sonnet-5-5"
    assert CFG["crew"]["fast_model"].startswith("claude-haiku-4-5")


# ----------------------------------------------------------------------------- 1. weekly scorecard
def test_weekly_report_scores_the_crew(tmp_path):
    from reporting import weekly_quant as wq
    path = tmp_path / "journal.db"
    j = Journal(path)
    day = "2026-09-28"

    def trade(pnl, crew):
        p = Position(Contract("SPY", day, 765.0, "call"), "SWING", crew["qty"], 1.0, at_ct(D, time(9, 0)))
        p.closed_ts, p.realized, p.crew = at_ct(D, time(9, 30)), pnl, crew
        j.record_trade(day, "paper", p)
    trade(-40.0, {"qty": 2, "qty_1x": 5, "up": 1.0, "cut_by": ["macro"], "tweaks": {}})        # cut saved $60
    trade(60.0, {"qty": 6, "qty_1x": 5, "up": 1.2, "cut_by": [], "tweaks": {"exits.stop_loss_pct": 0.15}})  # up +$10
    j.record_crew(day, at_ct(D, time(8, 55)), "block", "B", {"reason": "blackout: JOLTS", "desk": "macro"})
    j.record_crew(day, at_ct(D, time(15, 5)), "usage", None, {"est_cost_usd": 0.31, "web_searches": 7})
    rep = wq.build_report(path, date(2026, 10, 2))
    c = rep["crew"]["week"]
    assert abs(c["sizing"]["macro"]["delta"] - 60.0) < 1e-9 and abs(c["sizing"]["size-up gate"]["delta"] - 10.0) < 1e-9
    assert c["tweaks"]["with"] == {"trades": 1, "net": 60.0} and c["blocks"] == {"macro -> B": 1}
    assert c["est_cost_usd"] == 0.31 and c["web_searches"] == 7
    md = wq.render_markdown(rep)
    assert "## Crew scorecard" in md and "| macro | 1 | $60 |" in md
