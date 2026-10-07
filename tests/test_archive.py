"""The run command saves each real session (snapshot + the dashboard's feed events) for the after-hours review page."""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.archive import FEED_EVENTS, EventLog, SessionArchive, list_days, load_day
from agentdesk.bus import Bus
from agentdesk.clock import CT


def ts(day: str, hhmm: str = "10:00") -> float:
    return datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=CT).timestamp()


class Engine:
    def __init__(self, day="2026-09-29", **extra):
        self.snap = {"type": "snapshot", "ts": ts(day, "15:09"), "day": day, "mode": "paper", **extra}

    def snapshot(self):
        return dict(self.snap)


def test_snapshot_is_written_atomically_under_its_day(tmp_path):
    arc = SessionArchive(tmp_path)
    assert arc.save(Engine(price=765.5)) == tmp_path / "2026-09-29.snapshot.json"
    data = json.loads((tmp_path / "2026-09-29.snapshot.json").read_text())
    assert data["price"] == 765.5 and data["day"] == "2026-09-29"
    assert [p.name for p in tmp_path.iterdir()] == ["2026-09-29.snapshot.json"]    # no temp file left behind


def test_snapshot_before_the_engine_knows_its_day_is_skipped(tmp_path):
    e = Engine()
    e.snap["day"] = "None"
    assert SessionArchive(tmp_path).save(e) is None
    assert list(tmp_path.iterdir()) == []


def test_snapshot_with_odd_values_still_saves(tmp_path):
    from datetime import date
    arc = SessionArchive(tmp_path)
    arc.save(Engine(when=date(2026, 9, 29)))
    assert json.loads((tmp_path / "2026-09-29.snapshot.json").read_text())["when"] == "2026-09-29"


def test_a_failing_snapshot_is_logged_not_raised(tmp_path, caplog):
    class Broken:
        def snapshot(self):
            raise RuntimeError("boom")
    assert SessionArchive(tmp_path).save(Broken()) is None
    assert "boom" in caplog.text


def test_event_log_keeps_feed_events_by_session_day(tmp_path):
    bus = Bus()
    log = EventLog(tmp_path)
    bus.taps.append(log)
    bus.emit("tick", ts("2026-09-29"), price=765.0)                       # not a feed event
    bus.emit("bar", ts("2026-09-29"), bar={"tf": "1m"})
    bus.emit("log", ts("2026-09-29", "09:00"), msg="huddle done")
    bus.emit("book_skip", ts("2026-09-30", "08:45"), book="B", why="event day")
    log.close()
    day1 = [json.loads(x) for x in (tmp_path / "2026-09-29.events.jsonl").read_text().splitlines()]
    day2 = [json.loads(x) for x in (tmp_path / "2026-09-30.events.jsonl").read_text().splitlines()]
    assert [e["type"] for e in day1] == ["log"] and day1[0]["msg"] == "huddle done"
    assert [e["type"] for e in day2] == ["book_skip"]


def test_event_log_appends_across_restarts(tmp_path):
    for msg in ("first run", "second run"):
        log = EventLog(tmp_path)
        log({"type": "log", "ts": ts("2026-09-29"), "msg": msg})
        log.close()
    lines = (tmp_path / "2026-09-29.events.jsonl").read_text().splitlines()
    assert [json.loads(x)["msg"] for x in lines] == ["first run", "second run"]


def test_event_log_never_raises_into_the_bus(tmp_path):
    (tmp_path / "2026-09-29.events.jsonl").mkdir()          # can't open a directory for writing
    bus = Bus()
    seen = []
    bus.taps.append(EventLog(tmp_path))
    bus.taps.append(seen.append)                            # a good tap after the broken one
    bus.emit("log", ts("2026-09-29"), msg="still fine")     # must not raise
    assert [m["msg"] for m in seen] == ["still fine"]


def test_bus_tap_errors_never_reach_the_engine():
    bus = Bus()

    def bad(msg):
        raise ValueError("tap broke")
    seen = []
    bus.taps.append(bad)
    bus.taps.append(seen.append)
    bus.emit("log", 1.0, msg="x")
    assert [m["msg"] for m in seen] == ["x"]


def test_feed_events_cover_what_the_activity_tab_shows():
    assert {"order", "fill", "position", "trade_closed", "skip", "crew", "proposal", "log", "book_order",
            "book_position", "book_closed", "book_skip", "f_position"} <= FEED_EVENTS
    assert not {"tick", "bar", "session", "snapshot", "agent"} & FEED_EVENTS


def test_list_and_load_days(tmp_path):
    arc = SessionArchive(tmp_path)
    arc.save(Engine("2026-09-28", price=760.0))
    arc.save(Engine("2026-09-29", price=765.0))
    log = EventLog(tmp_path)
    log({"type": "log", "ts": ts("2026-09-29"), "msg": "a"})
    log({"type": "log", "ts": ts("2026-09-29"), "msg": "b"})
    log.close()
    (tmp_path / "notes.txt").write_text("ignored")
    assert list_days(tmp_path) == ["2026-09-29", "2026-09-28"]            # newest first

    latest = load_day(tmp_path)
    assert latest["day"] == "2026-09-29" and latest["snapshot"]["price"] == 765.0
    assert [e["msg"] for e in latest["events"]] == ["a", "b"]
    assert latest["days"] == ["2026-09-29", "2026-09-28"] and latest["saved_ts"] > 0

    older = load_day(tmp_path, "2026-09-28")
    assert older["snapshot"]["price"] == 760.0 and older["events"] == []


def test_load_day_caps_events_and_skips_bad_lines(tmp_path):
    SessionArchive(tmp_path).save(Engine())
    with open(tmp_path / "2026-09-29.events.jsonl", "w") as f:
        for i in range(10):
            f.write(json.dumps({"type": "log", "ts": 1.0, "msg": str(i)}) + "\n")
        f.write('{"type": "log", "ts"')                                      # killed mid-write
    got = load_day(tmp_path, max_events=4)["events"]
    assert [e["msg"] for e in got] == ["6", "7", "8", "9"]


def test_load_day_unknown_or_bad_day(tmp_path):
    assert load_day(tmp_path) is None                                       # nothing saved yet
    SessionArchive(tmp_path).save(Engine())
    assert load_day(tmp_path, "2026-01-01") is None
    assert load_day(tmp_path, "../../etc/passwd") is None


def test_missing_dir_lists_nothing(tmp_path):
    assert list_days(tmp_path / "nope") == []


def test_position_mark_updates_are_not_saved(tmp_path):
    """Position updates arrive with every quote (thousands a day); only opens and closes make Activity lines."""
    log = EventLog(tmp_path)
    for kind in ("book_position", "position", "f_position"):
        for ev in ("open", "update", "scale", "closed"):
            log({"type": kind, "ts": ts("2026-09-29"), "event": ev, "pos": {"id": 1}})
    log.close()
    kept = [(e["type"], e["event"]) for e in map(json.loads, (tmp_path / "2026-09-29.events.jsonl").read_text().splitlines())]
    assert kept == [(k, ev) for k in ("book_position", "position", "f_position") for ev in ("open", "closed")]


def test_an_unreadable_newest_snapshot_falls_back_to_the_next_day(tmp_path):
    """A power cut during the 60 s save can leave an empty newest file; the earlier days must still show (L17)."""
    arc = SessionArchive(tmp_path)
    arc.save(Engine("2026-09-28", price=760.0))
    arc.save(Engine("2026-09-29", price=765.0))
    (tmp_path / "2026-09-29.snapshot.json").write_text("")
    got = load_day(tmp_path)
    assert got["day"] == "2026-09-28" and got["snapshot"]["price"] == 760.0
    assert got["days"] == ["2026-09-29", "2026-09-28"]
    assert load_day(tmp_path, "2026-09-29") is None                          # asked for by name: no silent swap


def test_snapshot_is_fsynced_before_the_rename(tmp_path, monkeypatch):
    import agentdesk.archive as archive
    calls = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(archive.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(archive.os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))[1])
    assert SessionArchive(tmp_path).save(Engine(price=765.5)) is not None
    assert calls == ["fsync", "replace"]
    assert json.loads((tmp_path / "2026-09-29.snapshot.json").read_text())["price"] == 765.5


def test_load_day_without_events_never_opens_the_events_file(tmp_path, monkeypatch):
    """The review server's /api/state asks for max_events=0 on every poll; it must not parse the whole log (M17)."""
    import agentdesk.archive as archive
    SessionArchive(tmp_path).save(Engine(price=765.5))
    log = EventLog(tmp_path)
    log({"type": "log", "ts": ts("2026-09-29"), "msg": "a"})
    log.close()

    def no_open(*a, **k):
        raise AssertionError(f"opened {a[0]}")
    monkeypatch.setattr(archive, "open", no_open, raising=False)
    got = load_day(tmp_path, max_events=0)
    assert got["snapshot"]["price"] == 765.5 and got["events"] == []
