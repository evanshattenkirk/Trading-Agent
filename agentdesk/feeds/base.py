"""Feed + quote interfaces shared by the simulator and the live vendors."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import AsyncIterator

from ..bars import Bar, Trade


@dataclass
class Heartbeat:
    ts: float


@dataclass
class Quote:
    bid: float
    ask: float
    ts: float

    @property
    def mark(self) -> float:
        return round((self.bid + self.ask) / 2, 3)

    @property
    def spread_pct(self) -> float:
        m = self.mark
        return (self.ask - self.bid) / m if m > 0 else 1.0


class Feed:
    """Streams SPY trades (and heartbeats) and serves 1m history for warm-up."""
    is_sim = False
    name = "feed"

    async def history_1m(self, days: int) -> list[Bar]:
        return []

    def stream(self) -> AsyncIterator[Trade | Heartbeat]:
        raise NotImplementedError

    def now(self) -> float:
        return time.time()

    async def close(self) -> None:
        pass


class QuoteSource:
    name = "quotes"

    async def quote(self, contract) -> Quote | None:
        raise NotImplementedError

    async def start(self) -> None:
        pass


async def heartbeat_into(q: asyncio.Queue, every: float = 1.0) -> None:
    while True:
        await q.put(Heartbeat(time.time()))
        await asyncio.sleep(every)
