"""Incremental indicators matching TradingView conventions.

- EMA seeded with the SMA of the first `n` values (Pine's ta.ema behaviour).
- MACD(12,26,9): line = EMA12 - EMA26, signal = EMA9(line), hist = line - signal.
- RSI(14): Wilder smoothing, seeded with the simple average of the first 14 changes.

Every indicator supports `update(x)` (commit a closed bar) and `preview(x)`
(value if the forming bar closed at x, without mutating state). Filters on
15m/5m use preview so they react to the live candle, triggers use closed bars.
"""
from __future__ import annotations

from dataclasses import dataclass


class EMA:
    def __init__(self, n: int):
        self.n = n
        self.alpha = 2.0 / (n + 1)
        self.value: float | None = None
        self._seed: list[float] = []

    def _next(self, x: float) -> float | None:
        if self.value is not None:
            return self.alpha * x + (1 - self.alpha) * self.value
        if len(self._seed) + 1 >= self.n:
            return (sum(self._seed) + x) / self.n
        return None

    def update(self, x: float) -> float | None:
        nv = self._next(x)
        if self.value is None and nv is None:
            self._seed.append(x)
        self.value = nv if nv is not None else self.value
        return self.value

    def preview(self, x: float) -> float | None:
        return self._next(x)


@dataclass(frozen=True)
class MACDValue:
    macd: float
    signal: float

    @property
    def hist(self) -> float:
        return self.macd - self.signal

    @property
    def bull(self) -> bool:
        return self.macd > self.signal


class MACD:
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast, self.slow, self.sig = EMA(fast), EMA(slow), EMA(signal)
        self.value: MACDValue | None = None

    def update(self, x: float) -> MACDValue | None:
        f, s = self.fast.update(x), self.slow.update(x)
        if f is None or s is None:
            return None
        line = f - s
        sig = self.sig.update(line)
        if sig is None:
            return None
        self.value = MACDValue(line, sig)
        return self.value

    def preview(self, x: float) -> MACDValue | None:
        f, s = self.fast.preview(x), self.slow.preview(x)
        if f is None or s is None:
            return None
        line = f - s
        sig = self.sig.preview(line)
        return None if sig is None else MACDValue(line, sig)


class RSI:
    def __init__(self, n: int = 14):
        self.n = n
        self.prev: float | None = None
        self.avg_gain: float | None = None
        self.avg_loss: float | None = None
        self._gains: list[float] = []
        self._losses: list[float] = []
        self.value: float | None = None

    @staticmethod
    def _rsi(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    def _step(self, x: float):
        if self.prev is None:
            return None, None, None
        ch = x - self.prev
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if self.avg_gain is None:
            if len(self._gains) + 1 < self.n:
                return None, g, l
            ag = (sum(self._gains) + g) / self.n
            al = (sum(self._losses) + l) / self.n
        else:
            ag = (self.avg_gain * (self.n - 1) + g) / self.n
            al = (self.avg_loss * (self.n - 1) + l) / self.n
        return (ag, al), g, l

    def update(self, x: float) -> float | None:
        avgs, g, l = self._step(x)
        if avgs is None and g is not None:
            self._gains.append(g)
            self._losses.append(l)
        elif avgs is not None:
            self.avg_gain, self.avg_loss = avgs
            self.value = self._rsi(*avgs)
        self.prev = x
        return self.value

    def preview(self, x: float) -> float | None:
        avgs, _, _ = self._step(x)
        return None if avgs is None else self._rsi(*avgs)
