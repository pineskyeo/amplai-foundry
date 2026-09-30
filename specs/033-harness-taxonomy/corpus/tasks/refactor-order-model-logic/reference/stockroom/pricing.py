"""Prices in cents: quantity tiers, percentage discounts, tax and order totals."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .models import Order, OrderLine


@dataclass(frozen=True)
class Tier:
    """From ``min_quantity`` units on, each unit costs ``unit_price_cents``."""

    min_quantity: int
    unit_price_cents: int


def unit_price(base_cents: int, quantity: int, tiers: Iterable[Tier] = ()) -> int:
    """The unit price for ``quantity`` units: the tier with the largest ``min_quantity`` that
    ``quantity`` reaches, else ``base_cents``."""
    price = base_cents
    for tier in sorted(tiers, key=lambda t: t.min_quantity):
        if quantity > tier.min_quantity:
            price = tier.unit_price_cents
    return price


def apply_discount(cents: int, percent: float) -> int:
    """``cents`` less ``percent`` % (0-100); the discount is rounded half up to a whole cent."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return cents - int(cents * percent / 100)


def tax(cents: int, rate_bp: int) -> int:
    """Tax at ``rate_bp`` basis points (825 = 8.25 %), rounded half up to a whole cent."""
    if rate_bp < 0:
        raise ValueError("rate must not be negative")
    return (cents * rate_bp + 5000) // 10000


def line_total(line: OrderLine) -> int:
    return line.total_cents


def order_subtotal(order: Order) -> int:
    return order.subtotal_cents


def order_total(order: Order, *, discount_percent: float = 0, tax_bp: int = 0) -> int:
    """Subtotal, less the discount, plus tax on the discounted amount."""
    discounted = apply_discount(order_subtotal(order), discount_percent)
    return discounted + tax(discounted, tax_bp)
