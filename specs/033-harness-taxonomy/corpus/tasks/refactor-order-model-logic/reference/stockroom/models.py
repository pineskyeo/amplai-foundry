"""The records: catalogue items, order lines and orders."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .errors import ParseError

SKU = re.compile(r"[A-Z]{2,4}-\d{3,5}")
STATUSES = ("open", "paid", "shipped", "cancelled")


def check_sku(sku: str) -> str:
    """``sku`` when it looks like ``MUG-001`` (2-4 capitals, ``-``, 3-5 digits)."""
    if not SKU.fullmatch(sku):
        raise ParseError(f"bad sku: {sku!r}")
    return sku


@dataclass(frozen=True)
class Item:
    sku: str
    name: str
    price_cents: int
    tags: tuple[str, ...] = ()
    reorder_level: int = 0

    def __post_init__(self) -> None:
        check_sku(self.sku)
        if not self.name.strip():
            raise ParseError(f"{self.sku}: empty name")
        if self.price_cents < 0:
            raise ParseError(f"{self.sku}: negative price")
        if self.reorder_level < 0:
            raise ParseError(f"{self.sku}: negative reorder level")


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int
    unit_price_cents: int

    def __post_init__(self) -> None:
        check_sku(self.sku)
        if self.quantity <= 0:
            raise ParseError(f"{self.sku}: quantity must be positive")
        if self.unit_price_cents < 0:
            raise ParseError(f"{self.sku}: negative unit price")

    @property
    def total_cents(self) -> int:
        return self.quantity * self.unit_price_cents


@dataclass(frozen=True)
class Order:
    order_id: str
    customer: str
    placed: date
    lines: tuple[OrderLine, ...] = field(default=())
    status: str = "open"

    def __post_init__(self) -> None:
        if not self.order_id.strip():
            raise ParseError("empty order id")
        if self.status not in STATUSES:
            raise ParseError(f"{self.order_id}: unknown status {self.status!r}")

    @property
    def quantity(self) -> int:
        return sum(line.quantity for line in self.lines)

    @property
    def subtotal_cents(self) -> int:
        return sum(line.total_cents for line in self.lines)

    @property
    def is_active(self) -> bool:
        """False for a cancelled order; cancelled orders never count towards revenue."""
        return self.status != "cancelled"
