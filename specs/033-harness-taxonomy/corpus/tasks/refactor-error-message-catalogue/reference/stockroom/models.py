"""The records: catalogue items, order lines and orders."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from . import messages
from .errors import ParseError

SKU = re.compile(r"[A-Z]{2,4}-\d{3,5}")
STATUSES = ("open", "paid", "shipped", "cancelled")


def check_sku(sku: str) -> str:
    """``sku`` when it looks like ``MUG-001`` (2-4 capitals, ``-``, 3-5 digits)."""
    if not SKU.fullmatch(sku):
        raise ParseError(messages.SKU_INVALID.format(sku=sku))
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
            raise ParseError(messages.NAME_EMPTY.format(sku=self.sku))
        if self.price_cents < 0:
            raise ParseError(messages.PRICE_NEGATIVE.format(sku=self.sku))
        if self.reorder_level < 0:
            raise ParseError(messages.REORDER_LEVEL_NEGATIVE.format(sku=self.sku))


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int
    unit_price_cents: int

    def __post_init__(self) -> None:
        check_sku(self.sku)
        if self.quantity <= 0:
            raise ParseError(messages.LINE_QUANTITY_NOT_POSITIVE.format(sku=self.sku))
        if self.unit_price_cents < 0:
            raise ParseError(messages.UNIT_PRICE_NEGATIVE.format(sku=self.sku))


@dataclass(frozen=True)
class Order:
    order_id: str
    customer: str
    placed: date
    lines: tuple[OrderLine, ...] = field(default=())
    status: str = "open"

    def __post_init__(self) -> None:
        if not self.order_id.strip():
            raise ParseError(messages.ORDER_ID_EMPTY)
        if self.status not in STATUSES:
            raise ParseError(
                messages.STATUS_UNKNOWN.format(order_id=self.order_id, status=self.status)
            )

    @property
    def quantity(self) -> int:
        return sum(line.quantity for line in self.lines)
