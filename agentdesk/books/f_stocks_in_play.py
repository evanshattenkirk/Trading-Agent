"""Book F: large-cap "stocks in play" (docs/BOOK_F_HANDOFF.md). Paper only, long only, whole shares.

The section 3 rules are frozen. This module holds them as pure functions (shared by the engine and by
research/strategy_f_intraday.py, so paper and the backtest trade the same rules) plus the StocksInPlay state
machine that FHost (f_host.py) drives. All rule times are US/Eastern; the engine clock is CT.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from ..config import hhmm

ET = ZoneInfo("America/New_York")
AI_LIST = ("NVDA", "AMD", "AVGO", "MU", "TSM", "ARM", "MRVL", "SMCI", "WDC", "STX", "MSFT", "META", "GOOGL", "AMZN",
           "AAPL", "ORCL", "PLTR", "TSLA")


# ------------------------------------------------------------------ time
def at_et(d: date, t: time) -> float:
    return datetime.combine(d, t, ET).timestamp()


def et(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, ET)


def et_time(ts: float) -> time:
    return et(ts).time()


def exit_time_et(cfg: dict, half_day: bool = False) -> time:
    return time(12, 55) if half_day else hhmm(cfg["exit_et"])


# ------------------------------------------------------------------ indicators
def atr14(daily: list[dict]) -> float | None:
    """Wilder ATR(14) through the last bar given (pass bars through yesterday's close)."""
    trs = []
    for prev, b in zip(daily, daily[1:]):
        pc = prev["c"]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    if len(trs) < 14:
        return None
    a = sum(trs[:14]) / 14
    for tr in trs[14:]:
        a = (a * 13 + tr) / 14
    return a


def rvol5(today_vol, hist_vols: list) -> float | None:
    """Today's 09:30-09:35 volume over the mean of the same window in the prior 14 sessions."""
    if today_vol is None or len(hist_vols) < 14:
        return None
    base = sum(hist_vols[-14:]) / 14
    return today_vol / base if base > 0 else None


# ------------------------------------------------------------------ universe (07:30 CT)
def universe(daily: dict[str, list[dict]], sp500, cfg: dict) -> dict[str, dict]:
    """Section 3 universe from daily bars through yesterday: the top N S&P 500 names by 20-day average dollar volume
    plus the extras, then prior close >= min_price, ATR14 >= min_atr and 20-day dollar volume >= the floor.
    Names with fewer than 20 daily bars are left out (new listings enter once they have 20)."""
    u = cfg["universe"]
    info = {}
    for s, bars in daily.items():
        if len(bars) < 20:
            continue
        info[s] = {"atr": atr14(bars), "dv20": sum(b["c"] * b["v"] for b in bars[-20:]) / 20, "close": bars[-1]["c"]}
    top = sorted((s for s in info if s in sp500), key=lambda s: -info[s]["dv20"])[:u["top_sp500_by_dollar_vol"]]
    names = set(top) | {s for s in u.get("extra", []) if s in info}
    return {s: info[s] for s in names if info[s]["close"] >= u["min_price"] and info[s]["atr"] is not None
            and info[s]["atr"] >= u["min_atr"] and info[s]["dv20"] >= u["min_dollar_vol_20d"]}


# ------------------------------------------------------------------ scan
@dataclass
class ScanRow:
    symbol: str
    rvol5: float | None
    open: float                 # first 5-minute candle
    close: float
    or_high: float
    or_low: float
    vol5: float
    atr: float
    dollar_vol20: float
    rank: int | None = None
    picked: bool = False
    reason: str = ""
    news: dict | None = None

    @property
    def direction(self) -> str:
        return "green" if self.close > self.open else "red" if self.close < self.open else "doji"

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "rvol5": None if self.rvol5 is None else round(self.rvol5, 2),
                "direction": self.direction, "open": self.open, "close": self.close, "or_high": self.or_high,
                "or_low": self.or_low, "atr": round(self.atr, 3), "dollar_vol20": self.dollar_vol20, "rank": self.rank,
                "picked": self.picked, "reason": self.reason, "news": self.news, "ai": self.symbol in AI_LIST}


@dataclass
class ScanResult:
    rows: list[ScanRow]
    picks: list[ScanRow]
    shorts: list[ScanRow]


def scan_row(sym: str, info: dict, bars: list[dict], past_vols: list) -> ScanRow | None:
    """The opening-range row for one name from its 09:30-09:34 ET 1-minute bars (t = minutes after midnight ET).
    No bars: None (the name is dropped, never guessed)."""
    bs = sorted((b for b in bars if 570 <= b["t"] < 575), key=lambda b: b["t"])
    if not bs:
        return None
    vol = sum(b["v"] for b in bs)
    return ScanRow(sym, rvol5(vol, past_vols), bs[0]["o"], bs[-1]["c"], max(b["h"] for b in bs),
                   min(b["l"] for b in bs), vol, info["atr"], info["dv20"])


def rank_candidates(rows: list[ScanRow], cfg: dict) -> ScanResult:
    """RVOL5 >= rvol5_min and a green first candle, ranked by RVOL5 (ties: 20-day dollar volume), top_n bought.
    Red first candles with enough RVOL5 are ranked the same way into shadow shorts (logged, never traded)."""
    lo, n = cfg["rvol5_min"], cfg["top_n"]
    key = lambda r: (-r.rvol5, -r.dollar_vol20, r.symbol)
    live = [r for r in rows if r.rvol5 is not None and r.rvol5 >= lo]
    greens = sorted([r for r in live if r.direction == "green"], key=key)
    reds = sorted([r for r in live if r.direction == "red"], key=key)
    for r in rows:
        if r.rvol5 is None:
            r.reason = "no RVOL5 (missing history)"
        elif r.rvol5 < lo:
            r.reason = f"rvol5 {r.rvol5:.2f} < {lo:g}"
        elif r.direction == "doji":
            r.reason = "first candle not green (doji)"
    for i, r in enumerate(greens, 1):
        r.rank, r.picked = i, i <= n
        r.reason = "picked" if r.picked else f"rank {i} > top {n}"
    for i, r in enumerate(reds, 1):
        r.rank = i
        r.reason = f"red first candle: shadow short #{i}" if i <= n else f"red first candle, short rank {i} > top {n}"
    return ScanResult(rows, greens[:n], reds[:n])


# ------------------------------------------------------------------ sizing, stop, exit
def shares_for(entry: float, stop: float, cfg: dict) -> int:
    risk = entry - stop
    if entry <= 0 or risk <= 0:
        return 0
    return max(0, math.floor(min(cfg["risk_per_trade"] / risk, cfg["max_notional"] / entry) + 1e-9))


def stop_price(fill: float, atr: float, cfg: dict) -> float:
    return round(fill - cfg["stop_atr_frac"] * atr, 4)


def entry_limit(or_high: float) -> float:
    return round(or_high * 1.0005, 2)


@dataclass
class Q:
    bid: float
    ask: float
    last: float | None
    ts: float


@dataclass
class FPos:
    symbol: str
    qty: int
    entry: float
    stop: float
    opened_ts: float
    atr: float = 0.0
    or_high: float = 0.0
    rvol5: float | None = None
    rank: int | None = None
    news: dict | None = None
    id: str = ""
    status: str = "open"
    exiting: bool = False
    mark: float | None = None
    last_quote_ts: float = 0.0
    closed_ts: float | None = None
    exit_px: float | None = None
    exit_reason: str | None = None
    fills: list = field(default_factory=list)
    fees: float = 0.0

    @property
    def label(self) -> str:
        return f"F {self.symbol}"

    @property
    def risk(self) -> float:
        return round(self.qty * (self.entry - self.stop), 2)

    @property
    def unrealized(self) -> float:
        return 0.0 if self.mark is None or self.status != "open" else round((self.mark - self.entry) * self.qty, 2)

    @property
    def pnl(self) -> float:
        return 0.0 if self.exit_px is None else round((self.exit_px - self.entry) * self.qty, 2)

    @property
    def r_multiple(self) -> float | None:
        per = self.entry - self.stop
        if per <= 0:
            return None
        px = self.exit_px if self.exit_px is not None else self.mark
        return None if px is None else round((px - self.entry) / per, 3)

    def to_dict(self) -> dict:
        return {"book": "F", "id": self.id, "symbol": self.symbol, "label": self.label, "qty": self.qty,
                "entry": self.entry, "stop": self.stop, "mark": self.mark, "status": self.status,
                "opened_ts": self.opened_ts, "closed_ts": self.closed_ts, "exit_px": self.exit_px,
                "exit_reason": self.exit_reason, "risk": self.risk, "unrealized": self.unrealized, "pnl": self.pnl,
                "r": self.r_multiple, "rvol5": self.rvol5, "rank": self.rank, "news": self.news}


def should_exit(now: float, pos: FPos, quote: Q | None, cfg: dict, half_day: bool = False) -> str | None:
    """'stop' when the bid is at or below the stop; the day's exit time otherwise. No target, trail or breakeven."""
    if quote is not None and quote.bid <= pos.stop:
        return "stop"
    t = exit_time_et(cfg, half_day)
    if et_time(now) >= t:
        return f"exit {t:%H:%M} ET" + (" (half day)" if half_day else "")
    return None


# ------------------------------------------------------------------ bar-level fills (paper sim and backtest)
def _up(px: float, bp: float) -> float:
    return px * (1 + bp / 1e4)


def _dn(px: float, bp: float) -> float:
    return px * (1 - bp / 1e4)


def bar_entry_fill(bar: dict, or_high: float, limit: float, slip_bp: float = 0.0) -> float | None:
    """Buy-stop at the OR high sent as a limit at `limit`. Triggers when the bar trades above the OR high. Fills at
    max(OR high, bar open) if that is within the limit; a bar that opens through the limit fills at the limit only
    if it trades back down to it."""
    if bar["h"] <= or_high:
        return None
    px = max(or_high, bar["o"])
    if px > limit:
        if bar["l"] > limit:
            return None
        px = limit
    return _up(px, slip_bp)


def bar_stop_fill(bar: dict, stop: float, slip_bp: float = 0.0) -> float | None:
    """A bar that opens at or below the stop fills at its open (gap-through); one that trades down to it fills at
    the stop."""
    if bar["o"] <= stop:
        return _dn(bar["o"], slip_bp)
    if bar["l"] <= stop:
        return _dn(stop, slip_bp)
    return None


def bar_entry_then_stop(bar: dict, or_high: float, limit: float, atr: float, cfg: dict, slip_bp: float = 0.0):
    """(entry, stop, exit or None) for the bar that triggers. With entry and stop in the same bar, the stop hit."""
    fill = bar_entry_fill(bar, or_high, limit, slip_bp)
    if fill is None:
        return None
    stop = stop_price(fill, atr, cfg)
    return fill, stop, (_dn(stop, slip_bp) if bar["l"] <= stop else None)


def bar_short_entry_fill(bar: dict, or_low: float, slip_bp: float = 0.0) -> float | None:
    """Would-be short (logged only): sell-stop at the OR low; a gap below fills at the open."""
    if bar["l"] >= or_low:
        return None
    return _dn(min(or_low, bar["o"]), slip_bp)


def bar_short_stop_fill(bar: dict, stop: float, slip_bp: float = 0.0) -> float | None:
    if bar["o"] >= stop:
        return _up(bar["o"], slip_bp)
    if bar["h"] >= stop:
        return _up(stop, slip_bp)
    return None
