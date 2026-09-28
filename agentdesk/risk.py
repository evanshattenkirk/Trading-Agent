"""Risk manager: sizing, daily limits, cooldowns, blackouts, kill switch.

The crew (LLM desks) can only *restrict* through this module: a size multiplier
clamped to [min_size_multiplier, 1.0], extra blackout windows, and cooldowns.
Nothing the crew says can raise size, loosen a limit, or place an order.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .clock import ct_time, hm, session_date
from .config import hhmm


@dataclass
class Blackout:
    start: float
    end: float
    name: str
    flatten_at: float | None = None


@dataclass
class RiskState:
    day_pnl: float = 0.0
    peak_day_pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    loss_streak: int = 0
    cooldown_until: float = 0.0
    halted: bool = False
    halt_reason: str | None = None
    flatten_all: bool = False       # set by the kill switch and the safety watchdog
    paused: bool = False
    size_mult: float = 1.0
    blackouts: list[Blackout] = field(default_factory=list)


class RiskManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.r = cfg["risk"]
        self.s = cfg["sizing"]
        self.st = RiskState()

    def reset_day(self) -> None:
        keep = self.st.blackouts
        self.st = RiskState(blackouts=[b for b in keep])

    # ---- gates --------------------------------------------------------
    def can_enter(self, now: float, open_positions: int) -> tuple[bool, str]:
        st, r = self.st, self.r
        win = dict(self.cfg["strategy"]["entry_window"])
        if self.early_close(now):
            win["end"] = min(win["end"], "11:20")
        t = ct_time(now)
        if st.halted:
            return False, f"halted: {st.halt_reason}"
        if st.paused:
            return False, "paused"
        if not (hhmm(win["start"]) <= t < hhmm(win["end"])):
            return False, f"outside entry window {win['start']}-{win['end']}"
        if open_positions >= r["max_open_positions"]:
            return False, "position already open"
        if st.trades >= r["max_trades_per_day"]:
            return False, f"max {r['max_trades_per_day']} trades hit"
        if st.day_pnl <= -abs(r["max_daily_loss"]):
            self.halt(f"daily loss limit -${r['max_daily_loss']}")
            return False, st.halt_reason
        pl = r.get("profit_lock")
        if pl and st.peak_day_pnl >= pl["trigger"] and st.day_pnl <= st.peak_day_pnl * (1 - pl["giveback_pct"]):
            self.halt(f"profit lock: gave back {int(pl['giveback_pct'] * 100)}% of +${st.peak_day_pnl:.0f}")
            return False, st.halt_reason
        if now < st.cooldown_until:
            return False, f"cooldown until {hm(st.cooldown_until)}"
        for b in st.blackouts:
            if b.start <= now < b.end:
                return False, f"blackout: {b.name}"
        return True, "ok"

    def size(self, ask: float, up_mult: float = 1.0) -> tuple[int, str]:
        """Budget = $max_trade x multiplier. Crew cuts (<1) always win; a size-up (>1, max 1.25) only
        applies when no cut is active and also scales the contract cap (5 -> 6)."""
        if ask <= 0:
            return 0, "no ask"
        m = self.st.size_mult if self.st.size_mult < 1.0 else min(max(1.0, up_mult), self.cfg["crew"].get("max_size_multiplier", 1.25))
        budget = self.s["max_trade_dollars"] * m
        cap = round(self.s["max_contracts"] * m) if m > 1.0 else self.s["max_contracts"]
        qty = min(cap, int(budget // (ask * 100)))
        if qty < self.s["min_contracts"]:
            return 0, f"1 contract costs ${ask * 100:.0f} > budget ${budget:.0f}"
        tag = f" SIZE-UP {m:.2f}x" if m > 1.0 else ""
        return qty, f"{qty} x ${ask:.2f} = ${qty * ask * 100:.0f} (budget ${budget:.0f}{tag})"

    def early_close(self, now: float) -> bool:
        return str(session_date(now)) in {str(d) for d in (self.cfg.get("calendar") or {}).get("early_close", [])}

    def must_flatten(self, now: float) -> str | None:
        fl = "11:40" if self.early_close(now) else self.cfg["exits"]["flatten_at"]
        if ct_time(now) >= hhmm(fl):
            return f"flatten {fl} CT"
        for b in self.st.blackouts:
            if b.flatten_at is not None and b.flatten_at <= now < b.end:
                return f"flatten before {b.name}"
        if self.st.halted and self.st.flatten_all:
            return self.st.halt_reason or "kill switch"
        return None

    # ---- bookkeeping --------------------------------------------------
    def on_trade_closed(self, pnl: float, now: float) -> None:
        """Counts and streaks only; dollars already booked via on_realized()."""
        st = self.st
        st.trades += 1
        if pnl >= 0:
            st.wins += 1
            st.loss_streak = 0
        else:
            st.losses += 1
            st.loss_streak += 1
            c = self.r["loss_streak_cooldown"]
            if st.loss_streak >= c["losses"]:
                st.cooldown_until = now + c["minutes"] * 60

    def on_realized(self, pnl: float) -> None:
        """Partial fills (scale-outs) count toward the daily P&L immediately."""
        self.st.day_pnl += pnl
        self.st.peak_day_pnl = max(self.st.peak_day_pnl, self.st.day_pnl)

    def halt(self, reason: str, flatten: bool = False) -> None:
        self.st.halted, self.st.halt_reason = True, reason
        self.st.flatten_all = self.st.flatten_all or flatten

    def add_blackout(self, event_ts: float, name: str) -> None:
        eb = self.r["event_blackout"]
        fl = self.r.get("flatten_before_high_impact_min")
        self.st.blackouts.append(Blackout(
            event_ts - eb["before_min"] * 60, event_ts + eb["after_min"] * 60, name,
            event_ts - fl * 60 if fl is not None else None))

    def set_size_mult(self, m: float) -> None:
        lo = self.cfg["crew"]["min_size_multiplier"]
        self.st.size_mult = max(lo, min(1.0, m))

    def extend_cooldown(self, now: float, minutes: float) -> None:
        self.st.cooldown_until = max(self.st.cooldown_until, now + min(30, minutes) * 60)

    def to_dict(self) -> dict:
        st = self.st
        return {
            "day_pnl": round(st.day_pnl, 2), "peak_day_pnl": round(st.peak_day_pnl, 2), "trades": st.trades,
            "wins": st.wins, "losses": st.losses, "loss_streak": st.loss_streak,
            "cooldown_until": st.cooldown_until or None, "halted": st.halted, "halt_reason": st.halt_reason,
            "paused": st.paused, "size_mult": st.size_mult,
            "max_daily_loss": self.r["max_daily_loss"], "max_trades": self.r["max_trades_per_day"],
            "blackouts": [{"start": b.start, "end": b.end, "name": b.name} for b in st.blackouts],
        }
