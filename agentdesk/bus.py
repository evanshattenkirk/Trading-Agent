"""In-process pub/sub for the dashboard websocket, with an optional JSONL recorder and taps (the session archive)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import IO, Callable

log = logging.getLogger("agentdesk.bus")


class Bus:
    def __init__(self):
        self.subs: set[asyncio.Queue] = set()
        self.recorder: IO | None = None
        self.record_filter: set[str] | None = None
        self.taps: list[Callable[[dict], None]] = []     # called with every event; an error in one never reaches the emitter
        self.overflowed: set[asyncio.Queue] = set()      # subscribers that lost events; their socket resyncs from a snapshot

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)
        self.overflowed.discard(q)

    def take_overflow(self, q: asyncio.Queue) -> bool:
        """True once after `q` filled up and dropped events (a tab that fell behind)."""
        if q in self.overflowed:
            self.overflowed.discard(q)
            return True
        return False

    def emit(self, type_: str, ts: float, **data) -> None:
        msg = {"type": type_, "ts": round(ts, 3), **data}
        for q in list(self.subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                self.overflowed.add(q)
        if self.recorder and (self.record_filter is None or type_ in self.record_filter):
            self.recorder.write(json.dumps(msg, separators=(",", ":"), default=_round) + "\n")
        for tap in self.taps:
            try:
                tap(msg)
            except Exception:
                log.exception("bus tap failed on %s", type_)


def _round(o):
    if isinstance(o, float):
        return round(o, 4)
    return str(o)
