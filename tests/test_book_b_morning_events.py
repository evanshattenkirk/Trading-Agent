"""Book B skips days with a high-impact event before 14:00 CT, including the 07:30 CT prints (CPI, PPI, payrolls) the
crew only hears of at 08:10-08:25 (review plan 2026-10-06, H1). A print that has passed blocks no entry and stays off
the dashboard's live blackouts. Events from a desk's brief are bounded, future-only and never flatten (M8, D4), and
the one-blackout-per-event-window rule is the only de-dup (L5)."""
import asyncio
import copy
import json
import sys
from datetime import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from books_fakes import DAY, FakeEngine, FakeQuotes, ct_ts
from agentdesk.books.book import Book
from agentdesk.books.host import BookHost
from agentdesk.books.iron_fly import IronFly
from agentdesk.config import load_config
from agentdesk.crew import Crew

CFG = load_config()
SKIP = "high-impact event before 14:00 CT"


def run(c):
    return asyncio.run(c)


class FakeMessages:
    def __init__(self, *texts):
        self.texts, self.calls = list(texts), []

    async def create(self, **kw):
        self.calls.append(kw)
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", usage=None)


def desk(tmp_path, monkeypatch, *replies, events=(), calendar=None, now=None):
    """A paper desk at 08:10 CT: the real crew and risk manager, and book B on the real host."""
    cfg = copy.deepcopy(CFG)
    cfg["journal_path"] = str(tmp_path / "journal.db")
    cfg["crew"]["calendar_path"] = str(tmp_path / "econ_calendar.json")
    cfg["crew"]["events"] = list(events)
    if calendar is not None:
        (tmp_path / "econ_calendar.json").write_text(json.dumps({"week": "2026-09-28", "events": calendar}))
    if replies:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fq = FakeQuotes(now=now or ct_ts(8, 10))
    for right, k, bid, ask in (("call", 765, 2.00, 2.02), ("put", 765, 1.90, 1.92), ("call", 770, 0.40, 0.41),
                               ("put", 760, 0.35, 0.36)):
        fq.set(right, k, bid, ask)
    eng = FakeEngine(fq, cfg)
    eng.feed.t = now or ct_ts(8, 10)
    eng.set_agent = lambda now, activity, text="": None
    eng.l2 = eng.l2_rh = None
    eng.closed, eng.bars = [], {"1m": []}
    eng.crew = Crew(eng, cfg)
    if replies:
        eng.crew._client = SimpleNamespace(messages=FakeMessages(*replies))
    run(eng.crew.on_clock(ct_ts(8, 10)))             # the engine's first tick: config events load, the day starts
    cb = cfg["books"]["B_iron_fly"]
    book = Book("B_iron_fly", cb, IronFly(cb))
    return eng, BookHost(eng, cfg, books=[book]), book


def at_0845(eng, host):
    eng.quotes.now = eng.feed.t = ct_ts(8, 45)
    run(host.on_second(ct_ts(8, 45)))


def macro(*events, **kw):
    return json.dumps({"headline": "Busy morning", "bias": "neutral", "confidence": 0.6, "size_multiplier": 1.0,
                       "events": [{"time_ct": t, "name": n, "impact": i} for t, n, i in events], **kw})


# ----------------------------------------------------------------------------- H1: 07:30 prints reach book B
def test_a_0730_config_event_loaded_at_0810_skips_book_b(tmp_path, monkeypatch):
    eng, host, book = desk(tmp_path, monkeypatch, events=[{"date": str(DAY), "time": "07:30", "name": "CPI",
                                                           "impact": "high"}])
    at_0845(eng, host)
    assert book.skips and book.skips[-1]["why"] == f"{SKIP}: CPI"
    assert not book.open


def test_a_0730_print_from_the_weekly_calendar_at_0815_skips_book_b(tmp_path, monkeypatch):
    cal = [{"date": str(DAY), "time_ct": "07:30", "name": "Employment Situation", "impact": "high"}]
    eng, host, book = desk(tmp_path, monkeypatch, calendar=cal)
    run(eng.crew._ensure_calendar(ct_ts(8, 15)))
    at_0845(eng, host)
    assert book.skips[-1]["why"] == f"{SKIP}: Employment Situation"


def test_a_0730_print_in_macros_0825_brief_skips_book_b(tmp_path, monkeypatch):
    eng, host, book = desk(tmp_path, monkeypatch, macro(("07:30", "PPI (Sep)", "high"), ("15:00", "Late speaker", "high")),
                           now=ct_ts(8, 25))
    run(eng.crew._consult("premarket", ["macro"], ct_ts(8, 25)))
    at_0845(eng, host)
    assert book.skips[-1]["why"] == f"{SKIP}: PPI (Sep)"
    assert not book.open


def test_a_passed_print_blocks_no_entry_and_stays_off_the_live_blackouts(tmp_path, monkeypatch):
    eng, host, book = desk(tmp_path, monkeypatch, events=[{"date": str(DAY), "time": "07:30", "name": "CPI",
                                                           "impact": "high"}])
    assert eng.risk.to_dict()["blackouts"] == []               # the dashboard lists blackouts, not history
    assert "CPI" not in eng.crew.directive["blackouts"]
    assert eng.risk.can_enter(ct_ts(8, 45), 0)[1] != "blackout: CPI"
    assert eng.risk.must_flatten(ct_ts(8, 45)) is None
    ok, why = host._gate(book, ct_ts(8, 45))
    assert ok, why


def test_book_b_still_trades_a_day_whose_only_event_is_after_1400(tmp_path, monkeypatch):
    eng, host, book = desk(tmp_path, monkeypatch, events=[{"date": str(DAY), "time": "14:30", "name": "Late auction",
                                                           "impact": "high"}])
    at_0845(eng, host)
    assert book.open and not book.skips


def test_reset_day_drops_yesterdays_passed_events():
    from agentdesk.risk import RiskManager
    r = RiskManager(copy.deepcopy(CFG))
    r.add_blackout(ct_ts(7, 30), "CPI", added_ts=ct_ts(8, 10))
    assert [n for _, n in r.st.passed_events] == ["CPI"]
    r.reset_day("2026-09-29")
    assert r.st.passed_events == []


# ----------------------------------------------------------------------------- L5: the window is the only de-dup
def test_a_mistimed_calendar_event_does_not_hide_macros_correctly_timed_one(tmp_path, monkeypatch):
    cal = [{"date": str(DAY), "time_ct": "12:00", "name": "FOMC rate decision", "impact": "high"}]
    eng, host, book = desk(tmp_path, monkeypatch, macro(("13:00", "FOMC rate decision", "high")), calendar=cal,
                           now=ct_ts(8, 25))
    run(eng.crew._ensure_calendar(ct_ts(8, 25)))
    run(eng.crew._consult("premarket", ["macro"], ct_ts(8, 25)))
    assert sorted(b.start for b in eng.risk.st.blackouts) == [ct_ts(11, 50), ct_ts(12, 50)]
    assert eng.risk.can_enter(ct_ts(13, 5), 0)[1] == "blackout: FOMC rate decision"
