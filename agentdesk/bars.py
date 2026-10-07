"""Bar builders: clock-aligned time bars and N-trade tick bars, plus session VWAP."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900}


@dataclass
class Bar:
    tf: str
    t: float            # start, epoch seconds
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0
    n: int = 0          # trade count
    end: float = 0.0    # close time (last trade ts for tick bars)

    def add(self, px: float, sz: float, ts: float) -> None:
        self.h = max(self.h, px)
        self.l = min(self.l, px)
        self.c = px
        self.v += sz
        self.n += 1
        self.end = ts

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Trade:
    ts: float
    px: float
    sz: float = 100.0


class TimeBarBuilder:
    def __init__(self, tf: str):
        self.tf = tf
        self.sec = TF_SECONDS[tf]
        self.cur: Bar | None = None
        self.closed_t: float | None = None     # start of the last period closed; it is final
        self.late = 0                          # prints ignored because their period had already closed
        self.since: float | None = None        # prints before this are in the warm-up history already (mid-day restart)

    def _start(self, ts: float) -> float:
        return float(int(ts // self.sec) * self.sec)

    def on_trade(self, tr: Trade) -> Bar | None:
        start = self._start(tr.ts)
        if (self.closed_t is not None and start <= self.closed_t) or (self.since is not None and tr.ts < self.since):
            self.late += 1                     # a late print never reopens or bleeds forward
            return None
        closed = None
        if self.cur is not None and start > self.cur.t:
            closed = self._close()
        if self.cur is None:
            self.cur = Bar(self.tf, start, tr.px, tr.px, tr.px, tr.px, 0.0, 0, tr.ts)
        self.cur.add(tr.px, tr.sz, tr.ts)
        return closed

    def on_bar(self, b: Bar) -> Bar | None:
        """Aggregate a closed lower-timeframe bar (e.g. 1m -> 5m)."""
        start = self._start(b.t)
        closed = None
        if self.cur is not None and start > self.cur.t:
            closed = self._close()
        if self.cur is None:
            self.cur = Bar(self.tf, start, b.o, b.h, b.l, b.c, b.v, b.n, b.t + 60)
        else:
            self.cur.h, self.cur.l, self.cur.c = max(self.cur.h, b.h), min(self.cur.l, b.l), b.c
            self.cur.v += b.v
            self.cur.n += b.n
            self.cur.end = b.t + 60
        if b.t + 60 >= self.cur.t + self.sec:     # last 1m bar of the period
            closed = closed or self._close()
        return closed

    def flush(self, now: float) -> Bar | None:
        if self.cur is not None and now >= self.cur.t + self.sec:
            return self._close()
        return None

    def _close(self) -> Bar:
        b, self.cur = self.cur, None
        b.end = b.t + self.sec
        self.closed_t = b.t
        return b


class TickBarBuilder:
    def __init__(self, n: int, tf: str | None = None):
        self.size = n
        self.tf = tf or f"{n}t"
        self.cur: Bar | None = None
        self.count = 0          # bars closed today, used as an x-axis index by the UI

    def on_trade(self, tr: Trade) -> Bar | None:
        if self.cur is None:
            self.cur = Bar(self.tf, tr.ts, tr.px, tr.px, tr.px, tr.px, 0.0, 0, tr.ts)
        self.cur.add(tr.px, tr.sz, tr.ts)
        if self.cur.n >= self.size:
            b, self.cur = self.cur, None
            self.count += 1
            return b
        return None

    def reset(self) -> None:
        self.cur, self.count = None, 0


class VWAP:
    def __init__(self):
        self.pv = 0.0
        self.v = 0.0

    def add(self, px: float, sz: float) -> None:
        self.pv += px * sz
        self.v += sz

    @property
    def value(self) -> float | None:
        return self.pv / self.v if self.v else None

    def reset(self) -> None:
        self.pv = self.v = 0.0
