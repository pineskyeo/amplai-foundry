"""Instalment plans: an order's total paid in monthly shares."""

from __future__ import annotations

from datetime import date

from .dates import add_months
from .errors import ParseError
from .models import Order
from .money import split_evenly
from .pricing import order_total

MAX_INSTALMENTS = 12


def plan_instalments(
    order: Order, count: int, first_due: date, *, tax_bp: int = 0
) -> list[tuple[date, int]]:
    """``(due date, cents)`` per instalment: ``split_evenly`` shares, due monthly from
    ``first_due``."""
    if not 1 <= count <= MAX_INSTALMENTS:
        raise ValueError("count must be between 1 and 12")
    if tax_bp < 0:
        raise ValueError("tax_bp must not be negative")
    if order.status == "cancelled":
        raise ParseError(f"order {order.order_id} is cancelled")
    shares = split_evenly(order_total(order, tax_bp=tax_bp), count)
    return [(add_months(first_due, i), share) for i, share in enumerate(shares)]
