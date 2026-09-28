"""In-process pub/sub for the dashboard websocket, with an optional JSONL recorder."""
from __future__ import annotations

import asyncio
import json
from typing import IO


class Bus:
    def __init__(self):
        self.subs: set[asyncio.Queue] = set()
        self.recorder: IO | None = None
        self.record_filter: set[str] | None = None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)

    def emit(self, type_: str, ts: float, **data) -> None:
        msg = {"type": type_, "ts": round(ts, 3), **data}
        for q in list(self.subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass
        if self.recorder and (self.record_filter is None or type_ in self.record_filter):
            self.recorder.write(json.dumps(msg, separators=(",", ":"), default=_round) + "\n")


def _round(o):
    if isinstance(o, float):
        return round(o, 4)
    return str(o)
