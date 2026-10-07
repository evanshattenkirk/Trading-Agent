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


M_RTH = 0.80                # share of a VIX day's sd inside the regular session (HANDOFF section 7, strategies_bcd.py)
RTH_SEC = 390 * 60
TAIL_SEC = 15 * 60          # SPY 0DTE options trade until 15:15 CT, 15 minutes past the cash close


def trading_clock_t_sec(sec_to_expiry: float, m_rth: float = M_RTH) -> float:
    """Seconds to hand bs_price / quote_from_model (which count a 365-day calendar year) so that a VIX-style annual
    `iv` prices on the trading-day clock of HANDOFF section 7 and the B/D research model:
        sd over the time left = m_rth x iv / sqrt(252) x sqrt(RTH share left + 15/390).
    `sec_to_expiry` is the time to the 15:15 CT expiry, capped at one session plus the tail. bs_price itself is
    unchanged, so the simulator keeps its own (calendar) clock."""
    f = min(max(0.0, sec_to_expiry), RTH_SEC + TAIL_SEC) / RTH_SEC
    return m_rth ** 2 * f / 252 * YEAR


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
