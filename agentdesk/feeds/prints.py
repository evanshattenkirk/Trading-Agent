"""Bad-print filter for the SPY trade stream, applied before any bar is built.

The consolidated tape (SIP) carries prints that never set the last sale price (out-of-sequence, average-price,
prior-reference and similar condition codes) and the odd isolated print far from the market. One such print on
2026-08-11, about 7% above SPY, made a 144-tick bar spike and a +$10,782 model trade in the IEX vs SIP replay
(research/iex_vs_sip.py). This filter is the streaming form of that script's clean_trades: over 60 sessions it
dropped 3.2% of SIP prints by condition and 61 outliers, and nothing on IEX.
"""
from __future__ import annotations

import logging
from collections import deque
from statistics import median

from ..bars import Trade

log = logging.getLogger("agentdesk.prints")

# CTA / UTDF sale conditions that don't update the last sale price (plus extended hours, which RTH bars never use).
EXCLUDE_CONDITIONS = frozenset("B C G H M N P Q R T U V W Z 4 7 9".split())


class PrintFilter:
    """push() returns the prints to pass on, in order. A print more than max_dev from the median of recent accepted
    prints is held; once `confirm` prints in a row agree on the new level they are all released (a real gap).
    Held prints that don't get confirmed are dropped."""

    def __init__(self, enabled: bool = True, max_dev: float = 0.005, window: int = 50, confirm: int = 3,
                 exclude: frozenset = EXCLUDE_CONDITIONS):
        self.enabled = enabled
        self.max_dev = max_dev
        self.confirm = confirm
        self.exclude = exclude
        self.recent: deque = deque(maxlen=window)
        self.ref: float | None = None
        self.pending: list[Trade] = []
        self.stats = {"kept": 0, "dropped_condition": 0, "dropped_outlier": 0}

    def _drop_pending(self) -> None:
        if self.pending:
            log.warning("dropped %d isolated print(s) at %s vs ~%.2f", len(self.pending),
                        ", ".join(f"{t.px:.2f}" for t in self.pending), self.ref)
            self.stats["dropped_outlier"] += len(self.pending)
            self.pending = []

    def push(self, tr: Trade, conditions=None) -> list[Trade]:
        if not self.enabled:
            return [tr]
        if conditions and self.exclude.intersection(conditions):
            self.stats["dropped_condition"] += 1
            return []
        if self.ref is not None and abs(tr.px / self.ref - 1) > self.max_dev:
            if self.pending and abs(tr.px / self.pending[0].px - 1) > self.max_dev:
                self._drop_pending()
            self.pending.append(tr)
            if len(self.pending) < self.confirm:
                return []
            out, self.pending = self.pending, []
            self.recent.clear()                     # the market moved: rebuild the reference from the new level
            self.ref = median(t.px for t in out)
        else:
            self._drop_pending()
            out = [tr]
        for t in out:
            self.stats["kept"] += 1
            self.recent.append(t.px)
            if self.ref is None or self.stats["kept"] % 10 == 0:
                self.ref = median(self.recent)
        return out
