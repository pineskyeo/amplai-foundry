"""Customer returns of shipped orders: refunds, restocking fees and restocked units."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from .errors import ParseError, StockroomError
from .inventory import Inventory
from .orders import OrderBook

REASONS = ("unwanted", "damaged", "wrong_item")
RESTOCKING_FEE_BP = 1500
_RESTOCKED_REASONS = ("unwanted", "wrong_item")


@dataclass(frozen=True)
class ReturnRequest:
    return_id: str
    order_id: str
    sku: str
    quantity: int
    day: date
    reason: str


@dataclass(frozen=True)
class Refund:
    return_id: str
    order_id: str
    sku: str
    quantity: int
    gross_cents: int
    fee_cents: int
    refund_cents: int
    restocked: int


def restocking_fee(gross_cents: int, rate_bp: int = RESTOCKING_FEE_BP) -> int:
    """``gross_cents * rate_bp / 10000`` rounded half to even, in exact integer arithmetic."""
    if gross_cents < 0 or rate_bp < 0:
        raise ValueError("amount and rate must not be negative")
    quotient, rest = divmod(gross_cents * rate_bp, 10000)
    if rest * 2 > 10000 or (rest * 2 == 10000 and quotient % 2 == 1):
        quotient += 1
    return quotient


class ReturnDesk:
    def __init__(self, book: OrderBook, inventory: Inventory, *, window_days: int = 30) -> None:
        if window_days < 0:
            raise ValueError("window_days must not be negative")
        self._book = book
        self._inventory = inventory
        self._window = timedelta(days=window_days)
        self._accepted: dict[str, tuple[ReturnRequest, Refund]] = {}
        self._from_line: dict[tuple[str, int], int] = {}
        self._refunds: list[Refund] = []

    def process(self, request: ReturnRequest) -> Refund:
        known = self._accepted.get(request.return_id)
        if known is not None:
            if known[0] == request:
                return known[1]
            raise StockroomError(f"return id {request.return_id} was used for another return")
        if request.reason not in REASONS:
            raise ParseError(f"unknown return reason {request.reason!r}")
        if request.quantity < 1:
            raise ParseError("return quantity must be at least 1")
        order = self._book.get(request.order_id)
        if order.status != "shipped":
            raise StockroomError(f"{order.order_id} is not shipped")
        if not order.placed <= request.day <= order.placed + self._window:
            raise StockroomError(f"{request.return_id}: outside the return window")
        lines = [
            (line.unit_price_cents, n, line.quantity)
            for n, line in enumerate(order.lines)
            if line.sku == request.sku
        ]
        if not lines:
            raise StockroomError(f"{order.order_id} has no {request.sku}")
        left = sum(qty - self._from_line.get((order.order_id, n), 0) for _, n, qty in lines)
        if request.quantity > left:
            raise StockroomError(f"{request.return_id}: only {left} {request.sku} can be returned")
        wanted, gross, taken = request.quantity, 0, []
        for price, n, qty in sorted(lines):
            units = min(wanted, qty - self._from_line.get((order.order_id, n), 0))
            if units > 0:
                taken.append((n, units))
                gross += units * price
                wanted -= units
        fee = restocking_fee(gross) if request.reason == "unwanted" else 0
        restocked = request.quantity if request.reason in _RESTOCKED_REASONS else 0
        if restocked:
            self._inventory.receive(request.sku, restocked)
        for n, units in taken:
            key = (order.order_id, n)
            self._from_line[key] = self._from_line.get(key, 0) + units
        refund = Refund(
            request.return_id,
            request.order_id,
            request.sku,
            request.quantity,
            gross,
            fee,
            gross - fee,
            restocked,
        )
        self._accepted[request.return_id] = (request, refund)
        self._refunds.append(refund)
        return refund

    def process_all(self, requests: Iterable[ReturnRequest]) -> list[Refund]:
        saved = (
            dict(self._accepted),
            dict(self._from_line),
            list(self._refunds),
            dict(self._inventory._on_hand),
            dict(self._inventory._reserved),
        )
        try:
            return [self.process(request) for request in requests]
        except Exception:
            self._accepted, self._from_line, self._refunds = saved[0], saved[1], saved[2]
            self._inventory._on_hand, self._inventory._reserved = saved[3], saved[4]
            raise

    def returned(self, order_id: str, sku: str) -> int:
        try:
            order = self._book.get(order_id)
        except StockroomError:
            return 0
        return sum(
            self._from_line.get((order_id, n), 0)
            for n, line in enumerate(order.lines)
            if line.sku == sku
        )

    def refunds(self) -> list[Refund]:
        return list(self._refunds)
