from __future__ import annotations

from dataclasses import dataclass, field


class OrderStateError(Exception):
    """The broker may hold a live order or fill the engine can't confirm. The engine halts and flattens."""

    def __init__(self, msg: str, order_id: str | None = None, filled_qty: int = 0, avg_price: float = 0.0):
        super().__init__(msg)
        self.order_id, self.filled_qty, self.avg_price = order_id, filled_qty, avg_price


@dataclass
class OrderResult:
    status: str                 # filled | partial | unfilled | rejected
    filled_qty: int = 0
    avg_price: float = 0.0
    order_id: str | None = None
    message: str = ""
    review: dict | None = None  # Robinhood review_option_order response (shadow/live)
    raw: dict = field(default_factory=dict)


class Broker:
    name = "broker"
    live = False

    async def start(self) -> None: ...

    async def resolve(self, contract):
        return contract

    async def submit(self, contract, side: str, qty: int, limit: float, now: float) -> OrderResult:
        raise NotImplementedError

    async def cancel_all(self) -> None: ...

    async def buying_power(self) -> float | None:
        return None

    async def open_positions(self) -> list:
        return []

    async def position_qty(self) -> int | None:
        """Total option contracts held at the broker, or None when the broker holds nothing real (paper)."""
        return None
