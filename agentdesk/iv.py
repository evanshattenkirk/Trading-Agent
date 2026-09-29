"""Single-stock option chains for book E and the IV recorder (HANDOFF 7E; spec
docs/superpowers/specs/2026-09-29-book-e-design.md).

Pure helpers: expiry pickers (front, pre- and post-announcement, ~30 days), the ATM strike, and a call pacer.
RobinhoodChains (below) is the read-only chain data both users share.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date

from .feeds.base import Quote


@dataclass
class IVQuote(Quote):
    iv: float | None = None


def front_expiry(exps, today: date) -> date | None:
    return min((e for e in exps if e > today), default=None)


def d30_expiry(exps, today: date, days: int = 30) -> date | None:
    return min((e for e in exps if e > today), key=lambda e: (abs((e - today).days - days), e), default=None)


def pre_expiry(exps, d: date, timing: str) -> date | None:
    """Last expiry before the announcement: before D for am or unknown timing, on or before D for pm."""
    return max((e for e in exps if (e <= d if timing == "pm" else e < d)), default=None)


def post_expiry(exps, d: date, timing: str) -> date | None:
    """First expiry that carries the announcement: on or after D for am, after D for pm or unknown."""
    return min((e for e in exps if (e >= d if timing == "am" else e > d)), default=None)


def atm_strike(strike_sets: list, spot: float | None) -> float | None:
    """The strike nearest spot that is listed (call and put) in every given expiry; ties go to the lower strike."""
    if not strike_sets or not spot:
        return None
    common = set.intersection(*(set(s) for s in strike_sets))
    return min(common, key=lambda k: (abs(k - spot), k), default=None)


class Pacer:
    """At most `rate` calls per second: wait() sleeps until the next slot."""

    def __init__(self, rate: float, clock=time.monotonic, sleep=asyncio.sleep):
        self.gap, self.clock, self.sleep = 1.0 / rate, clock, sleep
        self.next = -1e18

    async def wait(self) -> None:
        now = self.clock()
        if now < self.next:
            await self.sleep(self.next - now)
            now = self.next
        self.next = now + self.gap
