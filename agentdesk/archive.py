"""Saves each real session for the after-hours review page (python -m agentdesk review).

Per session day, in `review.sessions_dir`:
- YYYY-MM-DD.snapshot.json: engine.snapshot(), the same payload the dashboard loads on connect. Rewritten every
  `review.snapshot_every_sec` and once more at shutdown (after open positions are sold), via a temp file and a
  rename so a crash never leaves half a file.
- YYYY-MM-DD.events.jsonl: the dashboard's feed events (no ticks or bars), appended as they happen, so the
  review page can rebuild the Activity tab. A restart appends to the same file.
Read-only for trading: nothing here feeds back into the engine, and a write error is logged, never raised.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import deque
from pathlib import Path
from typing import IO

from .clock import session_date

log = logging.getLogger("agentdesk.archive")

# what app.js turns into Activity lines (plus the state those handlers touch; the snapshot overrides that state)
FEED_EVENTS = frozenset({"order", "fill", "position", "trade_closed", "skip", "crew", "proposal", "log",
                         "book_order", "book_position", "book_closed", "book_skip", "f_position"})
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SNAP_SUFFIX, EVENTS_SUFFIX = ".snapshot.json", ".events.jsonl"


def _default(o):
    return round(o, 4) if isinstance(o, float) else str(o)


class SessionArchive:
    """Writes the day's snapshot file."""

    def __init__(self, directory: Path | str):
        self.dir = Path(directory)

    def save(self, engine) -> Path | None:
        try:
            snap = engine.snapshot()
            day = str(snap.get("day") or "")
            if not DAY_RE.match(day):
                return None                      # the engine hasn't started its session yet
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / f"{day}{SNAP_SUFFIX}"
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(snap, separators=(",", ":"), default=_default))
            os.replace(tmp, path)
            return path
        except Exception as ex:
            log.warning("session snapshot not saved: %r", ex)
            return None

    async def run(self, engine, every: float) -> None:
        """Saves every `every` seconds until cancelled."""
        while True:
            await asyncio.sleep(every)
            self.save(engine)


class EventLog:
    """A Bus tap: appends feed events to the events file of the event's session day."""

    def __init__(self, directory: Path | str, types: frozenset[str] = FEED_EVENTS):
        self.dir = Path(directory)
        self.types = types
        self.day: str | None = None
        self.fh: IO | None = None
        self.failed = False

    def __call__(self, msg: dict) -> None:
        if msg.get("type") not in self.types:
            return
        try:
            day = str(session_date(float(msg["ts"])))
            if day != self.day or self.fh is None:
                self.close()
                self.dir.mkdir(parents=True, exist_ok=True)
                self.fh = open(self.dir / f"{day}{EVENTS_SUFFIX}", "a", buffering=1)
                self.day = day
            self.fh.write(json.dumps(msg, separators=(",", ":"), default=_default) + "\n")
            self.failed = False
        except Exception as ex:
            if not self.failed:                  # say it once, not on every event
                log.warning("session event log not written: %r", ex)
            self.failed = True
            self.close()

    def close(self) -> None:
        if self.fh is not None:
            try:
                self.fh.close()
            except Exception:
                pass
        self.fh = None


def list_days(directory: Path | str) -> list[str]:
    """Days with a saved snapshot, newest first."""
    d = Path(directory)
    if not d.is_dir():
        return []
    days = {p.name[:-len(SNAP_SUFFIX)] for p in d.iterdir() if p.name.endswith(SNAP_SUFFIX)}
    return sorted((x for x in days if DAY_RE.match(x)), reverse=True)


def load_day(directory: Path | str, day: str | None = None, max_events: int = 3000) -> dict | None:
    """The saved snapshot and the last `max_events` feed events of `day` (default: the newest), or None."""
    d = Path(directory)
    days = list_days(d)
    if day is None:
        day = days[0] if days else None
    if day not in days:
        return None
    snap_path = d / f"{day}{SNAP_SUFFIX}"
    try:
        snapshot = json.loads(snap_path.read_text())
        saved_ts = snap_path.stat().st_mtime
    except (OSError, ValueError) as ex:
        log.warning("saved session %s unreadable: %r", day, ex)
        return None
    events: deque = deque(maxlen=max_events)
    ev_path = d / f"{day}{EVENTS_SUFFIX}"
    if ev_path.exists():
        with open(ev_path) as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue                     # a line cut off by a kill
    return {"day": day, "days": days, "saved_ts": saved_ts, "snapshot": snapshot, "events": list(events)}


DEFAULTS = {"sessions_dir": "~/.agentdesk/sessions", "claims_dir": "~/.agentdesk/claims", "snapshot_every_sec": 60,
            "modes": ["paper", "shadow", "live"]}


def review_cfg(cfg) -> dict:
    return {**DEFAULTS, **((cfg or {}).get("review") or {})}


def attach(cfg, mode: str, bus) -> SessionArchive | None:
    """For a run in one of `review.modes`: taps the bus for feed events and returns the snapshot writer."""
    rv = review_cfg(cfg)
    if mode not in rv["modes"]:
        return None
    d = Path(os.path.expanduser(rv["sessions_dir"]))
    bus.taps.append(EventLog(d))
    return SessionArchive(d)
