"""Level 2 order book monitor (Robinhood get_equity_price_book).

Default mode is OBSERVE: the book is read, scored and logged on every entry, but it never
blocks a trade. There is no historical Level 2 to backtest against, so the only honest way
to find out whether it helps your entries is to record it live and compare outcomes later
(`python -m agentdesk l2-report`). Switch to ENFORCE only after that report says so.

Metrics (computed on each snapshot):
  imbalance   (bid size - ask size) / total, over levels within `near` dollars of mid.  +1 = all bids
  micro_edge  microprice - mid, in cents. Microprice leans toward the thinner side, i.e. where
              the next tick is more likely to go.
  walls       levels whose size is >= wall_mult x the median level size on that side
"""
from __future__ import annotations

import asyncio
import logging
import random
import statistics
import time
from collections import deque
from dataclasses import asdict, dataclass, field


@dataclass
class BookStats:
    ts: float
    bid: float
    ask: float
    mid: float
    imbalance: float
    micro_edge_c: float
    bid_depth: float
    ask_depth: float
    bid_wall: tuple | None = None       # (price, size)
    ask_wall: tuple | None = None
    bids: list = field(default_factory=list)   # [(price, size)] best first, for the UI
    asks: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}

    def short(self) -> str:
        w = f", ask wall {self.ask_wall[0]:.2f} x {self.ask_wall[1] / 1000:.0f}k" if self.ask_wall else ""
        return f"L2 imb {self.imbalance:+.2f}, micro {self.micro_edge_c:+.1f}c{w}"


def _levels(side) -> list[tuple[float, float]]:
    out = []
    for lv in side or []:
        if isinstance(lv, dict):
            p = lv.get("price") or lv.get("price_level")
            q = lv.get("quantity") or lv.get("size") or lv.get("shares")
        else:
            p, q = lv[0], lv[1]
        if p is not None and q is not None:
            out.append((float(p), float(q)))
    return out


def compute(book: dict, ts: float, near: float = 0.25, wall_mult: float = 4.0, wall_range: float = 1.0,
            min_wall: float = 10000) -> BookStats | None:
    bids = sorted(_levels(book.get("bids")), key=lambda x: -x[0])
    asks = sorted(_levels(book.get("asks")), key=lambda x: x[0])
    if not bids or not asks:
        return None
    bb, ba = bids[0], asks[0]
    mid = (bb[0] + ba[0]) / 2
    bd = sum(q for p, q in bids if p >= mid - near)
    ad = sum(q for p, q in asks if p <= mid + near)
    imb = (bd - ad) / (bd + ad) if bd + ad else 0.0
    micro = (bb[0] * ba[1] + ba[0] * bb[1]) / (ba[1] + bb[1])

    def wall(levels, above: bool):
        sizes = [q for _, q in levels[:20]]
        if len(sizes) < 4:
            return None
        med = statistics.median(sizes)
        for p, q in levels:
            if abs(p - mid) > wall_range:
                break
            if q >= wall_mult * med and q >= min_wall:
                return (p, q)
        return None

    return BookStats(ts, bb[0], ba[0], mid, imb, (micro - mid) * 100, bd, ad, wall(bids, False), wall(asks, True),
                     bids[:8], asks[:8])


class L2Monitor:
    def __init__(self, cfg, source=None, symbol: str = "SPY"):
        c = cfg.get("l2") or {}
        self.mode = c.get("mode", "observe")            # off | observe | enforce
        self.poll = c.get("poll_ms", 1000) / 1000
        self.near = c.get("near_dollars", 0.25)
        self.wall_mult = c.get("wall_mult", 4.0)
        self.min_wall = c.get("min_wall_shares", 10000)
        self.min_imb = c.get("min_imbalance_for_calls", -0.25)
        self.wall_block = c.get("block_if_ask_wall_within", 0.15)
        self.source, self.symbol = source, symbol
        self.latest: BookStats | None = None
        self.hist: deque = deque(maxlen=600)
        self._task = None

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def update(self, book: dict, ts: float) -> BookStats | None:
        st = compute(book, ts, self.near, self.wall_mult, 1.0, self.min_wall)
        if st:
            self.latest = st
            self.hist.append(st)
        return st

    def gate(self, side: str, spot: float, now: float) -> tuple[bool, str]:
        st = self.latest
        if st is None or now - st.ts > 5:
            return True, "no fresh book"
        if side == "call":
            if st.imbalance < self.min_imb:
                return False, f"book ask-heavy (imb {st.imbalance:+.2f} < {self.min_imb:+.2f})"
            if st.ask_wall and st.ask_wall[0] - spot <= self.wall_block:
                return False, f"ask wall {st.ask_wall[0]:.2f} x {st.ask_wall[1] / 1000:.0f}k within ${self.wall_block:.2f}"
        else:
            if st.imbalance > -self.min_imb:
                return False, f"book bid-heavy (imb {st.imbalance:+.2f})"
            if st.bid_wall and spot - st.bid_wall[0] <= self.wall_block:
                return False, f"bid wall {st.bid_wall[0]:.2f} within ${self.wall_block:.2f}"
        return True, "book ok"

    async def run_live(self, rh, on_update) -> None:
        last_warn = -1e18
        while True:
            try:
                book = await rh.price_book(self.symbol)
                if book:
                    st = self.update(book, time.time())
                    if st:
                        on_update(st)
            except Exception as ex:                 # observe-only: keep polling, but say why the book went quiet
                if time.time() - last_warn >= 60:
                    last_warn = time.time()
                    logging.getLogger("agentdesk.l2").warning("L2 book read failed (logged once a minute): %s", ex)
            await asyncio.sleep(self.poll)


class SimBook:
    """Random book around the simulated price. It has no predictive power by construction."""

    def __init__(self, seed: int = 3):
        self.rng = random.Random(seed)

    def book(self, px: float) -> dict:
        r = self.rng
        bid0 = round(px - 0.005, 2)
        ask0 = round(bid0 + 0.01, 2)
        def mk(p):
            base = r.lognormvariate(7.4, 0.5)
            if abs(p * 2 - round(p * 2)) < 1e-6 and r.random() < 0.25:      # occasional size on half-dollar prints
                base *= r.uniform(6, 14)
            return max(100, int(base))
        bids = [{"price": f"{bid0 - 0.01 * k:.2f}", "quantity": mk(bid0 - 0.01 * k)} for k in range(40)]
        asks = [{"price": f"{ask0 + 0.01 * k:.2f}", "quantity": mk(ask0 + 0.01 * k)} for k in range(40)]
        return {"bids": bids, "asks": asks}
