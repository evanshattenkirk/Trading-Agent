"""Black-Scholes for the simulator and the modeled-option backtest fallback."""
from __future__ import annotations

import math

YEAR = 365.0 * 24 * 3600


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_sec: float, iv: float, right: str, r: float = 0.04) -> float:
    t = max(t_sec, 60.0) / YEAR
    sq = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / sq
    d2 = d1 - sq
    if right == "call":
        return spot * _ncdf(d1) - strike * math.exp(-r * t) * _ncdf(d2)
    return strike * math.exp(-r * t) * _ncdf(-d2) - spot * _ncdf(-d1)


def smile_iv(base_iv: float, spot: float, strike: float) -> float:
    """Crude 0DTE skew: puts/lower strikes richer, a small smile on both wings."""
    m = (strike - spot) / spot * 100          # % moneyness
    return max(0.05, base_iv - 0.012 * m + 0.004 * m * m)


def tick_spread(px: float) -> float:
    return 0.01 if px < 1 else 0.02 if px < 3 else 0.03


def quote_from_model(spot: float, strike: float, t_sec: float, base_iv: float, right: str) -> tuple[float, float]:
    mid = bs_price(spot, strike, t_sec, smile_iv(base_iv, spot, strike), right)
    mid = max(0.01, mid)
    half = tick_spread(mid) / 2
    bid = max(0.0, round(mid - half, 2))
    ask = round(max(mid + half, bid + 0.01), 2)
    return bid, ask
