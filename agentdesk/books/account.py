"""Account-level risk across books (HANDOFF sections 9 and 11). Paper notional balance; B/C/D sizing by max
loss; an open-risk cap over every open position (book A's open debit counts); a global halt."""
from __future__ import annotations

ACCOUNT = {"paper_balance": 10000, "open_risk_cap": 2500, "per_position_max_loss": 300, "fee_per_leg": 0.04}


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
