"""Cashback: a share of each paid or shipped order goes back to its customer."""

from __future__ import annotations

from .models import Order
from .orders import OrderBook
from .pricing import order_subtotal, tax

EARNING = ("paid", "shipped")


def _check(rate_bp: int) -> None:
    if not 0 <= rate_bp <= 10000:
        raise ValueError("rate_bp must be between 0 and 10000")


def order_cashback(order: Order, rate_bp: int) -> int:
    """``rate_bp`` basis points of a paid or shipped order's subtotal, rounded half up; else 0."""
    _check(rate_bp)
    if order.status not in EARNING:
        return 0
    return tax(order_subtotal(order), rate_bp)


def cashback_by_customer(book: OrderBook, rate_bp: int) -> list[tuple[str, int]]:
    """``(customer, cents)`` for every customer with a paid or shipped order, largest first."""
    _check(rate_bp)
    totals: dict[str, int] = {}
    for order in book:
        if order.status in EARNING:
            totals[order.customer] = totals.get(order.customer, 0) + order_cashback(order, rate_bp)
    return sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))
