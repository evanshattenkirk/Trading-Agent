"""Events from a desk's web-search brief are bounded (review plan 2026-10-06, M8; Evan's decision D4): at most four
a desk a day, only times still ahead, citation markup stripped, and they block entries but never flatten. Only an
event in the weekly calendar or config gets a flatten time."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from books_fakes import DAY, ct_ts
from test_book_b_morning_events import desk, macro, run


def test_brief_events_are_capped_per_desk_per_day(tmp_path, monkeypatch):
    six = [(f"{h:02d}:00", f"Event {h}", "high") for h in (9, 10, 11, 12, 13, 14)]
    eng, host, book = desk(tmp_path, monkeypatch, macro(*six), macro(("14:30", "Event 14:30", "high")), now=ct_ts(8, 25))
    run(eng.crew._consult("premarket", ["macro"], ct_ts(8, 25)))
    assert [b.name for b in eng.risk.st.blackouts] == ["Event 9", "Event 10", "Event 11", "Event 12"]
    eng.feed.t = ct_ts(11, 30)
    run(eng.crew._consult("midday", ["macro"], ct_ts(11, 30)))
    assert len(eng.risk.st.blackouts) == 4                     # the day's four are spent


def test_brief_events_must_be_in_the_future(tmp_path, monkeypatch):
    eng, host, book = desk(tmp_path, monkeypatch, macro(("11:25", "Just out", "high"), ("13:00", "Ahead", "high")),
                           now=ct_ts(11, 30))
    run(eng.crew._consult("midday", ["macro"], ct_ts(11, 30)))
    assert [b.name for b in eng.risk.st.blackouts] == ["Ahead"]
    assert eng.risk.must_flatten(ct_ts(11, 30)) is None
    assert eng.risk.can_enter(ct_ts(11, 31), 0)[1] != "blackout: Just out"


def test_brief_only_events_block_entries_but_never_flatten(tmp_path, monkeypatch):
    eng, host, book = desk(tmp_path, monkeypatch, macro(("13:00", "Fed speaker Waller", "high")), now=ct_ts(8, 25))
    run(eng.crew._consult("premarket", ["macro"], ct_ts(8, 25)))
    (b,) = eng.risk.st.blackouts
    assert b.flatten_at is None
    assert eng.risk.must_flatten(ct_ts(12, 56)) is None
    assert eng.risk.can_enter(ct_ts(12, 56), 0)[1] == "blackout: Fed speaker Waller"


def test_calendar_and_config_events_still_flatten_and_confirm_a_brief_event(tmp_path, monkeypatch):
    cal = [{"date": str(DAY), "time_ct": "13:00", "name": "Fed chair speaks", "impact": "high"}]
    eng, host, book = desk(tmp_path, monkeypatch, macro(("13:00", "Powell", "high")), calendar=cal,
                           events=[{"date": str(DAY), "time": "10:00", "name": "ISM", "impact": "high"}], now=ct_ts(8, 25))
    run(eng.crew._consult("premarket", ["macro"], ct_ts(8, 25)))   # Macro's brief lands before the calendar
    assert eng.risk.must_flatten(ct_ts(12, 56)) is None
    run(eng.crew._ensure_calendar(ct_ts(8, 26)))                # the calendar has the same event: it may flatten
    assert eng.risk.must_flatten(ct_ts(12, 56)) == "flatten before Powell"
    assert eng.risk.must_flatten(ct_ts(9, 56)) == "flatten before ISM"
    assert len(eng.risk.st.blackouts) == 2


def test_cite_markup_is_stripped_from_event_names_and_pitches(tmp_path, monkeypatch):
    eng, host, book = desk(tmp_path, monkeypatch)
    b = eng.crew._finish({"headline": "x", "events": [{"time_ct": "13:00", "impact": "high",
                                                        "name": "<cite index='3-1'>FOMC minutes</cite>"}],
                          "proposals": [{"scope": "standing", "title": "<cite index='1-1'>Wider trail</cite>",
                                         "rationale": "Runners <cite index='2-4'>gave back 40%</cite>",
                                         "evidence": "<cite index='5-2'>3 of 4</cite> runners", "params": {}}]}, "macro")
    assert b["events"][0]["name"] == "FOMC minutes"
    p = b["proposals"][0]
    assert (p["title"], p["rationale"], p["evidence"]) == ("Wider trail", "Runners gave back 40%", "3 of 4 runners")
