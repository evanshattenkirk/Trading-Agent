# Multi-book framework + books B, C, D: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run paper books B (iron fly), C (ORB bull-put) and D (iron condor) next to book A in sim/paper/shadow, with multi-leg paper fills, account-level risk and a per-book dashboard, without changing book A.

**Architecture:** A new `agentdesk/books/` package holds the interfaces, combo positions, fills, account risk, the three strategies and a `BookHost`. The engine keeps book A's path as it is and calls the host from a few one-line hooks (bar close, each second, kill/flatten, safety trip, watchdog, snapshot). Every B/C/D fill is simulated by `PaperBroker.submit_combo`; shadow/live only add a read-only `review_option_order`.

**Tech Stack:** Python 3.11+, asyncio, pytest, SQLite, FastAPI websocket dashboard (vanilla JS).

**Spec:** `docs/superpowers/specs/2026-09-28-multi-book-bcd-design.md` (approved by Evan 2026-09-28 with all six recommendations).

## Global Constraints

- Paper only. Never call `place_option_order`, `cancel_option_order` or `exercise_option`; B/C/D code must have no path to them.
- Book A's behavior is unchanged: its 38 existing tests pass unmodified and a seeded sim day gives identical A trades with B/C/D on and off.
- Engine edits are wiring only; `_submit`, `_work_order`, `_guard`, `_reconcile` and the halt conditions stay as they are. Don't touch `OptionQuoteRecorder` (recorder thread) or `RobinhoodBroker`.
- Paper notional balance $10,000; open-risk cap $1,500; per-position max loss $300 for B/C/D; C budget $400 (lower of the two wins); fee $0.04 per contract per leg per side.
- Paper combo fill: mid − 1¢ per leg for a credit (mid + 1¢ per leg when paying), never worse than natural; every fill logs mid and natural.
- `stop_debit_x_credit: 2.0` for B and D (replaces `stop_x_credit`).
- D quiet filter uses only 08:30–09:00 CT data (range / close of that window vs trailing 14-day median of the same) plus |spot/VWAP − 1| ≤ 0.12%.
- D strikes: EM = 0.80 × (prior VIX close / 100 / √252) × √(RTH minutes left / 390 + 15/390) × spot; shorts `ceil(spot + 0.9·EM)` / `floor(spot − 0.9·EM)`, wings ±$2.
- B's VIX1D check uses the Vol desk's `vix1d_flag`; missing flag means B trades and notes "VIX1D unavailable".
- Crew cuts on B/C/D: `max(1, floor(lots × mult))`; no size-up for B/C/D.
- Run `python -m pytest -q tests` after every task; commit after every task with the session's attribution lines.

## Review Focus

1. **Cheap wings.** A $0.02/$0.03 wing is 40% wide, so a literal "spread > 25% of mid" rule would block every entry and, applied to open positions, starve the watchdog into a safety halt late in the day. Expected: spreads of 2¢ or less are exempt; open positions only need a fresh, uncrossed quote with an ask. Pinned in Task 1 and Task 5.
2. **Restart after the entry minute.** If the engine starts at 10:15, B and D must skip the day with a "missed" reason, never enter late; D with no 08:30–09:00 bars must skip, not guess. Pinned in Tasks 6 and 7.
3. **Transient failure at the entry minute.** A missing quote at 08:45:00 must not cost B the day (retry inside the 5-minute grace) and must never double-enter. Pinned in Task 6.
4. **A buggy book.** A book whose strategy raises every tick must halt and flatten itself after `max_consecutive_errors`, while A and the other books keep trading and no global safety trip fires. Pinned in Task 5.
5. **Volume from two feeds.** C's volume median is warmed from history (SIP-scale on Alpaca) but live bars on IEX carry ~5% of the volume; mixing them would block C for the first hours. Expected: with `data.alpaca.feed: iex`, C rebuilds its volume baseline from live bars only. Pinned in Task 8.

---

## File Structure

| File | Responsibility |
|---|---|
| `agentdesk/books/__init__.py` | Package marker, docstring pointing at HANDOFF section 9 |
| `agentdesk/books/base.py` | `Leg`, `OrderIntent`, `ExitIntent`, `Skip`, `MarketContext`, `Strategy` base, `credit_exit`, `mins` |
| `agentdesk/books/combo.py` | `leg_problem`, `ComboQuote`, `paper_fair`, `ComboPosition` |
| `agentdesk/books/fills.py` | `rh_legs`, `ComboExecutor` (reprice loop, ref_id, shadow review) |
| `agentdesk/books/legs.py` | `fetch_quotes` (one batched Robinhood call per combo) |
| `agentdesk/books/account.py` | `AccountRisk` (balance, sizing, open-risk cap, buying power, global halt) |
| `agentdesk/books/book.py` | `Book` (per-book day state and gates) |
| `agentdesk/books/host.py` | `BookHost` (entries, management, forced exits, kill, snapshot, events) |
| `agentdesk/books/iron_fly.py` | Book B strategy |
| `agentdesk/books/iron_condor.py` | Book D strategy + `expected_move` |
| `agentdesk/books/orb_bull_put.py` | Book C strategy |
| `agentdesk/books/vol.py` | `SimVix`, `RobinhoodVix` (prior VIX close) |
| `agentdesk/brokers/paper.py` | + `submit_combo` |
| `agentdesk/journal.py` | + `book`, `legs`, `max_loss` columns |
| `agentdesk/engine.py` | + one-line hooks to `self.books` |
| `agentdesk/__main__.py` | `check_live_promotion`, host wiring |
| `agentdesk/crew.py` | Vol desk asked for `vix1d_flag` |
| `config.yaml` | `books.account`, `books.fills`, new per-book keys |
| `agentdesk/web/*` | Book switcher, per-book P&L strip, combo card, book column/markers |
| `tests/books_fakes.py` | `FakeQuotes`, `FakeFeed`, `RecBus`, `FakeEngine`, `contracts()` |
| `tests/test_books_*.py` | One test file per task |

---

### Task 1: Combo primitives and config

**Files:**
- Create: `agentdesk/books/__init__.py`, `agentdesk/books/base.py`, `agentdesk/books/combo.py`
- Modify: `config.yaml` (books section)
- Test: `tests/test_books_combo.py`

**Interfaces:**
- Produces: `Leg(right, strike, side, ratio=1)`; `OrderIntent(legs, credit, width, reason, lots=None, budget=None, meta={})`; `ExitIntent(reason, urgent=False)`; `Skip(reason)`; `MarketContext(now, day, spot, vwap, events=[], vix_prev=None, vix1d_flag=None, size_mult=1.0, open=[])`; `Strategy(cfg)` with `warmup(bars)`, `new_day(day)`, `on_bar(tf, bar, ctx)`, `on_clock(now, ctx)`, `on_quote(pos, cq, now, ctx)`, `on_closed(pos, now)`, `entry_failed(now)`, `plan(pos) -> str`; `credit_exit(pos, close_mid, now, cfg)`; `mins(t) -> int`.
- Produces: `leg_problem(q, now, max_age, max_spread_pct, tick_exempt, opening) -> str|None`; `ComboQuote(legs, quotes, problem=None)` with `.mid(credit)`, `.half_spread()`, `.natural(credit, opening)`; `paper_fair(cq, credit, opening, model, cents_per_leg) -> float`; `ComboPosition(book, setup, legs, contracts, qty_initial, entry, credit, width, opened_ts, ...)` with `.max_loss`, `.risk_basis`, `.unrealized`, `.pnl_per_share(px)`, `.label`, `.to_dict()`.

- [ ] **Step 1: Write the failing tests** (`tests/test_books_combo.py`)

```python
import sys
from datetime import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.books.base import ExitIntent, Leg, credit_exit
from agentdesk.books.combo import ComboPosition, ComboQuote, leg_problem, paper_fair
from agentdesk.clock import at_ct
from agentdesk.config import load_config
from agentdesk.exits import Contract
from agentdesk.feeds.base import Quote

FLY = [Leg("call", 765, "sell"), Leg("put", 765, "sell"), Leg("call", 770, "buy"), Leg("put", 760, "buy")]


def q(b, a, ts=100.0):
    return Quote(b, a, ts)


QS = [q(2.00, 2.02), q(1.90, 1.92), q(0.40, 0.41), q(0.35, 0.36)]              # mid 3.16, half-spreads 0.03
WIDE = [q(1.95, 2.07), q(1.85, 1.97), q(0.35, 0.45), q(0.30, 0.40)]            # mid 3.17, half-spreads 0.22


def fly_pos(qty=2, entry=3.20):
    cs = [Contract("SPY", "2026-09-28", float(l.strike), l.right) for l in FLY]
    return ComboPosition("B", "IRON FLY", FLY, cs, qty, entry, True, 5.0, 100.0)


def test_credit_mid_and_natural():
    cq = ComboQuote(FLY, QS)
    assert cq.mid(True) == pytest.approx(3.16)
    assert cq.natural(True, opening=True) == pytest.approx(3.13)     # sell at bids, buy wings at asks
    assert cq.natural(True, opening=False) == pytest.approx(3.19)    # buy back at asks, sell wings at bids


def test_paper_fair_mid_offset_never_worse_than_natural():
    assert paper_fair(ComboQuote(FLY, QS), True, True, "mid_offset", 0.01) == pytest.approx(3.13)
    wide = ComboQuote(FLY, WIDE)
    assert paper_fair(wide, True, True, "mid_offset", 0.01) == pytest.approx(3.13)    # 3.17 - 4 legs x 1c
    assert paper_fair(wide, True, True, "natural", 0.01) == pytest.approx(2.95)
    assert paper_fair(wide, True, False, "mid_offset", 0.01) == pytest.approx(3.21)   # paying to close: mid + 4c


def test_leg_problems_fail_closed():
    assert leg_problem(None, 100.0, 5, 0.25, 0.02, True) == "no quote"
    assert "stale" in leg_problem(q(1.00, 1.02, ts=90.0), 100.0, 5, 0.25, 0.02, True)
    assert "crossed" in leg_problem(q(1.10, 1.00), 100.0, 5, 0.25, 0.02, True)
    assert "zero" in leg_problem(q(0.0, 0.0), 100.0, 5, 0.25, 0.02, True)
    assert "spread" in leg_problem(q(0.80, 1.20), 100.0, 5, 0.25, 0.02, True)
    assert leg_problem(q(1.00, 1.02), 100.0, 5, 0.25, 0.02, True) is None


def test_cheap_wing_is_not_rejected_as_wide():                        # review focus 1
    assert leg_problem(q(0.02, 0.03), 100.0, 5, 0.25, 0.02, True) is None      # 1 tick = 40% of mid
    assert leg_problem(q(0.00, 0.01), 100.0, 5, 0.25, 0.02, False) is None     # an open position still marks
    assert leg_problem(q(0.00, 0.01), 100.0, 5, 0.25, 0.02, True) == "zero bid"
    assert leg_problem(q(0.80, 1.20), 100.0, 5, 0.25, 0.02, False) is None     # wide never blocks a stop check


def test_combo_position_risk_and_pnl():
    p = fly_pos()
    assert p.max_loss == pytest.approx(360.0)            # (5 - 3.20) x 100 x 2
    assert p.risk_basis == pytest.approx(360.0)
    p.mark = 1.60
    assert p.unrealized == pytest.approx(320.0)
    assert p.pnl_per_share(1.60) == pytest.approx(1.60)
    assert p.id.startswith("B")
    d = p.to_dict()
    assert d["book"] == "B" and len(d["legs"]) == 4 and d["max_loss"] == pytest.approx(360.0)
    assert d["contract"] == p.label and "765" in p.label


def test_credit_exit_take_profit_stop_and_close():
    c = {"take_profit_pct": 0.5, "stop_debit_x_credit": 2.0, "close_ct": "14:30"}
    p = fly_pos(entry=3.00)
    early = at_ct(__import__("datetime").date(2026, 9, 28), time(10, 0))
    assert credit_exit(p, 2.00, early, c) is None
    assert credit_exit(p, 1.50, early, c).reason.startswith("take profit")
    st = credit_exit(p, 6.00, early, c)
    assert st.urgent and "stop" in st.reason
    late = at_ct(__import__("datetime").date(2026, 9, 28), time(14, 30))
    assert "close 14:30" in credit_exit(p, 2.00, late, c).reason


def test_config_has_one_explicit_stop_field():
    books = load_config()["books"]
    for k in ("B_iron_fly", "D_iron_condor"):
        assert books[k]["stop_debit_x_credit"] == 2.0 and "stop_x_credit" not in books[k]
    assert books["account"] == {"paper_balance": 10000, "open_risk_cap": 1500, "per_position_max_loss": 300,
                                "fee_per_leg": 0.04}
    assert books["fills"]["model"] == "mid_offset" and books["fills"]["cents_per_leg"] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest -q tests/test_books_combo.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'agentdesk.books'`

- [ ] **Step 3: Implement**

`agentdesk/books/__init__.py`:
```python
"""Paper books B-E next to the engine's book A (HANDOFF section 9). Every fill here is simulated."""
```

`agentdesk/books/base.py`:
```python
"""Interfaces shared by the paper books (HANDOFF section 9)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..clock import ct_time
from ..config import hhmm


@dataclass(frozen=True)
class Leg:
    right: str              # "call" | "put"
    strike: float
    side: str               # side at open: "sell" | "buy"
    ratio: int = 1


@dataclass
class OrderIntent:
    legs: list[Leg]         # sold legs first for a credit structure, so the order's direction comes out right
    credit: bool
    width: float            # widest wing in $: max loss per share = width - credit
    reason: str
    lots: int | None = None         # fixed lots (B, D)
    budget: float | None = None     # size from max loss: floor(budget / max loss per lot) (C)
    meta: dict = field(default_factory=dict)


@dataclass
class ExitIntent:
    reason: str
    urgent: bool = False    # urgent: start at natural; otherwise work from mid


@dataclass
class Skip:
    reason: str


@dataclass
class MarketContext:
    now: float
    day: date
    spot: float | None
    vwap: float | None
    events: list = field(default_factory=list)      # [(event ts, name)] high-impact events today
    vix_prev: float | None = None
    vix1d_flag: bool | None = None                  # None: no VIX1D source today
    size_mult: float = 1.0                          # crew cut, <= 1.0
    open: list = field(default_factory=list)        # this book's open positions


class Strategy:
    name = "strategy"

    def __init__(self, cfg: dict):
        self.c = cfg

    def warmup(self, bars_1m: list) -> None: ...

    def new_day(self, day) -> None: ...

    def on_bar(self, tf: str, bar, ctx: MarketContext):
        return None

    def on_clock(self, now: float, ctx: MarketContext):
        return None

    def on_quote(self, pos, cq, now: float, ctx: MarketContext):
        return None

    def on_closed(self, pos, now: float) -> None: ...

    def entry_failed(self, now: float) -> None:
        """The host couldn't place the intent for a transient reason (quote, fill)."""

    def plan(self, pos) -> str:
        return ""


def mins(t) -> int:
    return t.hour * 60 + t.minute


def credit_exit(pos, close_mid: float, now: float, c: dict) -> ExitIntent | None:
    """B and D: take profit when the debit to close is <= (1 - tp) x credit; stop when it reaches
    stop_debit_x_credit x credit (a loss of 1x credit at 2.0); close at close_ct."""
    if close_mid <= pos.entry * (1 - c["take_profit_pct"]):
        return ExitIntent(f"take profit {int(round(c['take_profit_pct'] * 100))}%")
    if close_mid >= pos.entry * c["stop_debit_x_credit"]:
        return ExitIntent(f"stop: debit {c['stop_debit_x_credit']:g}x credit", urgent=True)
    if ct_time(now) >= hhmm(c["close_ct"]):
        return ExitIntent(f"close {c['close_ct']} CT")
    return None
```

`agentdesk/books/combo.py`:
```python
"""Multi-leg quotes and positions.

Prices are in the structure's own sign: a credit structure quotes the credit it collects (and the debit to
buy it back), a debit structure the debit it pays. natural = every leg at its touch (sell at bid, buy at ask).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from ..exits import Contract
from .base import Leg

_ids = itertools.count(1)


def leg_problem(q, now: float, max_age: float, max_spread_pct: float, tick_exempt: float, opening: bool) -> str | None:
    """Why a leg quote can't be used, or None. Entries need the full check; an open position only needs a fresh,
    uncrossed quote with an ask, so a zero-bid or wide wing never blocks a stop check."""
    if q is None:
        return "no quote"
    if now - q.ts > max_age:
        return f"stale ({now - q.ts:.0f}s)"
    if q.ask <= 0:
        return "zero ask"
    if q.bid > q.ask:
        return "crossed"
    if opening:
        if q.bid <= 0:
            return "zero bid"
        spread, mid = q.ask - q.bid, (q.ask + q.bid) / 2
        if spread > tick_exempt + 1e-9 and spread > max_spread_pct * mid:
            return f"spread {spread / mid:.0%} of mid"
    return None


@dataclass
class ComboQuote:
    legs: list[Leg]
    quotes: list
    problem: str | None = None

    def mid(self, credit: bool) -> float:
        v = sum((1 if l.side == "sell" else -1) * l.ratio * (q.bid + q.ask) / 2 for l, q in zip(self.legs, self.quotes))
        return round(v if credit else -v, 4)

    def half_spread(self) -> float:
        return sum(l.ratio * (q.ask - q.bid) / 2 for l, q in zip(self.legs, self.quotes))

    def natural(self, credit: bool, opening: bool) -> float:
        receiving = credit == opening
        m, h = self.mid(credit), self.half_spread()
        return round(m - h if receiving else m + h, 4)


def paper_fair(cq: ComboQuote, credit: bool, opening: bool, model: str, cents_per_leg: float) -> float:
    """The price a paper combo order fills at: natural, or mid -/+ cents per leg, never worse than natural."""
    mid, nat = cq.mid(credit), cq.natural(credit, opening)
    if model == "natural":
        return round(nat, 2)
    n = sum(l.ratio for l in cq.legs)
    receiving = credit == opening
    f = mid - cents_per_leg * n if receiving else mid + cents_per_leg * n
    f = max(f, nat) if receiving else min(f, nat)
    return round(f, 2)


@dataclass
class ComboPosition:
    book: str                   # "B"
    setup: str                  # "IRON FLY"
    legs: list[Leg]
    contracts: list[Contract]   # same order as legs
    qty_initial: int
    entry: float                # fill: credit received (credit) or debit paid, per share
    credit: bool
    width: float
    opened_ts: float
    strike_reason: str = ""
    entry_reasons: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    id: str = ""
    qty: int = 0
    mark: float = 0.0           # mid price to close
    peak: float = 0.0           # best mark seen (lowest debit for a credit structure)
    stop: float = 0.0           # display: close price that stops out (0 = not price-based)
    target: float = 0.0         # display: close price that takes profit
    realized: float = 0.0
    fees: float = 0.0
    fills: list = field(default_factory=list)
    status: str = "open"
    closed_ts: float | None = None
    exit_reason: str | None = None
    last_quote_ts: float = 0.0  # last time the host saw usable quotes on every leg (safety watchdog)
    l2: dict | None = None
    exiting: bool = False

    def __post_init__(self):
        self.id = self.id or f"{self.book}{next(_ids)}"
        self.qty = self.qty or self.qty_initial
        self.mark = self.mark or self.entry
        self.peak = self.peak or self.entry
        self.last_quote_ts = self.last_quote_ts or self.opened_ts

    def pnl_per_share(self, close_px: float) -> float:
        return (self.entry - close_px) if self.credit else (close_px - self.entry)

    def _risk(self, qty: int) -> float:
        per = (self.width - self.entry) if self.credit else self.entry
        return round(max(0.0, per) * 100 * qty, 2)

    @property
    def max_loss(self) -> float:
        return self._risk(self.qty)

    @property
    def risk_basis(self) -> float:
        return self._risk(self.qty_initial)

    @property
    def unrealized(self) -> float:
        return self.pnl_per_share(self.mark) * 100 * self.qty

    @property
    def total_pnl(self) -> float:
        return self.realized + self.unrealized - self.fees

    @property
    def pnl_pct(self) -> float:
        return self.total_pnl / self.risk_basis if self.risk_basis else 0.0

    @property
    def label(self) -> str:
        c = self.contracts[0]
        legs = "/".join(f"{'-' if l.side == 'sell' else '+'}{l.strike:g}{l.right[0].upper()}" for l in self.legs)
        return f"{c.symbol} {legs} {c.expiry[5:]}"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "book": self.book, "contract": self.label, "occ": ",".join(c.occ for c in self.contracts),
            "setup": self.setup, "qty": self.qty, "qty_initial": self.qty_initial, "entry": round(self.entry, 2),
            "mark": round(self.mark, 3), "peak": round(self.peak, 3), "stop": round(self.stop, 2),
            "target": round(self.target, 2), "credit": self.credit, "width": self.width,
            "max_loss": self.max_loss if self.status == "open" else self.risk_basis,
            "pnl_pct": round(self.pnl_pct * 100, 1), "realized": round(self.realized, 2),
            "unrealized": round(self.unrealized, 2), "total_pnl": round(self.total_pnl, 2), "fees": round(self.fees, 2),
            "opened_ts": self.opened_ts, "closed_ts": self.closed_ts, "status": self.status,
            "exit_reason": self.exit_reason, "strike_reason": self.strike_reason, "entry_reasons": self.entry_reasons,
            "fills": self.fills, "l2": self.l2,
            "legs": [{"right": l.right, "strike": l.strike, "side": l.side, "ratio": l.ratio} for l in self.legs],
            "meta": {k: v for k, v in self.meta.items() if isinstance(v, (int, float, str))},
        }
```

`config.yaml`, replace the `books:` block (keep A and C_bear_puts/E lines as they are):
```yaml
books:
  account: {paper_balance: 10000, open_risk_cap: 1500, per_position_max_loss: 300, fee_per_leg: 0.04}   # $10k paper (section 11 row); cap = sum of open max losses incl. A
  fills: {model: mid_offset, cents_per_leg: 1, reprice_step_c: 1, max_reprices: 4, max_quote_age_s: 5, max_leg_spread_pct: 0.25, tick_exempt: 0.02}
  history_days: 20        # 1m history the books load at startup (D's 14-day quiet filter, C's indicators)
  A_macd_calls:    {enabled: true,  paper_only: true,  risk_per_trade: 500, max_contracts: 5, daily_loss: 400}
  B_iron_fly:      {enabled: true,  paper_only: true,  lots: 1, entry_ct: "08:45", entry_grace_min: 5, wings: 5, take_profit_pct: 0.50, stop_debit_x_credit: 2.0, close_ct: "14:30", skip_event_before_ct: "14:00", vix1d_gap: 3.0}
  C_orb_bull_put:  {enabled: true,  paper_only: true,  max_loss_budget: 400, width: 2, max_trades_day: 2, window_ct: {start: "09:00", end: "13:30"}, or_minutes: 30, ema: 20, rsi: [55, 72], vol_mult: 0.8, vol_lookback: 20, stop_pct: 0.0018, target_pct: 0.0045, max_hold_min: 45, close_ct: "14:15"}
  C_bear_puts:     {enabled: false, paper_only: true,  note: "failed its backtest; stays off until a pre-registered filter set passes out-of-sample"}
  D_iron_condor:   {enabled: true,  paper_only: true,  lots: 1, entry_ct: "09:00", entry_grace_min: 5, short_em_mult: 0.9, em_rth_mult: 0.80, wings: 2, take_profit_pct: 0.50, stop_debit_x_credit: 2.0, close_ct: "14:25", quiet_filter: true, quiet_window_min: 30, quiet_lookback_days: 14, vwap_max_pct: 0.0012}
  E_earnings_iv:   {enabled: true,  paper_only: true,  max_debit: 500, max_debit_e2: 250, max_open: 3, structures: [straddle_t3, calendar_t10], never_hold_through_announcement: true}
```
Update the comment above `books:` to say B, C, D run in `agentdesk/books/` and E is not built yet. (E's `max_debit` reflects Evan's decision of 2026-09-28; if thread 6 has already changed it on main, keep main's value when merging.)

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest -q tests/test_books_combo.py && python -m pytest -q tests`
Expected: 7 new pass, 45 total pass.

- [ ] **Step 5: Commit** — `git add agentdesk/books config.yaml tests/test_books_combo.py && git commit -m "books: combo primitives, credit exits, books config"`

---

### Task 2: Multi-leg paper fills and the reprice loop

**Files:**
- Modify: `agentdesk/brokers/paper.py`
- Create: `agentdesk/books/fills.py`, `agentdesk/books/legs.py`, `tests/books_fakes.py`
- Test: `tests/test_books_fills.py`

**Interfaces:**
- Consumes: `Leg`, `ComboQuote`, `paper_fair` (Task 1); `order_args`, `dict_items`, `find_key`, `_short` from `brokers/robinhood.py` (read-only import).
- Produces: `PaperBroker.combo_model: str`, `PaperBroker.combo_cents: float`, `async PaperBroker.submit_combo(legs, contracts, qty, limit, credit, opening, now) -> OrderResult` (`raw` has `mid`, `natural`); `rh_legs(legs, ids, opening) -> list[dict]`; `ComboExecutor(broker, fills_cfg, reviewer=None)` with `async work(legs, contracts, qty, credit, opening, urgent, now) -> OrderResult` (`raw` adds `ref_id`, `limit`, `tries`); `async fetch_quotes(quotes, contracts) -> list[Quote|None]`.
- Produces (tests): `tests/books_fakes.py` with `FakeQuotes`, `contracts(legs)`, `FILLS`.

- [ ] **Step 1: Write the fakes and failing tests**

`tests/books_fakes.py`:
```python
"""Shared fakes for the books tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.exits import Contract
from agentdesk.feeds.base import Quote, QuoteSource

FILLS = {"model": "mid_offset", "cents_per_leg": 1, "reprice_step_c": 1, "max_reprices": 4,
         "max_quote_age_s": 5, "max_leg_spread_pct": 0.25, "tick_exempt": 0.02}


class FakeQuotes(QuoteSource):
    name = "fake"

    def __init__(self, now: float = 100.0):
        self.book: dict = {}
        self.now = now
        self.calls = 0

    def set(self, right, strike, bid, ask, ts=None):
        self.book[(right, float(strike))] = (bid, ask, ts)

    async def quote(self, c):
        self.calls += 1
        v = self.book.get((c.right, float(c.strike)))
        if v is None:
            return None
        b, a, ts = v
        return Quote(b, a, self.now if ts is None else ts)


def contracts(legs, expiry="2026-09-28"):
    return [Contract("SPY", expiry, float(l.strike), l.right) for l in legs]
```

`tests/test_books_fills.py`:
```python
import asyncio

import pytest

from books_fakes import FILLS, FakeQuotes, contracts
from agentdesk.books.base import Leg
from agentdesk.books.fills import ComboExecutor, rh_legs
from agentdesk.books.legs import fetch_quotes
from agentdesk.brokers.paper import PaperBroker
from agentdesk.brokers.robinhood import order_args

FLY = [Leg("call", 765, "sell"), Leg("put", 765, "sell"), Leg("call", 770, "buy"), Leg("put", 760, "buy")]
TIGHT = [(2.00, 2.02), (1.90, 1.92), (0.40, 0.41), (0.35, 0.36)]      # mid 3.16, natural 3.13
WIDE = [(1.95, 2.07), (1.85, 1.97), (0.35, 0.45), (0.30, 0.40)]       # mid 3.17, natural 2.95, fair 3.13


def run(c):
    return asyncio.run(c)


def quotes(rows):
    fq = FakeQuotes()
    for l, (b, a) in zip(FLY, rows):
        fq.set(l.right, l.strike, b, a)
    return fq


def broker(fq):
    pb = PaperBroker(fq)
    pb.combo_model, pb.combo_cents = "mid_offset", 0.01
    return pb


def test_submit_combo_fills_at_fair_or_rests():
    pb = broker(quotes(TIGHT))
    cs = contracts(FLY)
    assert run(pb.submit_combo(FLY, cs, 1, 3.16, True, True, 100.0)).status == "unfilled"
    r = run(pb.submit_combo(FLY, cs, 1, 3.13, True, True, 100.0))
    assert r.status == "filled" and r.filled_qty == 1 and r.avg_price == pytest.approx(3.13)
    assert r.raw["mid"] == pytest.approx(3.16) and r.raw["natural"] == pytest.approx(3.13)
    assert run(pb.submit_combo(FLY, cs, 1, 3.00, True, True, 100.0)).avg_price == pytest.approx(3.13)


def test_submit_combo_rejects_missing_leg_quote():
    fq = quotes(TIGHT)
    del fq.book[("put", 760.0)]
    assert run(broker(fq).submit_combo(FLY, contracts(FLY), 1, 3.0, True, True, 100.0)).status == "rejected"


def test_executor_walks_from_mid_toward_natural():
    ex = ComboExecutor(broker(quotes(WIDE)), FILLS)
    r = run(ex.work(FLY, contracts(FLY), 1, True, True, False, 100.0))
    assert r.status == "filled" and r.avg_price == pytest.approx(3.13)
    assert r.raw["tries"] == 2 and r.raw["ref_id"] and r.raw["limit"] == pytest.approx(3.13)


def test_executor_urgent_starts_at_natural():
    ex = ComboExecutor(broker(quotes(WIDE)), FILLS)
    r = run(ex.work(FLY, contracts(FLY), 1, True, False, True, 100.0))     # buy the fly back now
    assert r.raw["tries"] == 1 and r.raw["limit"] == pytest.approx(3.39)
    assert r.avg_price == pytest.approx(3.21)                             # paper fair: mid + 4c


def test_executor_natural_model_always_ends_filled():
    pb = broker(quotes(WIDE))
    pb.combo_model = "natural"
    r = run(ComboExecutor(pb, {**FILLS, "model": "natural"}).work(FLY, contracts(FLY), 1, True, True, False, 100.0))
    assert r.status == "filled" and r.avg_price == pytest.approx(2.95) and r.raw["tries"] == 5


def test_multi_leg_direction_open_credit_close_debit():
    ids = ["c765", "p765", "c770", "p760"]
    opening = rh_legs(FLY, ids, opening=True)
    assert opening[0] == {"option_id": "c765", "side": "sell", "position_effect": "open", "ratio_quantity": 1}
    assert order_args("acct", opening, 1, 3.13, True)["direction"] == "credit"
    closing = rh_legs(FLY, ids, opening=False)
    assert closing[0]["side"] == "buy" and closing[2] == {"option_id": "c770", "side": "sell",
                                                           "position_effect": "close", "ratio_quantity": 1}
    assert order_args("acct", closing, 1, 1.50, True)["direction"] == "debit"


class FakeRH:
    account = "123456"

    def __init__(self):
        self.calls = []

    async def instrument_id(self, c):
        return f"{c.right[0]}{c.strike:g}"

    async def call(self, tool, args):
        self.calls.append((tool, args))
        return {"ok": True}


def test_shadow_reviews_once_per_order_and_never_places():
    rh = FakeRH()
    ex = ComboExecutor(broker(quotes(WIDE)), FILLS, reviewer=rh)
    r = run(ex.work(FLY, contracts(FLY), 1, True, True, False, 100.0))
    assert [t for t, _ in rh.calls] == ["review_option_order"]
    assert rh.calls[0][1]["direction"] == "credit" and r.review == {"ok": True}
    assert r.status == "filled"                        # the fill itself is still paper


class RHQ:
    """Stand-in for RobinhoodQuotes whose batched get_option_quotes answers with `shape(ids)`."""
    def __init__(self, shape):
        self.rh, self.cache = FakeRH(), {}

        async def call(tool, args):
            self.rh.calls.append((tool, args))
            return shape(args["instrument_ids"])
        self.rh.call = call

    async def quote(self, c):
        raise AssertionError("should batch")


def test_fetch_quotes_batches_robinhood_calls():
    # Robinhood's real shape (recorder thread, 2026-09-28): prices nested under results[].quote
    src = RHQ(lambda ids: {"results": [{"instrument_id": i, "quote": {"bid_price": "1.00", "ask_price": "1.02"}}
                                       for i in ids]})
    qs = run(fetch_quotes(src, contracts(FLY)))
    assert len(src.rh.calls) == 1 and all(q.bid == 1.0 and q.ask == 1.02 for q in qs)
    assert len(src.cache) == 4


def test_fetch_quotes_accepts_flat_items_and_missing_legs():
    src = RHQ(lambda ids: {"quotes": [{"instrument_id": i, "bid_price": "1.00", "ask_price": "1.02"} for i in ids[:3]]})
    qs = run(fetch_quotes(src, contracts(FLY)))
    assert [q is None for q in qs] == [False, False, False, True]    # a missing leg is None, never a stale guess
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest -q tests/test_books_fills.py`
Expected: FAIL (`AttributeError: 'PaperBroker' object has no attribute 'submit_combo'` / missing `agentdesk.books.fills`).

- [ ] **Step 3: Implement**

Append to `agentdesk/brokers/paper.py` (inside `PaperBroker`; add the two class attributes under `name`):
```python
    combo_model = "mid_offset"      # mid_offset | natural (books.fills.model)
    combo_cents = 0.01              # $ per leg off mid (mid_offset)

    async def submit_combo(self, legs, contracts, qty: int, limit: float, credit: bool, opening: bool, now: float) -> OrderResult:
        """Multi-leg paper fill. The fair price is mid -/+ combo_cents per leg, never worse than natural; a limit at
        or through it fills there, a less aggressive one rests unfilled. All-or-nothing."""
        from ..books.combo import ComboQuote, paper_fair
        qs = [await self.quotes.quote(c) for c in contracts]
        if any(q is None for q in qs):
            return OrderResult("rejected", message="no quote on a leg")
        cq = ComboQuote(legs, qs)
        fair = paper_fair(cq, credit, opening, self.combo_model, self.combo_cents)
        receiving = credit == opening
        marketable = limit <= fair + 1e-9 if receiving else limit >= fair - 1e-9
        oid = f"paper-{next(_ids)}"
        extra = {"mid": cq.mid(credit), "natural": cq.natural(credit, opening)}
        if marketable:
            return OrderResult("filled", qty, fair, oid, raw=extra)
        return OrderResult("unfilled", 0, 0.0, oid, f"limit {limit:.2f} vs fair {fair:.2f}", raw=extra)
```

`agentdesk/books/legs.py`:
```python
"""Leg quotes for a combo: one batched get_option_quotes call on Robinhood, per-leg quotes elsewhere."""
from __future__ import annotations

import time

from ..feeds.base import Quote


async def fetch_quotes(quotes, contracts) -> list:
    rh = getattr(quotes, "rh", None)
    if rh is None or len(contracts) < 2:
        return [await quotes.quote(c) for c in contracts]
    from ..brokers.robinhood import dict_items, find_key
    ids = [await rh.instrument_id(c) for c in contracts]
    if not all(ids):
        return [await quotes.quote(c) for c in contracts]
    data = await rh.call("get_option_quotes", {"instrument_ids": ids})
    now, by = time.time(), {}
    for item in dict_items(data):
        q = item.get("quote") if isinstance(item.get("quote"), dict) else item   # results[].quote on Robinhood
        oid = str(item.get("instrument_id") or q.get("instrument_id") or find_key(item, ["instrument_id", "id"]) or "")
        b = q.get("bid_price", q.get("bid"))
        a = q.get("ask_price", q.get("ask"))
        if oid and b is not None and a is not None:
            by[oid] = Quote(float(b), float(a), now)
    cache = getattr(quotes, "cache", None)
    if cache is not None:
        cache.update(by)             # the paper fill right after reads these instead of re-fetching
    return [by.get(i) for i in ids]
```

`agentdesk/books/fills.py`:
```python
"""Working a multi-leg order: start at mid, step toward natural, last try at natural. One ref_id per logical
order. In shadow/live the first price is also sent to review_option_order; the fill is always paper."""
from __future__ import annotations

import uuid

from ..brokers.base import OrderResult
from .combo import ComboQuote


def rh_legs(legs, ids, opening: bool) -> list[dict]:
    """Robinhood legs. Closing flips every side, so a credit structure closes as a debit (order_args reads the
    direction from the first leg)."""
    out = []
    for l, oid in zip(legs, ids):
        side = l.side if opening else ("buy" if l.side == "sell" else "sell")
        out.append({"option_id": oid, "side": side, "position_effect": "open" if opening else "close",
                    "ratio_quantity": l.ratio})
    return out


class ComboExecutor:
    def __init__(self, broker, fills: dict, reviewer=None):
        self.broker, self.f, self.reviewer = broker, fills, reviewer

    async def work(self, legs, contracts, qty: int, credit: bool, opening: bool, urgent: bool, now: float) -> OrderResult:
        ref = str(uuid.uuid4())
        n = sum(l.ratio for l in legs)
        step = self.f["reprice_step_c"] / 100 * n
        receiving = credit == opening
        review, tries = None, 0
        while True:
            qs = [await self.broker.quotes.quote(c) for c in contracts]
            if any(q is None for q in qs):
                return OrderResult("rejected", message="no quote on a leg", raw={"ref_id": ref, "tries": tries})
            cq = ComboQuote(legs, qs)
            mid, nat = cq.mid(credit), cq.natural(credit, opening)
            if urgent or tries >= self.f["max_reprices"]:
                limit = nat
            else:
                limit = mid - step * tries if receiving else mid + step * tries
                limit = max(limit, nat) if receiving else min(limit, nat)
            limit = round(limit, 2)
            if self.reviewer is not None and review is None:
                review = await self._review(legs, contracts, qty, limit, opening)
            res = await self.broker.submit_combo(legs, contracts, qty, limit, credit, opening, now)
            tries += 1
            res.raw = {**res.raw, "ref_id": ref, "limit": limit, "tries": tries}
            res.review = review
            if res.status != "unfilled" or res.filled_qty or limit == round(nat, 2):
                return res

    async def _review(self, legs, contracts, qty: int, limit: float, opening: bool) -> dict:
        from ..brokers.robinhood import _short, order_args
        try:
            ids = [await self.reviewer.instrument_id(c) for c in contracts]
            args = order_args(self.reviewer.account, rh_legs(legs, ids, opening), qty, limit, True, contracts[0].symbol)
            return _short(await self.reviewer.call("review_option_order", args))
        except Exception as ex:
            return {"error": str(ex)[:200]}
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest -q tests/test_books_fills.py && python -m pytest -q tests`
Expected: 8 new pass; full suite passes.

- [ ] **Step 5: Commit** — `git commit -m "books: multi-leg paper fills, reprice loop, shadow review, batched leg quotes"`

---

### Task 3: Account risk and per-book state

**Files:**
- Create: `agentdesk/books/account.py`, `agentdesk/books/book.py`
- Test: `tests/test_books_account.py`

**Interfaces:**
- Produces: `AccountRisk(cfg=None)` with `.c`, `.realized`, `.halted`, `.halt_reason`, `.flatten`, `equity(a_day_pnl=0.0)`, `size(per_lot, lots=None, budget=None, mult=1.0) -> (int, str)`, `can_open(max_loss, open_risk, a_day_pnl=0.0) -> (bool, str)`, `halt(reason, flatten=False)`, `on_closed(net)`, `to_dict(open_risk, a_day_pnl)`.
- Produces: `Book(key, cfg, strategy)` with `.key`, `.letter`, `.c`, `.strategy`, `.open`, `.closed`, `.skips`, `.trades`, `.wins`, `.losses`, `.day_pnl`, `.halted`, `.halt_reason`, `.blocked`, `.errors`, `.entering`, `reset_day()`, `can_enter() -> (bool, str)`, `on_open(pos)`, `on_close(pos, net)`, `halt(reason)`, `to_dict()`.

- [ ] **Step 1: Write the failing tests**

```python
from books_fakes import contracts
from agentdesk.books.account import AccountRisk
from agentdesk.books.base import Leg, Strategy
from agentdesk.books.book import Book
from agentdesk.books.combo import ComboPosition

PUTS = [Leg("put", 764, "sell"), Leg("put", 762, "buy")]


def test_fixed_lots_capped_by_per_position_max_loss():
    a = AccountRisk({"per_position_max_loss": 300})
    assert a.size(170, lots=1)[0] == 1
    assert a.size(170, lots=2)[0] == 1
    n, why = a.size(310, lots=1)
    assert n == 0 and "310" in why


def test_budget_sizing_uses_lower_of_budget_and_cap():
    a = AccountRisk({"per_position_max_loss": 300})
    assert a.size(150, budget=400)[0] == 2
    assert a.size(160, budget=400)[0] == 1
    assert AccountRisk({"per_position_max_loss": 1000}).size(160, budget=400)[0] == 2


def test_crew_cut_never_skips_a_one_lot_book():
    a = AccountRisk({"per_position_max_loss": 1000})
    assert a.size(150, lots=1, mult=0.5)[0] == 1
    assert a.size(150, lots=4, mult=0.5)[0] == 2
    assert a.size(150, lots=4, mult=1.25)[0] == 4          # no size-up for B/C/D


def test_open_risk_cap_and_buying_power():
    a = AccountRisk({"paper_balance": 10000, "open_risk_cap": 1500})
    assert a.can_open(300, open_risk=1000)[0]
    ok, why = a.can_open(600, open_risk=1000)
    assert not ok and "cap" in why
    small = AccountRisk({"paper_balance": 500, "open_risk_cap": 1500})
    ok, why = small.can_open(200, open_risk=400)
    assert not ok and "buying power" in why
    assert small.can_open(200, open_risk=400, a_day_pnl=200)[0]
    small.on_closed(-150)
    assert small.equity() == 350


def test_halt_blocks_new_risk():
    a = AccountRisk()
    a.halt("KILL switch", flatten=True)
    ok, why = a.can_open(1, 0)
    assert not ok and "KILL" in why and a.flatten


def test_book_gates_and_day_state():
    b = Book("C_orb_bull_put", {"max_trades_day": 2}, Strategy({}))
    assert b.letter == "C" and b.can_enter()[0]
    p = ComboPosition("C", "ORB BULL PUT", PUTS, contracts(PUTS), 1, 0.50, True, 2.0, 100.0)
    b.on_open(p)
    assert b.can_enter() == (False, "position already open")
    b.on_close(p, -40.0)
    assert b.trades == 1 and b.losses == 1 and b.day_pnl == -40.0 and b.can_enter()[0]
    b.on_open(ComboPosition("C", "ORB BULL PUT", PUTS, contracts(PUTS), 1, 0.50, True, 2.0, 200.0))
    b.on_close(b.open[0], 25.0)
    assert b.can_enter() == (False, "max 2 trades today")
    b.blocked = "partial fill"
    b.reset_day()
    assert b.can_enter()[0] and b.blocked is None and b.trades == 0
    b.halt("error: boom")
    assert not b.can_enter()[0] and b.to_dict()["halted"]
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest -q tests/test_books_account.py` → FAIL (missing modules).

- [ ] **Step 3: Implement**

`agentdesk/books/account.py`:
```python
"""Account-level risk across books (HANDOFF sections 9 and 11). Paper notional balance; B/C/D sizing by max
loss; an open-risk cap over every open position (book A's open debit counts); a global halt."""
from __future__ import annotations

ACCOUNT = {"paper_balance": 10000, "open_risk_cap": 1500, "per_position_max_loss": 300, "fee_per_leg": 0.04}


class AccountRisk:
    def __init__(self, cfg: dict | None = None):
        self.c = {**ACCOUNT, **(cfg or {})}
        self.realized = 0.0             # B/C/D net P&L since start
        self.halted, self.halt_reason, self.flatten = False, None, False

    def equity(self, a_day_pnl: float = 0.0) -> float:
        return self.c["paper_balance"] + self.realized + a_day_pnl

    def size(self, per_lot: float, lots: int | None = None, budget: float | None = None, mult: float = 1.0) -> tuple[int, str]:
        """Lots for a position whose max loss is per_lot dollars per lot. A crew cut (mult < 1) rounds down but
        never below 1 lot; there is no size-up."""
        if per_lot <= 0:
            return 0, "no max loss"
        cap = self.c["per_position_max_loss"]
        n = int(min(budget, cap) // per_lot) if budget is not None else (lots or 1)
        if n >= 1 and mult < 1.0:
            n = max(1, int(n * mult))
        n = min(n, int(cap // per_lot))
        limit = min(cap, budget) if budget is not None else cap
        if n < 1:
            return 0, f"1 lot risks ${per_lot:.0f} > ${limit:.0f} max loss"
        return n, f"{n} x ${per_lot:.0f} max loss = ${n * per_lot:.0f} (limit ${limit:.0f})"

    def can_open(self, max_loss: float, open_risk: float, a_day_pnl: float = 0.0) -> tuple[bool, str]:
        if self.halted:
            return False, f"halted: {self.halt_reason}"
        cap = self.c["open_risk_cap"]
        if open_risk + max_loss > cap + 1e-9:
            return False, f"open-risk cap: ${open_risk:.0f} open + ${max_loss:.0f} > ${cap:.0f}"
        eq = self.equity(a_day_pnl)
        if eq < open_risk + max_loss:
            return False, f"buying power ${eq:.0f} below combined max loss ${open_risk + max_loss:.0f}"
        return True, "ok"

    def halt(self, reason: str, flatten: bool = False) -> None:
        if not self.halted:
            self.halted, self.halt_reason = True, reason
        self.flatten = self.flatten or flatten

    def on_closed(self, net: float) -> None:
        self.realized += net

    def to_dict(self, open_risk: float = 0.0, a_day_pnl: float = 0.0) -> dict:
        return {"paper_balance": self.c["paper_balance"], "equity": round(self.equity(a_day_pnl), 2),
                "open_risk": round(open_risk, 2), "open_risk_cap": self.c["open_risk_cap"],
                "per_position_max_loss": self.c["per_position_max_loss"], "halted": self.halted,
                "halt_reason": self.halt_reason}
```

`agentdesk/books/book.py`:
```python
"""One paper book's day state: open positions, trades, P&L and its own gates."""
from __future__ import annotations

from collections import deque


class Book:
    def __init__(self, key: str, cfg: dict, strategy):
        self.key, self.letter, self.c, self.strategy = key, key[0], cfg, strategy
        self.max_trades = int(cfg.get("max_trades_day", 1))
        self.open: list = []
        self.skips: deque = deque(maxlen=30)
        self.errors = 0
        self.entering = False
        self.reset_day()

    def reset_day(self) -> None:
        self.closed: list = []
        self.trades = self.wins = self.losses = 0
        self.day_pnl = 0.0
        self.halted, self.halt_reason, self.blocked = False, None, None

    def can_enter(self) -> tuple[bool, str]:
        if self.halted:
            return False, f"book halted: {self.halt_reason}"
        if self.blocked:
            return False, self.blocked
        if self.open:
            return False, "position already open"
        if self.trades >= self.max_trades:
            return False, f"max {self.max_trades} trades today"
        return True, "ok"

    def on_open(self, pos) -> None:
        self.open.append(pos)
        self.trades += 1

    def on_close(self, pos, net: float) -> None:
        self.open = [p for p in self.open if p.id != pos.id]
        self.closed.append(pos)
        self.day_pnl += net
        if net >= 0:
            self.wins += 1
        else:
            self.losses += 1

    def halt(self, reason: str) -> None:
        if not self.halted:
            self.halted, self.halt_reason = True, reason

    def to_dict(self) -> dict:
        unreal = sum(p.unrealized - p.fees for p in self.open)
        return {"book": self.letter, "key": self.key, "name": self.strategy.name, "day_pnl": round(self.day_pnl, 2),
                "open_pnl": round(unreal, 2), "trades": self.trades, "max_trades": self.max_trades, "wins": self.wins,
                "losses": self.losses, "halted": self.halted, "halt_reason": self.halt_reason, "blocked": self.blocked,
                "open": [p.to_dict() for p in self.open], "closed": [p.to_dict() for p in self.closed],
                "last_skip": self.skips[-1] if self.skips else None}
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest -q tests/test_books_account.py && python -m pytest -q tests` → all pass.
- [ ] **Step 5: Commit** — `git commit -m "books: account risk (paper balance, cap, buying power) and per-book state"`

---

### Task 4: Journal book column

**Files:**
- Modify: `agentdesk/journal.py`
- Test: `tests/test_books_journal.py`

**Interfaces:**
- Consumes: `ComboPosition` (Task 1).
- Produces: `Journal.record_trade(session, mode, p, book="A")`; `trades` rows gain `book`, `legs`, `max_loss`; `pnl_pct` uses `p.risk_basis` when present.

- [ ] **Step 1: Write the failing test**

```python
import json
import sqlite3

from books_fakes import contracts
from agentdesk.books.base import Leg
from agentdesk.books.combo import ComboPosition
from agentdesk.journal import Journal

PUTS = [Leg("put", 764, "sell"), Leg("put", 762, "buy")]


def test_combo_trade_row_carries_book_legs_and_risk(tmp_path):
    j = Journal(tmp_path / "j.db")
    p = ComboPosition("C", "ORB BULL PUT", PUTS, contracts(PUTS), 1, 0.50, True, 2.0, 100.0)
    p.realized, p.fees, p.closed_ts, p.exit_reason = 20.0, 0.16, 200.0, "SPY target +0.45%"
    j.record_trade("2026-09-28", "paper", p, book="C")
    r = j.trades()[0]
    assert r["book"] == "C" and r["max_loss"] == 150.0 and len(json.loads(r["legs"])) == 2
    assert abs(r["pnl_pct"] - (20.0 - 0.16) / 150.0) < 1e-9


def test_old_database_gains_columns_and_book_a_default(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, mode TEXT, contract TEXT,"
                     " occ TEXT, setup TEXT, qty INTEGER, entry REAL, opened_ts REAL, closed_ts REAL, realized REAL,"
                     " fees REAL, pnl REAL, pnl_pct REAL, peak REAL, exit_reason TEXT, strike_reason TEXT,"
                     " entry_reasons TEXT, fills TEXT);"
                     "INSERT INTO trades (session, contract) VALUES ('2026-09-25', 'SPY 766C 09-25');")
    db.commit()
    db.close()
    r = Journal(path).trades()[0]
    assert r["book"] == "A" and r["legs"] is None
```

- [ ] **Step 2: Run to verify failure** — FAIL (`record_trade() got an unexpected keyword argument 'book'`).

- [ ] **Step 3: Implement** in `agentdesk/journal.py`: replace the single `ALTER TABLE` try-block in `__init__` with a loop, and extend `record_trade`:

```python
        for col in ("l2 TEXT", "book TEXT DEFAULT 'A'", "legs TEXT", "max_loss REAL"):
            try:
                self.db.execute(f"ALTER TABLE trades ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass

    def record_trade(self, session: str, mode: str, p, book: str = "A") -> None:
        d = p.to_dict()
        basis = getattr(p, "risk_basis", None) or p.entry * 100 * p.qty_initial
        self.db.execute(
            "INSERT INTO trades (session,mode,contract,occ,setup,qty,entry,opened_ts,closed_ts,realized,fees,pnl,pnl_pct,peak,"
            "exit_reason,strike_reason,entry_reasons,fills,l2,book,legs,max_loss) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session, mode, d["contract"], d["occ"], p.setup, p.qty_initial, p.entry, p.opened_ts, p.closed_ts,
             p.realized, p.fees, p.realized - p.fees, (p.realized - p.fees) / basis if basis else 0, p.peak,
             p.exit_reason, p.strike_reason, json.dumps(p.entry_reasons), json.dumps(p.fills), json.dumps(p.l2),
             book, json.dumps(d["legs"]) if "legs" in d else None, getattr(p, "risk_basis", None)))
        self.db.commit()
```
Also add `book TEXT DEFAULT 'A', legs TEXT, max_loss REAL` to the `CREATE TABLE trades` in `SCHEMA` after `l2 TEXT` (the ALTERs then no-op on new files).

- [ ] **Step 4: Run to verify pass** — both new tests pass; full suite passes (book A's `record_trade` call is unchanged and defaults to `"A"`).
- [ ] **Step 5: Commit** — `git commit -m "journal: book, legs and max_loss columns"`

---

### Task 5: BookHost (entries, management, forced exits, kill, isolation)

**Files:**
- Create: `agentdesk/books/host.py`
- Modify: `tests/books_fakes.py` (add `FakeFeed`, `RecBus`, `FakeEngine`)
- Test: `tests/test_books_host.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4; engine attributes `cfg, quotes, broker, bus, journal, risk, price, vwap, open, crew, symbol, mode, feed`.
- Produces: `BookHost(engine, cfg, vix=None, reviewer=None, books=None)` with `.books`, `.account`, `.enabled`, `positions()`, `open_risk()`, `async start()`, `async on_bar(bar)`, `async on_second(now)`, `halt_all(reason, flatten=False)`, `async kill(now)`, `async flatten(reason, now)`, `snapshot()`, `summary()`. Bus events: `book_order`, `book_position` (`event` open/update), `book_closed`, `book_skip`, `books`, `log`.

- [ ] **Step 1: Add fakes to `tests/books_fakes.py`**

```python
from datetime import date, time

from agentdesk.brokers.paper import PaperBroker
from agentdesk.clock import at_ct
from agentdesk.config import load_config
from agentdesk.journal import Journal
from agentdesk.bars import VWAP
from agentdesk.risk import RiskManager

DAY = date(2026, 9, 28)


def ct_ts(h, m, s=0, day=DAY):
    return at_ct(day, time(h, m, s))


class FakeFeed:
    is_sim, name = False, "fake"

    def __init__(self):
        self.t = ct_ts(8, 30)

    def now(self):
        return self.t

    async def history_1m(self, days):
        return []


class RecBus:
    def __init__(self):
        self.events = []

    def emit(self, type_, ts, **data):
        self.events.append((type_, ts, data))

    def of(self, type_):
        return [d for t, _, d in self.events if t == type_]


class FakeEngine:
    def __init__(self, quotes, cfg=None):
        self.cfg = cfg or load_config()
        self.quotes, self.broker = quotes, PaperBroker(quotes)
        self.bus, self.journal = RecBus(), Journal(None)
        self.risk = RiskManager(self.cfg)
        self.price, self.vwap = 765.0, VWAP()
        self.vwap.add(765.0, 1)
        self.open, self.crew, self.symbol, self.mode, self.feed = [], None, "SPY", "paper", FakeFeed()
```

- [ ] **Step 2: Write the failing tests** (`tests/test_books_host.py`)

```python
import asyncio

import pytest

from books_fakes import FakeEngine, FakeQuotes, ct_ts
from agentdesk.books.base import ExitIntent, Leg, OrderIntent, Strategy
from agentdesk.books.book import Book
from agentdesk.books.host import BookHost

FLY = [Leg("call", 765, "sell"), Leg("put", 765, "sell"), Leg("call", 770, "buy"), Leg("put", 760, "buy")]
TIGHT = [(2.00, 2.02), (1.90, 1.92), (0.40, 0.41), (0.35, 0.36)]


def run(c):
    return asyncio.run(c)


class Scripted(Strategy):
    name = "TEST"

    def __init__(self, cfg=None, intent=None):
        super().__init__(cfg or {})
        self.intent, self.exit, self.boom, self.failed = intent, None, False, 0

    def on_clock(self, now, ctx):
        if self.boom:
            raise RuntimeError("boom")
        it, self.intent = self.intent, None
        return it

    def on_quote(self, pos, cq, now, ctx):
        return self.exit

    def entry_failed(self, now):
        self.failed += 1


def setup(intents=("B",), rows=TIGHT):
    fq = FakeQuotes(now=ct_ts(8, 45))
    for l, (b, a) in zip(FLY, rows):
        fq.set(l.right, l.strike, b, a)
    eng = FakeEngine(fq)
    books = [Book(f"{k}_test", {"max_trades_day": 1}, Scripted(intent=OrderIntent(list(FLY), True, 5.0, "test fly", lots=1)))
             for k in intents]
    return eng, fq, BookHost(eng, eng.cfg, books=books)


def tick(host, fq, now):
    fq.now = now
    run(host.on_second(now))


def test_entry_opens_combo_at_paper_fill_with_fees():
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    (pos,) = host.positions()
    assert pos.entry == pytest.approx(3.13) and pos.qty == 1 and pos.fees == pytest.approx(0.16)
    assert pos.fills[0]["mid"] == pytest.approx(3.16) and pos.fills[0]["natural"] == pytest.approx(3.13)
    assert eng.bus.of("book_position")[0]["event"] == "open"
    assert host.open_risk() == pytest.approx((5 - 3.13) * 100)


def test_exit_closes_journals_and_books_pnl():
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    host.books[0].strategy.exit = ExitIntent("take profit 50%")
    for l, (b, a) in zip(FLY, [(0.90, 0.92), (0.80, 0.82), (0.10, 0.11), (0.08, 0.09)]):
        fq.set(l.right, l.strike, b, a)
    tick(host, fq, ct_ts(9, 30))
    assert not host.positions()
    row = eng.journal.trades()[0]
    assert row["book"] == "B" and row["exit_reason"] == "take profit 50%"
    b = host.books[0]
    assert b.wins == 1 and b.day_pnl == pytest.approx(host.account.realized)
    assert eng.bus.of("book_closed")[0]["pos"]["book"] == "B"


def test_open_risk_cap_skips_entry():
    eng, fq, host = setup()
    host.account.c["open_risk_cap"] = 100
    tick(host, fq, ct_ts(8, 45))
    assert not host.positions() and "open-risk cap" in eng.bus.of("book_skip")[0]["why"]


def test_book_a_open_debit_counts_toward_open_risk():
    eng, fq, host = setup()

    class APos:
        entry, qty = 2.50, 4
    eng.open = [(APos(), None)]
    assert host.open_risk() == pytest.approx(1000.0)


def test_kill_flattens_every_book_and_blocks_entries():
    eng, fq, host = setup(intents=("B", "D"))
    tick(host, fq, ct_ts(8, 45))
    assert len(host.positions()) == 2
    run(host.kill(ct_ts(9, 0)))
    assert not host.positions() and host.account.halted
    assert all(r["exit_reason"] == "KILL switch" for r in eng.journal.trades())


def test_engine_safety_trip_flattens_books_but_a_daily_loss_halt_does_not():
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    eng.risk.halt("daily loss limit -$400")                 # book A's own halt
    tick(host, fq, ct_ts(8, 46))
    assert host.positions()
    eng.risk.halt("SAFETY: stale quote", flatten=True)      # what Engine._trip / kill set
    tick(host, fq, ct_ts(8, 47))
    assert not host.positions()


def test_forced_flatten_times():
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    tick(host, fq, ct_ts(14, 40))
    assert eng.journal.trades()[0]["exit_reason"] == "flatten 14:40 CT"


def test_sellout_forces_exit_five_minutes_early():
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    for c in host.positions()[0].contracts:
        c.sellout_ts = ct_ts(10, 0)
    tick(host, fq, ct_ts(9, 55))
    assert "sellout" in eng.journal.trades()[0]["exit_reason"]


def test_stale_leg_quote_makes_no_exit_decision():
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    pos = host.positions()[0]
    host.books[0].strategy.exit = ExitIntent("would exit")
    fq.set("put", 760, 0.35, 0.36, ts=ct_ts(8, 45))         # 15 s old at 08:45:15
    tick(host, fq, ct_ts(8, 45, 15))
    assert host.positions() and pos.last_quote_ts == ct_ts(8, 45)


def test_a_book_that_keeps_raising_halts_alone():               # review focus 4
    eng, fq, host = setup(intents=("B", "D"))
    tick(host, fq, ct_ts(8, 45))
    host.books[0].strategy.boom = True
    for s in range(1, 5):
        tick(host, fq, ct_ts(8, 46, s))
    b, d = host.books
    assert b.halted and "boom" in b.halt_reason and not b.open
    assert not d.halted and d.open and not host.account.halted and not eng.risk.st.halted


def test_partial_fill_blocks_new_orders():
    eng, fq, host = setup()

    async def partial(*a, **k):
        from agentdesk.brokers.base import OrderResult
        return OrderResult("partial", 1, 3.13, "x", raw={"mid": 3.16, "natural": 3.13, "ref_id": "r", "tries": 1})
    host.books[0].strategy.intent = OrderIntent(list(FLY), True, 5.0, "two lots", lots=2)
    host.account.c["per_position_max_loss"] = 1000
    host.exec.work = partial
    tick(host, fq, ct_ts(8, 45))
    b = host.books[0]
    assert b.open[0].qty == 1 and "partial" in b.blocked and not b.can_enter()[0]


def test_unfillable_entry_tells_strategy_to_retry():            # review focus 3 (host side)
    eng, fq, host = setup()
    fq.set("put", 760, 0.35, 0.36, ts=ct_ts(8, 40))           # stale leg at 08:45
    tick(host, fq, ct_ts(8, 45))
    assert not host.positions() and host.books[0].strategy.failed == 1


def test_early_close_day_blocks_late_entries_and_flattens_1140():
    eng, fq, host = setup()
    eng.cfg["calendar"]["early_close"] = ["2026-09-28"]
    tick(host, fq, ct_ts(8, 45))
    tick(host, fq, ct_ts(11, 40))
    assert "11:40" in eng.journal.trades()[0]["exit_reason"]


def test_snapshot_lists_book_a_first():
    eng, fq, host = setup()
    snap = host.snapshot()
    assert [b["book"] for b in snap["books"]] == ["A", "B"] and "open_risk" in snap["account"]
```

- [ ] **Step 3: Run to verify failure** — FAIL (`No module named 'agentdesk.books.host'`).

- [ ] **Step 4: Implement `agentdesk/books/host.py`**

```python
"""Runs paper books B, C, D next to the engine's book A.

The engine calls on_bar / on_second / kill / flatten / halt_all; book A's own path is untouched. Every fill here is
paper (PaperBroker.submit_combo); in shadow/live the first price of each order also goes to review_option_order.
One book's error halts and flattens that book only. A stale combo quote is caught by the engine's watchdog, which
halts everything, as for book A.
"""
from __future__ import annotations

import logging
from datetime import time

from ..brokers.paper import PaperBroker
from ..clock import ct_time, is_rth, session_date
from ..config import hhmm
from ..exits import Contract
from .account import AccountRisk
from .base import ExitIntent, MarketContext, OrderIntent, Skip
from .book import Book
from .combo import ComboPosition, ComboQuote, leg_problem, paper_fair
from .fills import ComboExecutor
from .legs import fetch_quotes

log = logging.getLogger("agentdesk.books")
FILLS = {"model": "mid_offset", "cents_per_leg": 1, "reprice_step_c": 1, "max_reprices": 4,
         "max_quote_age_s": 5, "max_leg_spread_pct": 0.25, "tick_exempt": 0.02}
HIST_SEED = 424242


def strategy_class(key: str):
    from .iron_condor import IronCondor
    from .iron_fly import IronFly
    from .orb_bull_put import OrbBullPut
    return {"B_iron_fly": IronFly, "C_orb_bull_put": OrbBullPut, "D_iron_condor": IronCondor}.get(key)


def build_books(cfg) -> list[Book]:
    out = []
    for key, bc in (cfg.get("books") or {}).items():
        cls = strategy_class(key) if isinstance(bc, dict) else None
        if cls is None or not bc.get("enabled"):
            continue
        if bc.get("paper_only") is not True:
            raise SystemExit(f"books.{key}: only a paper path exists for this book; set paper_only: true")
        out.append(Book(key, bc, cls(bc)))
    return out


class BookHost:
    def __init__(self, engine, cfg, vix=None, reviewer=None, books: list[Book] | None = None):
        self.e, self.cfg = engine, cfg
        bc = cfg.get("books") or {}
        self.account = AccountRisk(bc.get("account"))
        self.f = {**FILLS, **(bc.get("fills") or {})}
        self.broker = PaperBroker(engine.quotes)
        self.broker.combo_model, self.broker.combo_cents = self.f["model"], self.f["cents_per_leg"] / 100
        self.exec = ComboExecutor(self.broker, self.f, reviewer)
        self.vix = vix
        self.books = build_books(cfg) if books is None else books
        self.fee = self.account.c["fee_per_leg"]
        self.max_errors = (cfg["risk"].get("watchdog") or {}).get("max_consecutive_errors", 3)
        self.day = None
        self.vix_prev, self._vix_day, self._vix_try = None, None, -1e18
        self._last_manage, self._last_emit, self._busy = 0.0, 0.0, False

    @property
    def enabled(self) -> bool:
        return bool(self.books)

    def positions(self) -> list:
        return [p for b in self.books for p in b.open]

    def open_risk(self) -> float:
        a = sum(p.entry * 100 * p.qty for p, _ in self.e.open)
        return sum(p.max_loss for p in self.positions()) + a

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        try:
            hist = await self._history()
        except Exception as ex:
            hist = []
            self._log(self.e.feed.now(), "warn", f"books: history load failed ({ex}); indicators warm up live")
        for b in self.books:
            b.strategy.warmup(hist)

    async def _history(self) -> list:
        f = self.e.feed
        days = (self.cfg.get("books") or {}).get("history_days", 20)
        if f.is_sim:           # a separate generator: book A's sim day must not change
            from ..feeds.sim import SimFeed
            return await SimFeed(day=f.day, seed=HIST_SEED, start_px=f.px).history_1m(days)
        return await f.history_1m(days)

    def _new_day(self, d) -> None:
        self.day = d
        for b in self.books:
            b.reset_day()
            b.strategy.new_day(d)

    # ------------------------------------------------------------ engine hooks
    async def on_bar(self, bar) -> None:
        if bar.tf not in ("1m", "5m"):
            return
        now = bar.end or bar.t
        if session_date(now) != self.day:
            self._new_day(session_date(now))
        for book in self.books:
            await self._safe(book, self._book_bar(book, bar, now), now)

    async def on_second(self, now: float) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            if session_date(now) != self.day:
                self._new_day(session_date(now))
            await self._ensure_vix(now)
            if is_rth(now):
                for book in self.books:
                    await self._safe(book, self._book_clock(book, now), now)
            poll = self.cfg["robinhood"]["quote_poll_ms"] / 1000
            if self.positions() and now - self._last_manage >= poll:
                self._last_manage = now
                for book in self.books:
                    if book.open:
                        await self._safe(book, self._manage(book, now), now)
        finally:
            self._busy = False

    def halt_all(self, reason: str, flatten: bool = False) -> None:
        self.account.halt(reason, flatten)

    async def kill(self, now: float) -> None:
        self.account.halt("KILL switch", flatten=True)
        await self.flatten("KILL switch", now)

    async def flatten(self, reason: str, now: float) -> None:
        for book in self.books:
            for pos in list(book.open):
                try:
                    await self._exit(book, pos, ExitIntent(reason, urgent=True), now)
                except Exception as ex:          # keep going: the other positions still matter
                    log.exception("flatten %s failed", pos.label)
                    self._log(now, "error", f"flatten {pos.label} failed: {ex}")

    # ------------------------------------------------------------ per book
    async def _safe(self, book: Book, coro, now: float) -> None:
        try:
            await coro
            book.errors = 0
        except Exception as ex:
            book.errors += 1
            log.exception("book %s failed (%d in a row)", book.letter, book.errors)
            self._log(now, "error", f"book {book.letter}: {ex} ({book.errors} in a row)")
            if book.errors >= self.max_errors and not book.halted:
                book.halt(f"{book.errors} errors in a row: {ex}")
                self._log(now, "error", f"book {book.letter} halted and flattening: {book.halt_reason}")
                for pos in list(book.open):
                    try:
                        await self._exit(book, pos, ExitIntent(f"book halted: {book.halt_reason}", urgent=True), now)
                    except Exception:
                        log.exception("book %s flatten failed", book.letter)

    async def _book_bar(self, book: Book, bar, now: float) -> None:
        await self._act(book, book.strategy.on_bar(bar.tf, bar, self._ctx(now, book)), now)

    async def _book_clock(self, book: Book, now: float) -> None:
        await self._act(book, book.strategy.on_clock(now, self._ctx(now, book)), now)

    async def _act(self, book: Book, out, now: float) -> None:
        if isinstance(out, OrderIntent):
            await self._enter(book, out, now)
        elif isinstance(out, Skip):
            self._skip(book, now, out.reason)
        elif isinstance(out, ExitIntent):
            for pos in list(book.open):
                await self._exit(book, pos, out, now)

    def _ctx(self, now: float, book: Book) -> MarketContext:
        d = session_date(now)
        st = self.e.risk.st
        before = self.cfg["risk"]["event_blackout"]["before_min"] * 60
        events = [(b.start + before, b.name) for b in st.blackouts if session_date(b.start + before) == d]
        return MarketContext(now=now, day=d, spot=self.e.price, vwap=self.e.vwap.value, events=events,
                             vix_prev=self.vix_prev if self._vix_day == d else None, vix1d_flag=self._vix1d_flag(d),
                             size_mult=min(1.0, st.size_mult), open=list(book.open))

    def _vix1d_flag(self, d) -> bool | None:
        crew = self.e.crew
        b = (crew.briefs.get("vol") if crew else None) or {}
        if b.get("day") != str(d) or "vix1d_flag" not in b:
            return None
        return bool(b["vix1d_flag"])

    async def _ensure_vix(self, now: float) -> None:
        d = session_date(now)
        if self.vix is None or self._vix_day == d or now - self._vix_try < 60:
            return
        self._vix_try = now
        try:
            v = await self.vix.prior_close(d)
        except Exception as ex:
            v = None
            self._log(now, "warn", f"books: prior VIX close unavailable ({ex})")
        if v:
            self.vix_prev, self._vix_day = float(v), d
            self._log(now, "info", f"books: prior VIX close {self.vix_prev:.2f}")

    # ------------------------------------------------------------ entries
    def _gate(self, book: Book, now: float) -> tuple[bool, str]:
        st = self.e.risk.st
        if self.account.halted:
            return False, f"halted: {self.account.halt_reason}"
        if st.halted and st.flatten_all:
            return False, f"halted: {st.halt_reason}"
        if st.paused:
            return False, "paused"
        ok, why = book.can_enter()
        if not ok:
            return ok, why
        if self.e.risk.early_close(now) and ct_time(now) >= time(11, 20):
            return False, "early close: no entries after 11:20 CT"
        for b in st.blackouts:
            if b.start <= now < b.end:
                return False, f"blackout: {b.name}"
        return True, "ok"

    async def _enter(self, book: Book, it: OrderIntent, now: float) -> None:
        if book.entering:
            return
        ok, why = self._gate(book, now)
        if not ok:
            return self._skip(book, now, why)
        book.entering = True
        try:
            exp = str(session_date(now))
            cs = [await self.e.broker.resolve(Contract(self.e.symbol, exp, float(l.strike), l.right)) for l in it.legs]
            cq = await self._quote(it.legs, cs, now, opening=True)
            if cq.problem:
                book.strategy.entry_failed(now)
                return self._skip(book, now, f"quote: {cq.problem}")
            fair = paper_fair(cq, it.credit, True, self.f["model"], self.f["cents_per_leg"] / 100)
            if it.credit and fair <= 0:
                return self._skip(book, now, "no credit at the expected fill")
            per_lot = round(((it.width - fair) if it.credit else fair) * 100, 2)
            lots, size_why = self.account.size(per_lot, it.lots, it.budget, min(1.0, self.e.risk.st.size_mult))
            if lots < 1:
                return self._skip(book, now, size_why)
            ok, why = self.account.can_open(per_lot * lots, self.open_risk(), self.e.risk.st.day_pnl)
            if not ok:
                return self._skip(book, now, why)
            res = await self.exec.work(it.legs, cs, lots, it.credit, True, False, now)
            self._order_event(book, now, "open", lots, res, it.reason)
            if res.filled_qty <= 0:
                book.strategy.entry_failed(now)
                return self._skip(book, now, f"entry not filled ({res.message or res.status})")
            pos = ComboPosition(book.letter, book.strategy.name, list(it.legs), cs, res.filled_qty, res.avg_price,
                                it.credit, it.width, now, strike_reason=it.reason,
                                entry_reasons=[size_why] + list(it.meta.get("notes", [])), meta=dict(it.meta))
            pos.fees = self.fee * sum(l.ratio for l in it.legs) * res.filled_qty
            pos.fills.append(self._fill(now, "open", res, "entry"))
            c = book.c
            if "stop_debit_x_credit" in c:
                pos.stop = round(pos.entry * c["stop_debit_x_credit"], 2)
                pos.target = round(pos.entry * (1 - c["take_profit_pct"]), 2)
            pos.meta["plan"] = book.strategy.plan(pos)
            if res.status == "partial":
                book.blocked = f"partial fill {res.filled_qty}/{lots}: reconcile before new orders"
            book.on_open(pos)
            self.e.bus.emit("book_position", now, pos=pos.to_dict(), event="open", size_note=size_why)
            self._emit_books(now)
        finally:
            book.entering = False

    async def _quote(self, legs, contracts, now: float, opening: bool) -> ComboQuote:
        qs = await fetch_quotes(self.e.quotes, contracts)
        for c, q in zip(contracts, qs):
            p = leg_problem(q, now, self.f["max_quote_age_s"], self.f["max_leg_spread_pct"], self.f["tick_exempt"], opening)
            if p:
                return ComboQuote(legs, qs, f"{c.label}: {p}")
        return ComboQuote(legs, qs)

    # ------------------------------------------------------------ management
    async def _manage(self, book: Book, now: float) -> None:
        for pos in list(book.open):
            cq = await self._quote(pos.legs, pos.contracts, now, opening=False)
            if cq.problem:
                continue            # the engine watchdog halts everything if this lasts quote_stale_sec
            pos.last_quote_ts = now
            pos.mark = round(cq.mid(pos.credit), 3)
            pos.peak = min(pos.peak, pos.mark) if pos.credit else max(pos.peak, pos.mark)
            forced = self._forced(book, pos, now)
            it = ExitIntent(forced, urgent=True) if forced else book.strategy.on_quote(pos, cq, now, self._ctx(now, book))
            if it:
                await self._exit(book, pos, it, now)
            elif now - self._last_emit >= 2:
                self._last_emit = now
                self.e.bus.emit("book_position", now, pos=pos.to_dict(), event="update")

    def _forced(self, book: Book, pos, now: float) -> str | None:
        st = self.e.risk.st
        if self.account.halted and self.account.flatten:
            return self.account.halt_reason
        if st.halted and st.flatten_all:
            return st.halt_reason or "kill switch"
        if book.halted:
            return f"book halted: {book.halt_reason}"
        t = ct_time(now)
        if self.e.risk.early_close(now) and t >= time(11, 40):
            return "early close: flat 11:40 CT"
        fl = self.cfg["exits"]["flatten_at"]
        if t >= hhmm(fl):
            return f"flatten {fl} CT"
        so = min((c.sellout_ts for c in pos.contracts if getattr(c, "sellout_ts", None)), default=None)
        if so and now >= so - 300:
            return "5 min before Robinhood's sellout time"
        return None

    async def _exit(self, book: Book, pos, it: ExitIntent, now: float) -> None:
        if pos.status != "open" or pos.exiting or pos.qty <= 0:
            return
        pos.exiting = True
        try:
            res = await self.exec.work(pos.legs, pos.contracts, pos.qty, pos.credit, False, it.urgent, now)
            self._order_event(book, now, "close", pos.qty, res, it.reason)
            if res.filled_qty <= 0:
                self._log(now, "warn", f"book {book.letter} exit not filled ({it.reason}): {res.message or res.status}")
                return
            n = res.filled_qty
            pos.realized += pos.pnl_per_share(res.avg_price) * 100 * n
            pos.fees += self.fee * sum(l.ratio for l in pos.legs) * n
            pos.qty -= n
            pos.fills.append(self._fill(now, "close", res, it.reason))
            if pos.qty <= 0:
                self._close(book, pos, it.reason, now)
            else:
                book.blocked = f"partial exit, {pos.qty} left: reconcile before new orders"
        finally:
            pos.exiting = False

    def _close(self, book: Book, pos, reason: str, now: float) -> None:
        pos.status, pos.closed_ts, pos.exit_reason = "closed", now, reason
        net = pos.realized - pos.fees
        book.on_close(pos, net)
        self.account.on_closed(net)
        self.e.journal.record_trade(str(self.day), self.e.mode, pos, book=book.letter)
        book.strategy.on_closed(pos, now)
        self.e.bus.emit("book_closed", now, pos=pos.to_dict(), net=round(net, 2))
        self._emit_books(now)

    # ------------------------------------------------------------ events / snapshot
    def _fill(self, now, side, res, why) -> dict:
        r = res.raw or {}
        return {"ts": now, "side": side, "qty": res.filled_qty, "px": res.avg_price, "mid": r.get("mid"),
                "natural": r.get("natural"), "limit": r.get("limit"), "ref_id": r.get("ref_id"), "why": why}

    def _order_event(self, book, now, action, qty, res, why) -> None:
        self.e.bus.emit("book_order", now, book=book.letter, action=action, qty=qty, status=res.status,
                        filled=res.filled_qty, price=res.avg_price, limit=(res.raw or {}).get("limit"),
                        mid=(res.raw or {}).get("mid"), natural=(res.raw or {}).get("natural"), why=why,
                        review=res.review)

    def _skip(self, book: Book, now: float, why: str) -> None:
        if book.skips and book.skips[-1]["why"] == why:
            return                 # same reason as last time: already logged
        item = {"ts": now, "book": book.letter, "why": why}
        book.skips.append(item)
        self.e.bus.emit("book_skip", now, **item)

    def _log(self, now: float, level: str, msg: str) -> None:
        self.e.bus.emit("log", now, level=level, msg=msg)

    def _a_summary(self) -> dict:
        st = self.e.risk.st
        return {"book": "A", "key": "A_macd_calls", "name": "MACD CALLS", "day_pnl": round(st.day_pnl, 2),
                "open_pnl": round(sum(p.unrealized for p, _ in self.e.open), 2), "trades": st.trades,
                "max_trades": self.cfg["risk"]["max_trades_per_day"], "wins": st.wins, "losses": st.losses,
                "halted": st.halted, "halt_reason": st.halt_reason, "blocked": None, "open": [], "closed": [],
                "last_skip": None}

    def summary(self) -> list[dict]:
        return [self._a_summary()] + [{k: v for k, v in b.to_dict().items() if k not in ("open", "closed")}
                                      for b in self.books]

    def _emit_books(self, now: float) -> None:
        self.e.bus.emit("books", now, books=self.summary(),
                        account=self.account.to_dict(self.open_risk(), self.e.risk.st.day_pnl))

    def snapshot(self) -> dict:
        return {"books": [self._a_summary()] + [b.to_dict() for b in self.books],
                "account": self.account.to_dict(self.open_risk(), self.e.risk.st.day_pnl)}
```

- [ ] **Step 5: Run to verify pass** — `python -m pytest -q tests/test_books_host.py && python -m pytest -q tests` → all pass. (`strategy_class` imports the three strategy modules lazily; the host tests inject `books=` so they don't need them yet.)
- [ ] **Step 6: Commit** — `git commit -m "books: BookHost with entries, management, forced exits, kill and per-book isolation"`

---

### Task 6: Book B, iron fly

**Files:**
- Create: `agentdesk/books/iron_fly.py`
- Test: `tests/test_book_b.py`

**Interfaces:**
- Consumes: `Strategy`, `Leg`, `OrderIntent`, `Skip`, `credit_exit`, `mins`, `MarketContext` (Task 1); `BookHost`, `FakeEngine` (Task 5).
- Produces: `IronFly(cfg)` with `name = "IRON FLY"`.

- [ ] **Step 1: Write the failing tests**

```python
import asyncio

import pytest

from books_fakes import DAY, FakeEngine, FakeQuotes, ct_ts
from agentdesk.books.base import MarketContext
from agentdesk.books.book import Book
from agentdesk.books.combo import ComboQuote
from agentdesk.books.host import BookHost
from agentdesk.books.iron_fly import IronFly
from agentdesk.config import load_config

C = load_config()["books"]["B_iron_fly"]


def ctx(now, spot=765.3, events=(), vix1d=None):
    return MarketContext(now=now, day=DAY, spot=spot, vwap=765.0, events=list(events), vix1d_flag=vix1d)


def test_enters_at_0845_atm_with_5_wings_once():
    s = IronFly(C)
    assert s.on_clock(ct_ts(8, 44, 59), ctx(ct_ts(8, 44, 59))) is None
    it = s.on_clock(ct_ts(8, 45), ctx(ct_ts(8, 45)))
    assert [(l.right, l.strike, l.side) for l in it.legs] == [("call", 765, "sell"), ("put", 765, "sell"),
                                                              ("call", 770, "buy"), ("put", 760, "buy")]
    assert it.credit and it.width == 5 and it.lots == 1
    assert "VIX1D unavailable" in " ".join(it.meta["notes"])
    assert s.on_clock(ct_ts(8, 45, 1), ctx(ct_ts(8, 45, 1))) is None


def test_skips_event_before_1400_and_vix1d_flag():
    s = IronFly(C)
    out = s.on_clock(ct_ts(8, 45), ctx(ct_ts(8, 45), events=[(ct_ts(13, 0), "FOMC")]))
    assert "FOMC" in out.reason
    s.new_day(DAY)
    assert s.on_clock(ct_ts(8, 45), ctx(ct_ts(8, 45), events=[(ct_ts(14, 30), "late speaker")])).legs
    s.new_day(DAY)
    assert "VIX1D" in s.on_clock(ct_ts(8, 45), ctx(ct_ts(8, 45), vix1d=True)).reason


def test_restart_after_grace_skips_the_day():                     # review focus 2
    s = IronFly(C)
    assert "missed" in s.on_clock(ct_ts(10, 15), ctx(ct_ts(10, 15))).reason
    assert s.on_clock(ct_ts(10, 16), ctx(ct_ts(10, 16))) is None


def test_transient_failure_retries_inside_grace_only():            # review focus 3
    s = IronFly(C)
    assert s.on_clock(ct_ts(8, 45), ctx(ct_ts(8, 45))).legs
    s.entry_failed(ct_ts(8, 45))
    assert s.on_clock(ct_ts(8, 45, 1), ctx(ct_ts(8, 45, 1))).legs
    s.entry_failed(ct_ts(8, 45, 1))
    assert "missed" in s.on_clock(ct_ts(8, 51), ctx(ct_ts(8, 51))).reason


def test_exits_take_profit_stop_and_1430():
    s = IronFly(C)

    class P:
        entry = 3.00
    q = lambda mid: type("CQ", (), {"mid": lambda self, credit: mid})()
    assert s.on_quote(P, q(1.50), ct_ts(10, 0), None).reason.startswith("take profit")
    assert s.on_quote(P, q(6.00), ct_ts(10, 0), None).urgent
    assert "14:30" in s.on_quote(P, q(2.50), ct_ts(14, 30), None).reason
    assert s.on_quote(P, q(2.50), ct_ts(14, 29), None) is None


def test_b_trades_one_fly_through_the_host_and_no_duplicate():
    fq = FakeQuotes(now=ct_ts(8, 45))
    for right, k, b, a in (("call", 765, 2.00, 2.02), ("put", 765, 1.90, 1.92), ("call", 770, 0.40, 0.41), ("put", 760, 0.35, 0.36)):
        fq.set(right, k, b, a)
    eng = FakeEngine(fq)
    eng.price = 765.3
    host = BookHost(eng, eng.cfg, books=[Book("B_iron_fly", C, IronFly(C))])
    for s in range(0, 3):
        fq.now = ct_ts(8, 45, s)
        asyncio.run(host.on_second(ct_ts(8, 45, s)))
    assert len(host.positions()) == 1 and host.books[0].trades == 1
    assert host.positions()[0].stop == pytest.approx(6.26) and host.positions()[0].target == pytest.approx(1.56)
```

- [ ] **Step 2: Run to verify failure** — FAIL (`No module named 'agentdesk.books.iron_fly'`).

- [ ] **Step 3: Implement `agentdesk/books/iron_fly.py`**

```python
"""Book B, iron fly (HANDOFF 7B, kept exactly as Evan specified).

08:45 CT: sell the ATM call and put, buy wings +-$5, one 4-leg credit order. Skip days with a high-impact event
before 14:00 CT and days the Vol desk flags VIX1D more than 3 points above VIX. Take profit at 50% of credit; stop
when the debit to close reaches 2x credit (a loss of 1x credit); close by 14:30 CT. 1 lot.
"""
from __future__ import annotations

from ..clock import ct_time
from ..config import hhmm
from .base import Leg, OrderIntent, Skip, Strategy, credit_exit, mins


class IronFly(Strategy):
    name = "IRON FLY"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.decided = None

    def new_day(self, day) -> None:
        self.decided = None

    def on_clock(self, now, ctx):
        if self.decided == ctx.day:
            return None
        t, start = ct_time(now), hhmm(self.c["entry_ct"])
        if t < start:
            return None
        self.decided = ctx.day
        if mins(t) - mins(start) > self.c.get("entry_grace_min", 5):
            return Skip(f"missed the {self.c['entry_ct']} CT entry")
        cutoff = hhmm(self.c.get("skip_event_before_ct", "14:00"))
        ev = [n for ts, n in ctx.events if ct_time(ts) < cutoff]
        if ev:
            return Skip(f"high-impact event before {self.c.get('skip_event_before_ct', '14:00')} CT: {ev[0]}")
        if ctx.vix1d_flag:
            return Skip(f"Vol desk: VIX1D more than {self.c.get('vix1d_gap', 3):g} points above VIX")
        if not ctx.spot:
            return Skip("no SPY price")
        k, w = float(round(ctx.spot)), float(self.c["wings"])
        notes = [] if ctx.vix1d_flag is not None else ["VIX1D unavailable: check skipped"]
        return OrderIntent([Leg("call", k, "sell"), Leg("put", k, "sell"), Leg("call", k + w, "buy"), Leg("put", k - w, "buy")],
                           credit=True, width=w, reason=f"ATM {k:g} iron fly, wings +-{w:g}",
                           lots=int(self.c.get("lots", 1)), meta={"notes": notes})

    def entry_failed(self, now) -> None:
        self.decided = None            # try again next second; the grace window still bounds it

    def on_quote(self, pos, cq, now, ctx):
        return credit_exit(pos, cq.mid(True), now, self.c)

    def plan(self, pos) -> str:
        return (f"take profit at {pos.entry * (1 - self.c['take_profit_pct']):.2f} debit, stop at "
                f"{pos.entry * self.c['stop_debit_x_credit']:.2f}, close {self.c['close_ct']} CT")
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest -q tests/test_book_b.py && python -m pytest -q tests`.
- [ ] **Step 5: Commit** — `git commit -m "book B: iron fly"`

---

### Task 7: Book D, iron condor, and the prior VIX close

**Files:**
- Create: `agentdesk/books/iron_condor.py`, `agentdesk/books/vol.py`
- Test: `tests/test_book_d.py`

**Interfaces:**
- Consumes: Task 1 base; `Bar` from `agentdesk/bars.py`.
- Produces: `IronCondor(cfg)` (`name = "IRON CONDOR"`), `expected_move(spot, vix, now, rth_mult=0.80) -> float`; `SimVix(feed)`, `RobinhoodVix(rh)` with `async prior_close(day) -> float | None`.

- [ ] **Step 1: Write the failing tests**

```python
import asyncio
from datetime import date, timedelta

import pytest

from books_fakes import DAY, ct_ts
from agentdesk.bars import Bar
from agentdesk.books.base import MarketContext
from agentdesk.books.iron_condor import IronCondor, expected_move
from agentdesk.books.vol import RobinhoodVix, SimVix
from agentdesk.config import load_config

C = load_config()["books"]["D_iron_condor"]


def ctx(now, spot=765.0, vwap=765.2, vix=15.0, day=DAY):
    return MarketContext(now=now, day=day, spot=spot, vwap=vwap, vix_prev=vix)


def opening_range(s, day, rng, close=765.0):
    """30 one-minute bars 08:30-08:59 CT whose high-low range is rng dollars, last close = close."""
    for i in range(30):
        t = ct_ts(8, 30 + i, day=day)
        s.on_bar("1m", Bar("1m", t, close, close + (rng if i == 10 else 0), close - (0 if i != 20 else 0), close, 100, 1, t + 60), None)


def warm(s, n=14, rng=4.0):
    d, days = DAY, []
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    for d in reversed(days):
        opening_range(s, d, rng)


def test_expected_move_matches_backtest_formula():
    assert expected_move(765.0, 15.0, ct_ts(9, 0)) == pytest.approx(5.6705, abs=1e-3)


def test_strikes_at_09_quiet_day():
    s = IronCondor(C)
    warm(s)
    opening_range(s, DAY, 2.0)                                   # 2/765 < 4/765 median: quiet
    it = s.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0)))
    assert [(l.right, l.strike, l.side) for l in it.legs] == [("call", 771, "sell"), ("put", 759, "sell"),
                                                              ("call", 773, "buy"), ("put", 757, "buy")]
    assert it.width == 2 and it.credit


def test_quiet_filter_uses_only_the_first_30_minutes():
    s = IronCondor(C)
    warm(s)
    opening_range(s, DAY, 6.0)                                   # wider than the median: not quiet
    assert "not quiet" in s.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0))).reason
    t = ct_ts(9, 30)                                             # later bars must not change anything
    s.on_bar("1m", Bar("1m", t, 765, 766, 764, 765, 100, 1, t + 60), None)
    assert s.days[DAY][0] - s.days[DAY][1] == pytest.approx(6.0)


def test_far_from_vwap_skips():
    s = IronCondor(C)
    warm(s)
    opening_range(s, DAY, 2.0)
    assert "VWAP" in s.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0), vwap=763.0)).reason


def test_needs_14_days_and_vix():
    s = IronCondor(C)
    warm(s, n=5)
    opening_range(s, DAY, 2.0)
    assert "14 prior days" in s.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0))).reason
    s2 = IronCondor(C)
    warm(s2)
    opening_range(s2, DAY, 2.0)
    assert "VIX" in s2.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0), vix=None)).reason


def test_waits_for_the_0859_bar_then_skips_without_it():            # review focus 2
    s = IronCondor(C)
    warm(s)
    assert s.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0))) is None           # no bars yet today: wait
    assert "08:30" in s.on_clock(ct_ts(9, 6), ctx(ct_ts(9, 6))).reason   # grace over


def test_exits():
    s = IronCondor(C)

    class P:
        entry = 0.45
    q = lambda mid: type("CQ", (), {"mid": lambda self, credit: mid})()
    assert s.on_quote(P, q(0.22), ct_ts(10, 0), None).reason.startswith("take profit")
    assert s.on_quote(P, q(0.90), ct_ts(10, 0), None).urgent
    assert "14:25" in s.on_quote(P, q(0.40), ct_ts(14, 25), None).reason


def test_vix_sources():
    assert asyncio.run(SimVix(type("F", (), {"base_iv": 0.16})()).prior_close(DAY)) == 16.0

    class RH:
        async def start(self):
            pass

        async def call(self, tool, args):
            if tool == "get_indexes":
                return {"indexes": [{"id": "vix-id", "symbol": "VIX"}]}
            return {"results": [{"bars": [
                {"begins_at": "2026-09-24T00:00:00Z", "close_value": "15.67"},
                {"begins_at": "2026-09-25T00:00:00Z", "close_value": "14.87"},
                {"begins_at": "2026-09-26T00:00:00Z", "close_value": "14.87", "interpolated": True},
                {"begins_at": "2026-09-28T00:00:00Z", "close_value": "16.10"}]}]}
    assert asyncio.run(RobinhoodVix(RH()).prior_close(date(2026, 9, 28))) == 14.87
```

Note for the implementer: `opening_range` builds bars whose high exceeds the close by `rng` on bar 10 and whose low equals the close, so each day's range is exactly `rng`; the quiet measure is `range / close of the 08:59 bar`.

- [ ] **Step 2: Run to verify failure** — FAIL (missing modules).

- [ ] **Step 3: Implement**

`agentdesk/books/vol.py`:
```python
"""Prior VIX close for book D (strikes) from Robinhood's index data (read-only), or the simulator's IV."""
from __future__ import annotations

from datetime import datetime, time, timedelta


class SimVix:
    def __init__(self, feed):
        self.feed = feed

    async def prior_close(self, day) -> float | None:
        return round(getattr(self.feed, "base_iv", 0.16) * 100, 2)


class RobinhoodVix:
    def __init__(self, rh):
        self.rh, self._id = rh, None

    async def prior_close(self, day) -> float | None:
        from ..brokers.robinhood import dict_items
        await self.rh.start()
        if self._id is None:
            data = await self.rh.call("get_indexes", {"symbols": "VIX"})
            rows = dict_items(data.get("indexes", data) if isinstance(data, dict) else data)
            ids = [r.get("id") for r in rows if r.get("symbol") == "VIX" and r.get("id")]
            if not ids:
                return None
            self._id = ids[0]
        start = (datetime.combine(day, time()) - timedelta(days=10)).strftime("%Y-%m-%dT00:00:00Z")
        data = await self.rh.call("get_index_historicals", {"instrument_ids": [self._id], "start_time": start, "interval": "day"})
        res = data.get("results", []) if isinstance(data, dict) else []
        best = None
        for b in (res[0].get("bars", []) if res else []):
            if b.get("interpolated") or b.get("close_value") is None:
                continue
            if str(b.get("begins_at", ""))[:10] < str(day):
                best = float(b["close_value"])
        return best
```

`agentdesk/books/iron_condor.py`:
```python
"""Book D, 10:00 ET iron condor (HANDOFF 7D, quiet filter per Evan's 2026-09-28 answer).

09:00 CT: short call and put at +-0.9x the remaining-session expected move from the prior VIX close, wings $2
beyond, one 4-leg credit order. Quiet filter: the 08:30-09:00 CT range (over that window's last close) is below its
trailing 14-day median, and price is within 0.12% of VWAP. Take profit 50%; stop at a 2x-credit debit; close 14:25.
"""
from __future__ import annotations

import math
import statistics

from ..clock import ct_time, session_date
from ..config import hhmm
from .base import Leg, OrderIntent, Skip, Strategy, credit_exit, mins

OPEN_MIN = 8 * 60 + 30
CLOSE_MIN = 15 * 60


def expected_move(spot: float, vix: float, now: float, rth_mult: float = 0.80) -> float:
    """strategies_bcd.sd_left: remaining RTH share of the VIX-implied day plus the 15-minute SPY 0DTE tail."""
    t = ct_time(now)
    left = max(0.0, (CLOSE_MIN - (mins(t) + t.second / 60)) / 390)
    return rth_mult * vix / 100 / math.sqrt(252) * math.sqrt(left + 15 / 390) * spot


class IronCondor(Strategy):
    name = "IRON CONDOR"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.decided = None
        self.days: dict = {}        # date -> [high, low, last close, complete] over the first quiet_window_min

    def warmup(self, bars_1m: list) -> None:
        for b in bars_1m:
            self._track(b)

    def new_day(self, day) -> None:
        self.decided = None

    def on_bar(self, tf, bar, ctx):
        if tf == "1m":
            self._track(bar)
        return None

    def _track(self, b) -> None:
        if b.tf != "1m":
            return
        w = self.c.get("quiet_window_min", 30)
        m = mins(ct_time(b.t)) - OPEN_MIN
        if not 0 <= m < w:
            return
        d = session_date(b.t)
        cur = self.days.get(d)
        if cur is None:
            self.days[d] = [b.h, b.l, b.c, m == w - 1]
        else:
            cur[0], cur[1], cur[2] = max(cur[0], b.h), min(cur[1], b.l), b.c
            cur[3] = cur[3] or m == w - 1

    def _rng(self, d) -> float:
        h, l, c, _ = self.days[d]
        return (h - l) / c

    def on_clock(self, now, ctx):
        if self.decided == ctx.day:
            return None
        t, start = ct_time(now), hhmm(self.c["entry_ct"])
        if t < start:
            return None
        late = mins(t) - mins(start) > self.c.get("entry_grace_min", 5)
        quiet = self.c.get("quiet_filter", True)
        today = self.days.get(ctx.day)
        if quiet and not (today and today[3]):
            if not late:
                return None                 # the 08:59 bar hasn't closed yet
            self.decided = ctx.day
            return Skip("no complete 08:30-09:00 CT bars today")
        self.decided = ctx.day
        if late:
            return Skip(f"missed the {self.c['entry_ct']} CT entry")
        if ctx.vix_prev is None:
            return Skip("no prior VIX close")
        if not ctx.spot:
            return Skip("no SPY price")
        notes = []
        if quiet:
            n = self.c.get("quiet_lookback_days", 14)
            past = [self._rng(d) for d in sorted(d for d, v in self.days.items() if d < ctx.day and v[3])][-n:]
            if len(past) < n:
                return Skip(f"quiet filter needs {n} prior days, have {len(past)}")
            rng, med = self._rng(ctx.day), statistics.median(past)
            if rng >= med:
                return Skip(f"not quiet: 08:30-09:00 range {rng:.2%} >= {n}-day median {med:.2%}")
            vmax = self.c.get("vwap_max_pct", 0.0012)
            if ctx.vwap is None or abs(ctx.spot / ctx.vwap - 1) > vmax:
                return Skip(f"price more than {vmax:.2%} from VWAP")
            notes.append(f"quiet: range {rng:.2%} < median {med:.2%}")
        em = expected_move(ctx.spot, ctx.vix_prev, now, self.c.get("em_rth_mult", 0.80))
        d = self.c["short_em_mult"] * em
        kc, kp, w = float(math.ceil(ctx.spot + d)), float(math.floor(ctx.spot - d)), float(self.c["wings"])
        return OrderIntent([Leg("call", kc, "sell"), Leg("put", kp, "sell"), Leg("call", kc + w, "buy"), Leg("put", kp - w, "buy")],
                           credit=True, width=w, reason=f"shorts {kp:g}P/{kc:g}C at +-{self.c['short_em_mult']:g} EM "
                           f"(EM ${em:.2f}, prior VIX {ctx.vix_prev:.2f})", lots=int(self.c.get("lots", 1)),
                           meta={"notes": notes, "em": round(em, 3)})

    def entry_failed(self, now) -> None:
        self.decided = None

    def on_quote(self, pos, cq, now, ctx):
        return credit_exit(pos, cq.mid(True), now, self.c)

    def plan(self, pos) -> str:
        return (f"take profit at {pos.entry * (1 - self.c['take_profit_pct']):.2f} debit, stop at "
                f"{pos.entry * self.c['stop_debit_x_credit']:.2f}, close {self.c['close_ct']} CT")
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest -q tests/test_book_d.py && python -m pytest -q tests`.
- [ ] **Step 5: Commit** — `git commit -m "book D: iron condor with entry-time quiet filter; prior VIX close source"`

---

### Task 8: Book C, ORB bull-put spread

**Files:**
- Create: `agentdesk/books/orb_bull_put.py`
- Test: `tests/test_book_c.py`

**Interfaces:**
- Consumes: Task 1 base; `EMA`, `RSI`, `MACD` (`agentdesk/indicators.py`); `Bar`, `TimeBarBuilder` (`agentdesk/bars.py`); `is_rth`.
- Produces: `OrbBullPut(cfg)` (`name = "ORB BULL PUT"`), attributes `vols`, `orh`, `last_exit`; config key `reset_volume_on_live` (set by `build()` in Task 9 when the feed is Alpaca IEX).

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from books_fakes import DAY, ct_ts
from agentdesk.bars import Bar
from agentdesk.books.base import MarketContext
from agentdesk.books.orb_bull_put import OrbBullPut
from agentdesk.config import load_config

C = dict(load_config()["books"]["C_orb_bull_put"])


def bar5(h, m, o, c, v=1000, hi=None, lo=None):
    t = ct_ts(h, m)
    return Bar("5m", t, o, hi if hi is not None else max(o, c), lo if lo is not None else min(o, c), c, v, 10, t + 300)


def ctx(now, vwap=760.0, open_=()):
    return MarketContext(now=now, day=DAY, spot=None, vwap=vwap, open=list(open_))


def primed(rsi_path=True):
    """Strategy warmed so EMA20/RSI/volume median are ready: 40 rising-but-choppy prior bars, then today's
    opening range 08:30-09:00 CT with a high of 764.00."""
    s = OrbBullPut(C)
    px = 750.0
    for i in range(40):                                   # yesterday's afternoon, 5m bars
        t = ct_ts(10, 0) - 86400 + i * 300
        step = 0.35 if i % 3 else -0.25
        s._update(Bar("5m", t, px, px + 0.4, px - 0.4, px + step, 1000, 10, t + 300))
        px += step
    for i, c in enumerate([761.0, 762.0, 763.5, 763.0, 763.8, 763.6]):
        s.on_bar("5m", bar5(8, 30 + 5 * i, c - 0.3, c, hi=764.0 if i == 4 else c + 0.1), ctx(ct_ts(8, 35 + 5 * i)))
    return s


def test_fresh_breakout_enters_short_put_below_spot():
    s = primed()
    assert s.orh[DAY] == pytest.approx(764.0)
    it = s.on_bar("5m", bar5(9, 0, 763.7, 764.4, v=1200), ctx(ct_ts(9, 5)))
    assert [(l.right, l.strike, l.side) for l in it.legs] == [("put", 764, "sell"), ("put", 762, "buy")]
    assert it.credit and it.width == 2 and it.budget == 400 and it.meta["und"] == pytest.approx(764.4)


def test_close_above_orh_without_fresh_cross_does_not_fire():
    s = primed()
    s.on_bar("5m", bar5(9, 0, 763.9, 764.3, v=1200), ctx(ct_ts(9, 5), vwap=770.0))    # below VWAP: no entry
    assert s.on_bar("5m", bar5(9, 5, 764.3, 764.6, v=1200), ctx(ct_ts(9, 10))) is None  # prior close already above


def test_each_condition_blocks():
    for kw, why in ((dict(vwap=765.0), "vwap"),):
        s = primed()
        assert s.on_bar("5m", bar5(9, 0, 763.7, 764.4, v=1200), ctx(ct_ts(9, 5), **kw)) is None, why
    s = primed()
    assert s.on_bar("5m", bar5(9, 0, 764.6, 764.4, v=1200), ctx(ct_ts(9, 5))) is None      # red candle
    s = primed()
    assert s.on_bar("5m", bar5(9, 0, 763.7, 764.4, v=500), ctx(ct_ts(9, 5))) is None       # volume < 0.8 x median


def test_outside_window_and_duplicate_suppression():
    s = primed()
    assert s.on_bar("5m", bar5(8, 55, 763.7, 764.4, v=1200), ctx(ct_ts(9, 0))) is None     # inside the opening range
    s = primed()
    open_pos = type("P", (), {"opened_ts": ct_ts(9, 5), "meta": {"und": 764.4}})()
    assert s.on_bar("5m", bar5(9, 0, 763.7, 764.4, v=1200), ctx(ct_ts(9, 5), open_=[open_pos])) is None


def test_no_signal_from_the_bar_the_last_trade_exited_in():
    s = primed()
    s.on_closed(None, ct_ts(9, 2))
    assert s.on_bar("5m", bar5(9, 0, 763.7, 764.4, v=1200), ctx(ct_ts(9, 5))) is None


def test_underlying_exits_first_wins():
    s = OrbBullPut(C)
    pos = type("P", (), {"opened_ts": ct_ts(9, 5), "meta": {"und": 764.4}})()
    c = lambda now, spot: MarketContext(now=now, day=DAY, spot=spot, vwap=None)
    assert s.on_quote(pos, None, ct_ts(9, 10), c(ct_ts(9, 10), 764.4 * (1 - 0.0018))).urgent
    assert "target" in s.on_quote(pos, None, ct_ts(9, 10), c(ct_ts(9, 10), 764.4 * 1.0045)).reason
    assert "45 min" in s.on_quote(pos, None, ct_ts(9, 50), c(ct_ts(9, 50), 764.5)).reason
    assert s.on_quote(pos, None, ct_ts(9, 49), c(ct_ts(9, 49), 764.5)) is None
    late = type("P", (), {"opened_ts": ct_ts(13, 50), "meta": {"und": 764.4}})()
    assert "14:15" in s.on_quote(late, None, ct_ts(14, 15), c(ct_ts(14, 15), 764.5)).reason


def test_macd_cross_down_exits_on_a_later_bar():
    s = primed()
    pos = type("P", (), {"opened_ts": ct_ts(9, 5), "meta": {"und": 764.4}})()
    out = None
    px = 764.4
    for i in range(12):
        px -= 0.6
        out = s.on_bar("5m", bar5(9, 5 + 5 * i, px + 0.6, px, v=1200), ctx(ct_ts(9, 10 + 5 * i), open_=[pos]))
        if out:
            break
    assert out is not None and "MACD" in out.reason


def test_iex_volume_baseline_rebuilds_from_live_bars():             # review focus 5
    s = OrbBullPut({**C, "reset_volume_on_live": True})
    t = ct_ts(10, 0) - 86400
    for i in range(25):
        s._update(Bar("5m", t + i * 300, 750, 750.2, 749.8, 750.1, 20000, 10, t + i * 300 + 300))
    assert len(s.vols) == 20
    s.on_bar("5m", bar5(8, 30, 760.0, 760.2, v=900), ctx(ct_ts(8, 35)))
    assert list(s.vols) == [900]
```

- [ ] **Step 2: Run to verify failure** — FAIL (missing module).

- [ ] **Step 3: Implement `agentdesk/books/orb_bull_put.py`**

```python
"""Book C, ChatGPT's bullish 30-min ORB -> $2 bull-put credit spread (HANDOFF 7C, frozen; expected to lose).

On each 5m close from 09:00 to 13:30 CT: close above the 08:30-09:00 high with the prior close at or below it,
above VWAP, EMA20 above EMA20 three bars ago, RSI(14) 55-72, green candle, volume >= 0.8x the median of the prior
20 bars. Short put ~1 strike below spot, long put 2 lower; size floor(budget / max loss). Exits on SPY, first wins:
-0.18%, +0.45%, 5m MACD cross below signal, 45 minutes, 14:15 CT. At most 2 trades a day, one at a time.
"""
from __future__ import annotations

import math
import statistics
from collections import deque

from ..bars import TimeBarBuilder
from ..clock import ct_time, is_rth, session_date
from ..config import hhmm
from ..indicators import EMA, MACD, RSI
from .base import ExitIntent, Leg, OrderIntent, Strategy, mins

OPEN_MIN = 8 * 60 + 30


class OrbBullPut(Strategy):
    name = "ORB BULL PUT"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.ema, self.rsi, self.macd = EMA(cfg.get("ema", 20)), RSI(14), MACD(12, 26, 9)
        self.emas: deque = deque(maxlen=4)
        self.vols: deque = deque(maxlen=cfg.get("vol_lookback", 20))
        self.prev_close = None
        self.prev_macd = None
        self.orh: dict = {}
        self.last_exit = 0.0
        self._live = False

    def warmup(self, bars_1m: list) -> None:
        b5 = TimeBarBuilder("5m")
        for b in bars_1m:
            if b.tf == "1m" and is_rth(b.t):
                c = b5.on_bar(b)
                if c:
                    self._update(c)

    def _update(self, bar) -> dict:
        """Commit a closed 5m bar (the backtest's indicator series is continuous across days)."""
        m = mins(ct_time(bar.t)) - OPEN_MIN
        if 0 <= m < self.c.get("or_minutes", 30):
            d = session_date(bar.t)
            self.orh[d] = max(self.orh.get(d, bar.h), bar.h)
        e, r, mv = self.ema.update(bar.c), self.rsi.update(bar.c), self.macd.update(bar.c)
        if e is not None:
            self.emas.append(e)
        med = statistics.median(self.vols) if len(self.vols) == self.vols.maxlen else None
        self.vols.append(bar.v)
        prev, self.prev_close = self.prev_close, bar.c
        pm, self.prev_macd = self.prev_macd, mv
        down = bool(mv and pm and mv.macd < mv.signal and pm.macd >= pm.signal)
        return {"prev": prev, "ema_up": len(self.emas) == 4 and self.emas[-1] > self.emas[0], "rsi": r, "med": med,
                "macd_down": down}

    def on_bar(self, tf, bar, ctx):
        if tf != "5m":
            return None
        if not self._live:
            self._live = True
            if self.c.get("reset_volume_on_live"):
                self.vols.clear()           # history volume is SIP-scale; live IEX bars carry ~5% of it
        s = self._update(bar)
        if ctx.open:
            pos = ctx.open[0]
            if s["macd_down"] and bar.end > pos.opened_ts + 1:
                return ExitIntent("5m MACD crossed below signal")
            return None
        if bar.t < self.last_exit:
            return None
        w = self.c.get("window_ct", {"start": "09:00", "end": "13:30"})
        if mins(ct_time(bar.t)) < mins(hhmm(w["start"])) or mins(ct_time(bar.end)) > mins(hhmm(w["end"])):
            return None
        orh = self.orh.get(ctx.day)
        lo, hi = self.c.get("rsi", [55, 72])
        ok = (orh is not None and s["prev"] is not None and bar.c > orh and s["prev"] <= orh
              and ctx.vwap is not None and bar.c > ctx.vwap and s["ema_up"]
              and s["rsi"] is not None and lo <= s["rsi"] <= hi and bar.c > bar.o
              and s["med"] is not None and bar.v >= self.c.get("vol_mult", 0.8) * s["med"])
        if not ok:
            return None
        e = bar.c
        kp = float(math.floor(e) if e != math.floor(e) else e - 1)
        w2 = float(self.c["width"])
        return OrderIntent([Leg("put", kp, "sell"), Leg("put", kp - w2, "buy")], credit=True, width=w2,
                           reason=f"ORB breakout {e:.2f} > {orh:.2f}: short {kp:g}P / long {kp - w2:g}P",
                           budget=float(self.c["max_loss_budget"]),
                           meta={"und": e, "notes": [f"RSI {s['rsi']:.0f}", f"vol {bar.v:.0f} vs median {s['med']:.0f}"]})

    def on_quote(self, pos, cq, now, ctx):
        e = pos.meta["und"]
        px = ctx.spot
        sp, tp = self.c.get("stop_pct", 0.0018), self.c.get("target_pct", 0.0045)
        if px is not None:
            if px <= e * (1 - sp):
                return ExitIntent(f"SPY stop -{sp:.2%}", urgent=True)
            if px >= e * (1 + tp):
                return ExitIntent(f"SPY target +{tp:.2%}")
        hold = self.c.get("max_hold_min", 45)
        if now - pos.opened_ts >= hold * 60:
            return ExitIntent(f"{hold} min time exit")
        if ct_time(now) >= hhmm(self.c.get("close_ct", "14:15")):
            return ExitIntent(f"close {self.c.get('close_ct', '14:15')} CT")
        return None

    def on_closed(self, pos, now) -> None:
        self.last_exit = now

    def plan(self, pos) -> str:
        e = pos.meta["und"]
        return (f"SPY stop {e * (1 - self.c.get('stop_pct', 0.0018)):.2f}, target {e * (1 + self.c.get('target_pct', 0.0045)):.2f}, "
                f"5m MACD cross, {self.c.get('max_hold_min', 45)} min, {self.c.get('close_ct', '14:15')} CT")
```

Note: if `test_fresh_breakout...` fails because the priming path leaves RSI outside 55–72 or EMA not rising, adjust only the priming bars in the test (the rules are fixed); keep the assertions.

- [ ] **Step 4: Run to verify pass** — `python -m pytest -q tests/test_book_c.py && python -m pytest -q tests`.
- [ ] **Step 5: Commit** — `git commit -m "book C: ORB bull-put spread"`

---

### Task 9: Engine wiring, paper_only enforcement, live-mode rule, crew and proposals

**Files:**
- Modify: `agentdesk/engine.py` (hooks only), `agentdesk/__main__.py`, `agentdesk/crew.py` (Vol desk text), `agentdesk/server.py` (resume respects book halt: no change needed; verify)
- Test: `tests/test_books_engine.py`

**Interfaces:**
- Consumes: `BookHost`, `build_books` (Task 5); strategies (Tasks 6–8); `SimVix`, `RobinhoodVix` (Task 7).
- Produces: `Engine.books` (`BookHost | None`); `check_live_promotion(cfg, mode)` in `agentdesk/__main__.py`; `snapshot()["books"]`.

- [ ] **Step 1: Write the failing tests**

```python
import asyncio
import copy

import pytest

from books_fakes import FakeFeed, FakeQuotes, ct_ts
from agentdesk.__main__ import check_live_promotion
from agentdesk.books.base import Leg, OrderIntent
from agentdesk.books.book import Book
from agentdesk.books.host import BookHost, build_books
from agentdesk.brokers.paper import PaperBroker
from agentdesk.bus import Bus
from agentdesk.config import load_config
from agentdesk.engine import Engine
from agentdesk.journal import Journal
from agentdesk.proposals import TWEAKS, ProposalBook, validate

from test_books_host import FLY, TIGHT, Scripted

CFG = load_config()


def engine_with_books():
    fq = FakeQuotes(now=ct_ts(8, 45))
    for l, (b, a) in zip(FLY, TIGHT):
        fq.set(l.right, l.strike, b, a)
    feed = FakeFeed()
    e = Engine(copy.deepcopy(CFG), feed, fq, PaperBroker(fq), Bus(), Journal(None), "paper")
    e.price = 765.0
    e.vwap.add(765.0, 1)
    e.books = BookHost(e, e.cfg, books=[Book("B_test", {}, Scripted(intent=OrderIntent(list(FLY), True, 5.0, "t", lots=1)))])
    return e, fq, feed


def step(e, fq, feed, now):
    fq.now = feed.t = now
    asyncio.run(e._on_second(now))


def test_engine_runs_books_each_second_and_snapshot_has_them():
    e, fq, feed = engine_with_books()
    step(e, fq, feed, ct_ts(8, 45))
    assert len(e.books.positions()) == 1
    assert [b["book"] for b in e.snapshot()["books"]["books"]] == ["A", "B"]


def test_kill_flattens_books_too():
    e, fq, feed = engine_with_books()
    step(e, fq, feed, ct_ts(8, 45))

    async def go():
        await e.kill()
    asyncio.run(go())
    assert not e.books.positions() and e.books.account.halted


def test_watchdog_trips_on_a_stale_combo():
    e, fq, feed = engine_with_books()
    step(e, fq, feed, ct_ts(8, 45))
    pos = e.books.positions()[0]

    async def go():
        e._watchdog(pos.last_quote_ts + 11)
    asyncio.run(go())
    assert e.risk.st.halted and "no fresh quote" in e.risk.st.halt_reason and e.books.account.flatten


def test_live_mode_needs_exactly_one_promoted_book():
    check_live_promotion(CFG, "paper")                          # paper: nothing to check
    with pytest.raises(SystemExit, match="paper_only: false"):
        check_live_promotion(CFG, "live")                       # every book is paper_only today
    cfg = copy.deepcopy(CFG)
    cfg["books"]["A_macd_calls"]["paper_only"] = False
    check_live_promotion(cfg, "live")
    cfg["books"]["C_orb_bull_put"]["paper_only"] = False
    with pytest.raises(SystemExit):
        check_live_promotion(cfg, "live")


def test_books_refuse_to_build_without_paper_only():
    cfg = copy.deepcopy(CFG)
    cfg["books"]["D_iron_condor"]["paper_only"] = False
    with pytest.raises(SystemExit, match="D_iron_condor"):
        build_books(cfg)
    assert [b.letter for b in build_books(CFG)] == ["B", "C", "D"]


def test_book_code_has_no_order_placing_path():
    import pathlib
    src = "".join(p.read_text() for p in pathlib.Path("agentdesk/books").glob("*.py"))
    for tool in ("place_option_order", "cancel_option_order", "exercise_option"):
        assert tool not in src


def test_proposals_cannot_touch_books():
    assert not any(k.startswith("books.") for k in TWEAKS)
    assert validate(CFG, "books.C_orb_bull_put.max_loss_budget", 100)[0] is False
    item = ProposalBook(CFG, None).submit("quant", {"scope": "day", "title": "more C", "params": {"books.B_iron_fly.lots": 2}}, 0.0)
    assert item["status"].startswith("rejected")


def test_vol_desk_is_asked_for_vix1d():
    from agentdesk.crew import DESKS
    assert "vix1d_flag" in DESKS["vol"].role
```

- [ ] **Step 2: Run to verify failure** — FAIL (`Engine` has no `books`; `check_live_promotion` missing).

- [ ] **Step 3: Implement the engine hooks** (`agentdesk/engine.py`; nothing else in the file changes)

In `__init__`, after `self._mismatches = 0`:
```python
        self.books = None           # BookHost for paper books B/C/D (books/host.py); None when none is enabled
```
In `run()`, right after the two startup-halt blocks (before `self.set_agent(... "arriving" ...)`):
```python
        if self.books:
            if self.risk.st.halted:
                self.books.halt_all(self.risk.st.halt_reason)
            await self.books.start()
```
In `_on_second`, after the `manage` block and before `self._watchdog(now)`:
```python
        if self.books:
            await self._run(self.books.on_second(now), "books")
```
At the end of `_on_bar` (after the entry evaluation block):
```python
        if self.books and b.tf in ("1m", "5m"):
            await self._run(self.books.on_bar(b), "books")
```
In `_trip`, right after `self.risk.halt(f"SAFETY: {reason}", flatten=True)`:
```python
        if self.books:
            self.books.halt_all(f"SAFETY: {reason}", flatten=True)
```
In `_watchdog`, replace the loop head `for pos, _ in self.open:` and the message with:
```python
        held = [p for p, _ in self.open] + (self.books.positions() if self.books else [])
        for pos in held:
            age = now - pos.last_quote_ts
            if age > stale:
                label = getattr(pos, "label", None) or pos.contract.label
                self._trip(f"no fresh quote for {label} in {age:.0f}s, stop can't be checked", now)
                break
```
(Book A's `Position` has no `label` attribute, so its message is unchanged.)
In `kill()`, before `await self._cancel_all_quietly()`:
```python
        if self.books:
            await self.books.kill(now)
```
In `flatten()`, after the loop:
```python
        if self.books:
            await self.books.flatten(reason, now)
```
In `snapshot()`, add the key:
```python
            "books": self.books.snapshot() if self.books else None,
```

- [ ] **Step 4: Implement `__main__.py` wiring**

Add above `build`:
```python
def check_live_promotion(cfg, mode: str) -> None:
    """Live mode needs exactly one promoted book (paper_only: false). Only book A has a live order path in this
    build, so A is the only book that can be promoted (HANDOFF sections 10.8 and 12)."""
    if mode != "live":
        return
    promoted = [k for k, v in (cfg.get("books") or {}).items()
                if isinstance(v, dict) and v.get("enabled") and v.get("paper_only") is False]
    if promoted != ["A_macd_calls"]:
        raise SystemExit("Refusing live mode: set paper_only: false on exactly one book, and only A_macd_calls has a "
                         f"live order path. Promoted now: {promoted or 'none'}.")
```
First line of `build()`: `check_live_promotion(cfg, mode)`.
In `build()`, where the Alpaca IEX branch sets `tick_bar_effective`, also set the C volume flag:
```python
            if isinstance(cfg.get("books", {}).get("C_orb_bull_put"), dict):
                cfg["books"]["C_orb_bull_put"]["reset_volume_on_live"] = True
```
Before `return engine, bus`:
```python
    from .books.host import BookHost
    from .books.vol import RobinhoodVix, SimVix
    vix = SimVix(feed) if provider == "sim" else (RobinhoodVix(rh) if rh is not None else None)
    host = BookHost(engine, cfg, vix=vix, reviewer=rh if mode in ("shadow", "live") else None)
    engine.books = host if host.enabled else None
```

- [ ] **Step 5: Crew Vol desk** — in `agentdesk/crew.py`, append to the `"vol"` desk's role string: `" Also report vix1d_flag: true when VIX1D is more than 3 points above VIX, false when it isn't; leave it out if you can't find VIX1D (book B skips the day on true)."`

- [ ] **Step 6: Run to verify pass** — `python -m pytest -q tests/test_books_engine.py && python -m pytest -q tests` (the 38 original tests must pass unmodified).
- [ ] **Step 7: Commit** — `git commit -m "engine: run paper books B/C/D; enforce paper_only and the live promotion rule"`

---

### Task 10: Sim day with every book (no cross-contamination)

**Files:**
- Test: `tests/test_books_sim.py`

**Interfaces:**
- Consumes: `build(cfg, mode, speed, seed, sim_day)` from `agentdesk/__main__.py`; everything above.

- [ ] **Step 1: Write the test**

```python
import asyncio
import copy

import pytest

from agentdesk.__main__ import build
from agentdesk.config import load_config

CFG = load_config()
SEEDS = (21, 7)


def run_day(seed, books_on):
    cfg = copy.deepcopy(CFG)
    if not books_on:
        for k in ("B_iron_fly", "C_orb_bull_put", "D_iron_condor"):
            cfg["books"][k]["enabled"] = False
    engine, bus = build(cfg, "sim", 0, seed, "2026-09-28")
    engine.inline = True
    asyncio.run(engine.run())
    return engine


def a_trades(engine):
    return [(p.contract.label, p.setup, p.qty_initial, round(p.entry, 2), p.exit_reason, round(p.realized - p.fees, 2))
            for p in engine.closed]


@pytest.mark.parametrize("seed", SEEDS)
def test_book_a_is_identical_with_books_on_and_off(seed):
    off, on = run_day(seed, False), run_day(seed, True)
    assert a_trades(on) == a_trades(off)
    assert on.risk.st.day_pnl == pytest.approx(off.risk.st.day_pnl)


def test_books_trade_in_sim_and_journal_rows_are_tagged():
    books_traded, total = set(), 0
    for seed in SEEDS:
        e = run_day(seed, True)
        rows = e.journal.trades()
        assert {r["book"] for r in rows} <= {"A", "B", "C", "D"}
        assert sum(1 for r in rows if r["book"] == "A") == len(e.closed)
        combo = [r for r in rows if r["book"] != "A"]
        books_traded |= {r["book"] for r in combo}
        assert sum(b.day_pnl for b in e.books.books) == pytest.approx(e.books.account.realized)
        assert sum(r["pnl"] for r in combo) == pytest.approx(e.books.account.realized)
        assert not e.books.positions()                      # everything flat by the close
        total += len(combo)
    assert total >= 1 and books_traded & {"B", "D"}
```

- [ ] **Step 2: Run** — `python -m pytest -q tests/test_books_sim.py`. It exercises code from Tasks 1–9, so it should pass; if the second test finds no B/D trade on these seeds, print each book's `skips` for the seeds and pick seeds where the sim gives B or D a tradeable day (the sim's event days skip B by rule). Don't change strategy rules to make it trade. If a full sim day is slower than about 20 s, keep two seeds only.
- [ ] **Step 3: Commit** — `git commit -m "test: sim day with all books, book A unchanged"`

---

### Task 11: Dashboard (book switcher, per-book P&L, combo card, book labels) and demo

**Files:**
- Modify: `agentdesk/web/index.html`, `agentdesk/web/app.js`, `agentdesk/web/styles.css`
- Rebuild: the demo via `python -m agentdesk record-demo --seed 21 --out demo.jsonl` then `python tools/build_demo.py demo.jsonl <out>`

**Interfaces:**
- Consumes: bus events `books`, `book_position`, `book_order`, `book_closed`, `book_skip`; `snapshot.books` (Task 5/9).

- [ ] **Step 1: index.html** — in the header after `<div class="stats" id="stats">…</div>` add `<div class="books" id="books" role="tablist" aria-label="Books"></div>`. In the trades table header add a first column `<th>Book</th>`.

- [ ] **Step 2: app.js state and events**
  - Add to `S`: `books: [], account: null, combos: new Map(), bookClosed: [], activeBook: 'ALL'`.
  - `loadSnapshot`: `const bk = sn.books; S.books = bk ? bk.books.map(({open, closed, ...x}) => x) : []; S.account = bk?.account || null; S.combos = new Map((bk?.books || []).flatMap((b) => b.open || []).map((p) => [p.id, p])); S.bookClosed = (bk?.books || []).flatMap((b) => (b.closed || []).map((p) => ({ ...p, net: p.total_pnl })));` and add combo marks from their fills (below).
  - `apply()` new cases:
    ```js
    case 'books': S.books = e.books; S.account = e.account; mark('books', 'header'); break;
    case 'book_position':
      S.combos.set(e.pos.id, e.pos);
      if (e.event === 'open') { addComboMark(e.pos, e.ts, true); if (!S.bulk) drawMarkers(); feedPush(e.ts, 'buy', `Book ${e.pos.book} opened ${e.pos.qty}× ${e.pos.contract} for ${px(e.pos.entry)} credit · ${e.size_note || ''}`); }
      mark('pos', 'office', 'header'); break;
    case 'book_closed':
      S.combos.delete(e.pos.id); S.bookClosed.push({ ...e.pos, net: e.net }); addComboMark(e.pos, e.ts, false); if (!S.bulk) drawMarkers();
      feedPush(e.ts, 'sell', `Book ${e.pos.book} closed ${e.pos.contract} · ${e.pos.exit_reason} · ${money(e.net)}`); mark('pos', 'trades', 'header'); break;
    case 'book_order': feedPush(e.ts, e.action === 'open' ? 'buy' : 'sell', `Book ${e.book} ${e.action.toUpperCase()} ${e.qty}× lim ${px(e.limit)} (mid ${px(e.mid)}, natural ${px(e.natural)}) → ${e.status}${e.filled ? ` @ ${px(e.price)}` : ''}${e.review ? ' (reviewed by Robinhood)' : ''}`); break;
    case 'book_skip': feedPush(e.ts, 'skip', `Book ${e.book} passed: ${e.why}`); break;
    ```
  - `addComboMark(p, ts, open)`: `S.marks.push({ ts, buy: !open, text: `${p.book} ${open ? 'open' : 'close'}`, pnl: open ? 0 : (p.total_pnl || 0) })` (a credit open is a sell). Prefix book A's marks: in `addMark` change `text` to start with `'A·'`.

- [ ] **Step 3: app.js renderers**
  - `renderBooks()` (called from `render` when `all || dirty.has('books') || dirty.has('header')`): renders `ALL` plus one chip per `S.books` entry: letter, day P&L (`day_pnl + open_pnl`), `trades/max_trades`, a "halted" dot. Chips are `<button role="tab" aria-selected>`; click sets `S.activeBook` and marks `all`.
  - `renderHeader`: when `S.books.length`, Day P&L = sum over the active selection of `day_pnl + open_pnl` (ALL = every book); W/L likewise.
  - `renderPosition`: if `S.activeBook` is `B`, `C` or `D` → `renderCombo()`; if `ALL` and book A is flat but a combo is open → `renderCombo()` for the first open combo; otherwise the existing A card, and in `ALL` append one `<p class="note">` per open combo: `Book B · IRON FLY · credit 3.13 · mark 2.40 · +$73`.
  - `renderCombo(p)` (or a flat state with the book's `last_skip`): title `${p.qty}× ${p.setup}` + P&L %; a legs table (side, strike, right) ; `kv` rows Credit (`entry`), Mark (debit to close), Take profit (`target`), Stop (`stop`), Max loss, P&L; `p.meta.plan` as a note. For C show "exits on SPY" plan text instead of stop/target (they are 0).
  - `renderTrades`: rows = `[...S.closed.map((p) => ({...p, book: 'A'})), ...S.bookClosed]` filtered by `S.activeBook` (ALL shows all), sorted by `closed_ts` descending; first cell is the book letter.
  - `renderOffice`: the monitored position is the active book's first open position (A's from `S.positions`, others from `S.combos`), label `${p.book || 'A'} ${p.qty}X ${p.setup}`.

- [ ] **Step 4: styles.css** — `.books{display:flex;gap:6px;flex-wrap:wrap}` `.books button{…}` using the existing pill/button tokens; `[aria-selected=true]` uses the accent; the strip wraps under the stats at phone width.

- [ ] **Step 5: Verify in a browser** — run `python -m agentdesk run --mode sim --speed 600 --no-browser` in the background, open `http://127.0.0.1:8765` with Playwright (Chromium at `/opt/pw-browsers`), wait until ~10:00 sim time, screenshot the ALL view and each of B/C/D, and check the console has no errors. Then rebuild the demo: `python -m agentdesk record-demo --seed 21 --out demo.jsonl && python tools/build_demo.py demo.jsonl /tmp/…/demo.html`, load it the same way and screenshot.
- [ ] **Step 6: Commit** — `git commit -m "dashboard: book switcher, per-book P&L, combo card, book labels"`

---

### Task 12: Docs and final verification

**Files:**
- Modify: `CLAUDE.md`, `README.md` (books section), `config.yaml` comments
- Do not modify: `HANDOFF.md` (source of truth; changes need Evan)

- [ ] **Step 1: Update docs** — CLAUDE.md: "What exists" says B, C, D are built in `agentdesk/books/` (paper only), D's quiet filter uses 08:30–09:00 CT data (with the re-run numbers and `research/d_quiet_check.py`), `stop_debit_x_credit`, the $10k paper account and caps, live mode needs one promoted book, and the new test count. README: a short "Paper books" section with the same facts and how to switch books in the dashboard.
- [ ] **Step 2: Full verification** — `python -m pytest -q tests` (all pass, count noted); `python -m agentdesk record-demo --seed 21 --out /tmp/…/d.jsonl` prints A's P&L identical to a run on `main` for the same seed; `grep -rn "place_option_order\|cancel_option_order\|exercise_option" agentdesk/books` is empty.
- [ ] **Step 3: Final review** — superpowers:requesting-code-review on the whole branch; fix findings.
- [ ] **Step 4: Commit, push, draft PR** — push `claude/multi-book-framework-vaikf6`, open a draft PR against `main` with the attribution block, assign Evan, subscribe to PR activity. If `fix/pin-mcp-v1` has landed on main, merge main into the branch first.
