"""Fakes for book E and the IV recorder: a chain source with fixed expirations, strikes and quotes."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.exits import Contract
from agentdesk.iv import IVQuote


class FakeChains:
    def __init__(self, now: float = 0.0):
        self.now = now
        self.exps: dict[str, set] = {}
        self.listed: dict[tuple, set] = {}
        self.book: dict[tuple, tuple] = {}      # (symbol, expiry str, strike, right) -> (bid, ask, iv)
        self.px: dict[str, float] = {}
        self.calls: list[str] = []
        self.fail: set = set()                  # method names that raise
        self.pacer = None

    def chain(self, sym: str, exp: date, strikes) -> None:
        self.exps.setdefault(sym, set()).add(exp)
        self.listed[(sym, exp)] = {float(k) for k in strikes}

    def set(self, sym, exp, strike, right, bid, ask, iv=None) -> None:
        self.book[(sym, str(exp), float(strike), right)] = (bid, ask, iv)

    def drop(self, sym, exp, strike, right) -> None:
        self.book.pop((sym, str(exp), float(strike), right), None)

    def _hit(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(f"{name} failed")

    async def spots(self, syms):
        self._hit("spots")
        return {s: self.px[s] for s in syms if s in self.px}

    async def expirations(self, sym):
        self._hit("expirations")
        return sorted(self.exps.get(sym, set()))

    async def strikes(self, sym, exp):
        self._hit("strikes")
        return set(self.listed.get((sym, exp), set()))

    def contract(self, sym, exp, strike, right):
        return Contract(sym, str(exp), float(strike), right, f"{sym}|{exp}|{float(strike):g}|{right}")

    def _q(self, c):
        v = self.book.get((c.symbol, c.expiry, float(c.strike), c.right))
        return None if v is None else IVQuote(v[0], v[1], self.now, v[2])

    async def quotes(self, cs):
        self._hit("quotes")
        return [self._q(c) for c in cs]

    async def quote(self, c):
        return self._q(c)
