"""Position state and the combo exit plan.

All thresholds are on option premium (mark = mid), measured from the average entry.

  hard stop        mark <= entry * (1 - stop)                -> sell all
  scale-outs       mark >= entry * (1 + at)                  -> sell fraction of the initial size
  breakeven        after the first scale, stop moves to entry
  runner trail     after the first scale, stop = max(stop, peak * (1 - trail))
  cross-back       exit-TF MACD crosses below signal:
                     before any scale -> sell all (trade failed)
                     after a scale    -> sell the runner, unless "ripping"
  ripping          5m histogram rising and price above VWAP: runner ignores the fast
                   cross-back and waits for a 5m cross-back (still protected by the trail)
  time stop        N minutes in with no scale and gain < min_gain -> sell all
  flatten          at flatten_at, on a high-impact event, or on kill switch
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

_ids = itertools.count(1)


@dataclass
class Contract:
    symbol: str
    expiry: str             # YYYY-MM-DD
    strike: float
    right: str              # "call" | "put"
    broker_id: str | None = None

    @property
    def label(self) -> str:
        return f"{self.symbol} {self.strike:g}{'C' if self.right == 'call' else 'P'} {self.expiry[5:]}"

    @property
    def occ(self) -> str:
        y, m, d = self.expiry.split("-")
        return f"{self.symbol}{y[2:]}{m}{d}{'C' if self.right == 'call' else 'P'}{int(round(self.strike * 1000)):08d}"


@dataclass
class ExitIntent:
    qty: int
    reason: str
    urgent: bool            # urgent = hit the bid now; otherwise work a limit
    scale: bool = False     # a scale-out: counted done only once something sells (engine.exit rolls it back)
    stop_before: float | None = None    # a scale-out's stop before it moved to breakeven, restored by that rollback


@dataclass
class Position:
    contract: Contract
    setup: str
    qty_initial: int
    entry: float
    opened_ts: float
    id: int = field(default_factory=lambda: next(_ids))
    qty: int = 0
    peak: float = 0.0
    stop: float = 0.0
    scales_done: int = 0
    realized: float = 0.0
    fees: float = 0.0
    fills: list = field(default_factory=list)
    mark: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    closed_ts: float | None = None
    ripping: bool = False
    status: str = "open"
    exit_reason: str | None = None
    strike_reason: str = ""
    entry_reasons: list = field(default_factory=list)
    l2: dict | None = None
    last_quote_ts: float = 0.0      # last time manage() saw a usable quote (safety watchdog)
    crew: dict | None = None        # what the crew's vote did to this entry (qty at 1.0x, cutting desks, tweaks)

    def __post_init__(self):
        self.qty = self.qty or self.qty_initial
        self.peak = self.entry
        self.last_quote_ts = self.last_quote_ts or self.opened_ts

    @property
    def pnl_pct(self) -> float:
        return (self.mark / self.entry - 1) if self.entry else 0.0

    @property
    def unrealized(self) -> float:
        return (self.mark - self.entry) * 100 * self.qty

    @property
    def total_pnl(self) -> float:
        return self.realized + self.unrealized - self.fees

    def to_dict(self) -> dict:
        return {
            "id": self.id, "contract": self.contract.label, "occ": self.contract.occ, "setup": self.setup,
            "qty": self.qty, "qty_initial": self.qty_initial, "entry": round(self.entry, 2),
            "mark": round(self.mark, 2), "bid": round(self.bid, 2), "ask": round(self.ask, 2),
            "peak": round(self.peak, 2), "stop": round(self.stop, 2), "pnl_pct": round(self.pnl_pct * 100, 1),
            "realized": round(self.realized, 2), "unrealized": round(self.unrealized, 2),
            "total_pnl": round(self.total_pnl, 2), "scales_done": self.scales_done, "ripping": self.ripping,
            "opened_ts": self.opened_ts, "closed_ts": self.closed_ts, "status": self.status,
            "exit_reason": self.exit_reason, "strike_reason": self.strike_reason, "fills": self.fills,
            "entry_reasons": self.entry_reasons, "l2": self.l2,
        }


class ExitPlan:
    def __init__(self, ecfg: dict, pos: Position):
        self.stop_pct = ecfg["stop_loss_pct"]
        self.p = ecfg["swing" if pos.setup == "SWING" else "scalp"]
        self.pos = pos
        pos.stop = round(pos.entry * (1 - self.stop_pct), 2)

    @property
    def exit_tf(self) -> str:
        return self.p["exit_on_cross_back"]

    def targets(self) -> list[float]:
        return [round(self.pos.entry * (1 + s["at"]), 2) for s in self.p["scale_outs"]]

    def _update_stop(self) -> None:
        pos = self.pos
        first = self.p["scale_outs"][0]["at"]
        if pos.scales_done > 0 or (pos.qty_initial == 1 and pos.peak >= pos.entry * (1 + first)):
            trail = pos.peak * (1 - self.p["runner_trail_pct"])
            if self.p.get("breakeven_after_first_scale", True):
                trail = max(trail, pos.entry)
            pos.stop = round(max(pos.stop, trail), 2)

    def on_quote(self, bid: float, ask: float, now: float) -> ExitIntent | None:
        pos = self.pos
        if pos.qty <= 0:
            return None
        pos.bid, pos.ask = bid, ask
        pos.mark = round((bid + ask) / 2, 3) if ask > 0 else bid
        pos.peak = max(pos.peak, pos.mark)
        self._update_stop()

        if pos.mark <= pos.stop:
            label = f"stop -{self.stop_pct * 100:.0f}%" if pos.stop < pos.entry else ("breakeven stop" if pos.stop == round(pos.entry, 2) else "trailing stop")
            return ExitIntent(pos.qty, label, urgent=True)

        scales = self.p["scale_outs"]
        if pos.scales_done < len(scales) and pos.qty > 1:
            s = scales[pos.scales_done]
            if pos.mark >= pos.entry * (1 + s["at"]):
                n = max(1, int(pos.qty_initial * s["fraction"] + 0.5))    # half rounds up: 50% of 5 sells 3
                n = min(n, pos.qty - 1)          # always keep a runner
                stop_before = pos.stop
                pos.scales_done += 1
                self._update_stop()
                if n > 0:
                    return ExitIntent(n, f"scale +{int(s['at'] * 100)}%", urgent=False, scale=True, stop_before=stop_before)

        mins = (now - pos.opened_ts) / 60
        if pos.scales_done == 0 and mins >= self.p["time_stop_min"] and pos.pnl_pct < self.p["time_stop_min_gain"]:
            return ExitIntent(pos.qty, f"time stop {self.p['time_stop_min']}m", urgent=False)
        return None

    def on_cross_down(self, tf: str, ripping: bool) -> ExitIntent | None:
        pos = self.pos
        if pos.qty <= 0:
            return None
        pos.ripping = bool(ripping and self.p.get("ripping_hold"))
        if pos.scales_done == 0 and tf == self.exit_tf:
            return ExitIntent(pos.qty, f"{tf} MACD cross back", urgent=False)
        if pos.scales_done > 0:
            if tf == self.exit_tf and not pos.ripping:
                return ExitIntent(pos.qty, f"runner out: {tf} cross back", urgent=False)
            if tf == "5m" and pos.ripping:
                return ExitIntent(pos.qty, "runner out: 5m cross back", urgent=False)
        return None
