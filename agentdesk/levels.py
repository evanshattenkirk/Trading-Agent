"""Support / resistance levels used by strike selection and drawn on the chart."""
from __future__ import annotations

import math
from collections import deque

from .bars import Bar
from .clock import ct_time
from datetime import time


class Levels:
    def __init__(self):
        self.pdh = self.pdl = self.pdc = None
        self.orh = self.orl = None          # 15-minute opening range
        self.hod = self.lod = None
        self.pivots: list[tuple[float, str]] = []
        self._win: deque[Bar] = deque(maxlen=5)

    def reset_session(self) -> None:
        self.orh = self.orl = self.hod = self.lod = None
        self.pivots.clear()
        self._win.clear()

    def set_prior_day(self, h: float, l: float, c: float) -> None:
        self.pdh, self.pdl, self.pdc = h, l, c

    def on_1m(self, b: Bar) -> None:
        self.hod = b.h if self.hod is None else max(self.hod, b.h)
        self.lod = b.l if self.lod is None else min(self.lod, b.l)
        if ct_time(b.t) < time(8, 45):
            self.orh = b.h if self.orh is None else max(self.orh, b.h)
            self.orl = b.l if self.orl is None else min(self.orl, b.l)

    def on_5m(self, b: Bar) -> None:
        self._win.append(b)
        if len(self._win) == 5:
            mid = self._win[2]
            if all(mid.h > x.h for i, x in enumerate(self._win) if i != 2):
                self.pivots.append((mid.h, "5m pivot high"))
            if all(mid.l < x.l for i, x in enumerate(self._win) if i != 2):
                self.pivots.append((mid.l, "5m pivot low"))
            self.pivots = self.pivots[-12:]

    def all(self, px: float, vwap: float | None) -> list[tuple[float, str]]:
        out = []
        for v, name in [(self.pdh, "PDH"), (self.pdl, "PDL"), (self.pdc, "PDC"), (self.orh, "ORH"),
                        (self.orl, "ORL"), (self.hod, "HOD"), (self.lod, "LOD"), (vwap, "VWAP")]:
            if v is not None:
                out.append((round(v, 2), name))
        out += [(round(v, 2), n) for v, n in self.pivots]
        base = math.floor(px / 5) * 5
        out += [(float(base + k * 5), "$5 round") for k in (-1, 0, 1, 2)]
        return out

    def resistance_above(self, px: float, vwap: float | None, min_gap: float = 0.05) -> list[tuple[float, str]]:
        return sorted([x for x in self.all(px, vwap) if x[0] > px + min_gap])

    def support_below(self, px: float, vwap: float | None, min_gap: float = 0.05) -> list[tuple[float, str]]:
        return sorted([x for x in self.all(px, vwap) if x[0] < px - min_gap], reverse=True)
