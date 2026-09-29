"""HostGroup: the one `engine.books` object when book F (FHost) and book E (EHost) run next to the B/C/D/G BookHost.

The engine keeps calling start / on_bar / on_second / kill / flatten / halt_all / positions / snapshot on one object.
Every host shares one AccountRisk (paper equity, open-risk cap, global halt): F's and E's open risk is added to
BookHost.open_risk, and F and E each check their entries against A + B/C/D/G + F + E. BookHost keeps F and E on the
dashboard's book strip through `extra_books`.
"""
from __future__ import annotations

import asyncio


class HostGroup:
    def __init__(self, bookhost=None, fhost=None, ehost=None):
        self.bookhost, self.fhost, self.ehost = bookhost, fhost, ehost
        self.extras = [h for h in (fhost, ehost) if h is not None]
        self.hosts = [h for h in (bookhost, *self.extras) if h is not None]
        self._inline = True
        if len(self.hosts) > 1:
            lead = bookhost or self.extras[0]
            base = bookhost.open_risk if bookhost is not None else self.extras[0].other_risk    # A (+ B/C/D/G)
            for h in self.extras:
                h.account = lead.account
                h.other_risk = lambda h=h: base() + sum(x.open_risk() for x in self.extras if x is not h)
            if bookhost is not None:
                bookhost.open_risk = lambda: base() + sum(x.open_risk() for x in self.extras)
                bookhost.extra_books = [x.book for x in self.extras]

    @classmethod
    def of(cls, bookhost=None, fhost=None, ehost=None):
        return cls(bookhost if bookhost is not None and bookhost.enabled else None, fhost, ehost)

    @property
    def enabled(self) -> bool:
        return bool(self.hosts)

    @property
    def account(self):
        return self.hosts[0].account

    @property
    def books(self) -> list:
        return (list(self.bookhost.books) if self.bookhost else []) + [h.book for h in self.extras]

    async def _all(self, fn: str, *args) -> None:
        res = await asyncio.gather(*(getattr(h, fn)(*args) for h in self.hosts), return_exceptions=True)
        errs = [r for r in res if isinstance(r, BaseException)]
        if errs:
            raise errs[0]

    async def run(self, coro, inline: bool) -> None:
        """The engine's hook runner. on_bar/on_second hand each host's hook to that host's own run(), so an F error
        never counts toward B/C/D's error halt (and the reverse)."""
        self._inline = inline
        await coro

    async def _each(self, fn: str, *args) -> None:
        for h in self.hosts:
            await h.run(getattr(h, fn)(*args), self._inline)

    async def start(self) -> None:
        for h in self.hosts:
            await h.start()

    async def on_bar(self, bar) -> None:
        await self._each("on_bar", bar)

    async def on_second(self, now: float) -> None:
        await self._each("on_second", now)

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
        if self.bookhost:
            return self.bookhost.open_risk()
        h = self.extras[0]
        return h.other_risk() + h.open_risk()

    def snapshot(self) -> dict:
        if self.bookhost:
            snap = self.bookhost.snapshot()
            snap["books"] = [b for b in snap["books"] if b["book"] not in {x.book.letter for x in self.extras}]
        else:
            e = self.hosts[0].e
            snap = {"books": [], "account": self.account.to_dict(self.open_risk(), e.risk.st.day_pnl)}
        snap["books"] = list(snap["books"]) + [h.book.to_dict() for h in self.extras]
        if self.fhost:
            snap["f"] = self.fhost.detail()
        return snap
