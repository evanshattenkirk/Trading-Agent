"""HostGroup: the one `engine.books` object when book F (FHost) runs next to the B/C/D BookHost.

The engine keeps calling start / on_bar / on_second / kill / flatten / halt_all / positions / snapshot on one object.
F and B/C/D share one AccountRisk (paper equity, open-risk cap, global halt): F's open risk is added to
BookHost.open_risk and F checks its entries against A + B/C/D + F.
"""
from __future__ import annotations

import asyncio


class HostGroup:
    def __init__(self, bookhost=None, fhost=None):
        self.bookhost, self.fhost = bookhost, fhost
        self.hosts = [h for h in (bookhost, fhost) if h is not None]
        if bookhost is not None and fhost is not None:
            fhost.account = bookhost.account
            base = bookhost.open_risk
            fhost.other_risk = base
            bookhost.open_risk = lambda: base() + fhost.open_risk()

    @classmethod
    def of(cls, bookhost=None, fhost=None):
        return cls(bookhost if bookhost is not None and bookhost.enabled else None, fhost)

    @property
    def enabled(self) -> bool:
        return bool(self.hosts)

    @property
    def account(self):
        return self.hosts[0].account

    @property
    def books(self) -> list:
        return (list(self.bookhost.books) if self.bookhost else []) + ([self.fhost.book] if self.fhost else [])

    async def _all(self, fn: str, *args) -> None:
        res = await asyncio.gather(*(getattr(h, fn)(*args) for h in self.hosts), return_exceptions=True)
        errs = [r for r in res if isinstance(r, BaseException)]
        if errs:
            raise errs[0]

    async def start(self) -> None:
        for h in self.hosts:
            await h.start()

    async def on_bar(self, bar) -> None:
        await self._all("on_bar", bar)

    async def on_second(self, now: float) -> None:
        await self._all("on_second", now)

    def halt_all(self, reason: str, flatten: bool = False) -> None:
        for h in self.hosts:
            h.halt_all(reason, flatten)

    async def kill(self, now: float) -> None:
        await self._all("kill", now)

    async def flatten(self, reason: str, now: float) -> None:
        await self._all("flatten", reason, now)

    def positions(self) -> list:
        return [p for h in self.hosts for p in h.positions()]

    def open_risk(self) -> float:
        return self.bookhost.open_risk() if self.bookhost else self.fhost.other_risk() + self.fhost.open_risk()

    def snapshot(self) -> dict:
        if self.bookhost:
            snap = self.bookhost.snapshot()
        else:
            e = self.fhost.e
            snap = {"books": [], "account": self.account.to_dict(self.open_risk(), e.risk.st.day_pnl)}
        if self.fhost:
            snap["books"] = list(snap["books"]) + [self.fhost.book.to_dict()]
            snap["f"] = self.fhost.detail()
        return snap
