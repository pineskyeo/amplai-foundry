"""Repeat orders: a new open order with the lines of an earlier one."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from .errors import ParseError
from .models import Item, Order, OrderLine


def repeat_order(order: Order, items: Sequence[Item], *, order_id: str, placed: date) -> Order:
    """A new open order for ``order``'s customer with its lines as they were ordered."""
    if order.status == "cancelled":
        raise ParseError(f"order {order.order_id} is cancelled")
    if placed < order.placed:
        raise ParseError(
            f"repeat of {order.order_id} cannot be placed before {order.placed.isoformat()}"
        )
    sold = {item.sku for item in items}
    lines = []
    for line in order.lines:
        if line.sku not in sold:
            raise ParseError(f"{line.sku} is no longer sold")
        lines.append(OrderLine(line.sku, line.quantity, line.unit_price_cents))
    return Order(order_id, order.customer, placed, tuple(lines), "open")
