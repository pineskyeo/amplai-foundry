"""Invoices in another currency: exchange rates as whole millionths."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ParseError
from .models import Order
from .pricing import line_total

_RATE = re.compile(r"[0-9]+(\.[0-9]{1,6})?")
MICRO = 1_000_000


def parse_rate(text: str) -> int:
    """``"0.9215"`` -> 921500: the rate in millionths; zero and malformed rates are refused."""
    cleaned = text.strip()
    if not _RATE.fullmatch(cleaned):
        raise ParseError(f"not a rate: {text!r}")
    whole, _, frac = cleaned.partition(".")
    rate = int(whole) * MICRO + (int(frac.ljust(6, "0")) if frac else 0)
    if rate == 0:
        raise ParseError(f"not a rate: {text!r}")
    return rate


def convert(cents: int, rate: int) -> int:
    """``cents`` at ``rate`` millionths, rounded half up to a whole cent."""
    if cents < 0:
        raise ValueError("cents must not be negative")
    if rate <= 0:
        raise ValueError("rate must be positive")
    return (cents * rate + MICRO // 2) // MICRO


@dataclass(frozen=True)
class Invoice:
    order_id: str
    customer: str
    lines: tuple[tuple[str, int, int], ...]
    total: int


def make_invoice(order: Order, rate: int) -> Invoice:
    if order.status == "cancelled":
        raise ParseError(f"order {order.order_id} is cancelled")
    lines = tuple(
        (line.sku, line.quantity, convert(line_total(line), rate)) for line in order.lines
    )
    return Invoice(order.order_id, order.customer, lines, sum(amount for _, _, amount in lines))
