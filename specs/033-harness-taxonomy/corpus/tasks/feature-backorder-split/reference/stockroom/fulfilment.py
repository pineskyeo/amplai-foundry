"""Fulfilment: splitting an order into what stock covers now and a backorder."""

from __future__ import annotations

from .inventory import Inventory
from .models import Order, OrderLine


def split_order(order: Order, inventory: Inventory) -> tuple[Order | None, Order | None]:
    """``(ready, backorder)``: what the available stock covers now and what is left over.

    Lines are served in order and share the available stock of their SKU. ``inventory`` is not
    changed. When everything is covered the result is ``(order, None)``; when nothing is covered
    and there are lines it is ``(None, order)``; otherwise two new orders ``<id>-1`` and ``<id>-2``.
    """
    remaining: dict[str, int] = {}
    ready_lines: list[OrderLine] = []
    back_lines: list[OrderLine] = []
    for line in order.lines:
        left = remaining.setdefault(line.sku, inventory.available(line.sku))
        take = min(line.quantity, left)
        remaining[line.sku] = left - take
        if take > 0:
            ready_lines.append(OrderLine(line.sku, take, line.unit_price_cents))
        if line.quantity > take:
            back_lines.append(OrderLine(line.sku, line.quantity - take, line.unit_price_cents))
    if not back_lines:
        return order, None
    if not ready_lines:
        return None, order

    def part(suffix: str, lines: list[OrderLine]) -> Order:
        return Order(
            f"{order.order_id}-{suffix}", order.customer, order.placed, tuple(lines), order.status
        )

    return part("1", ready_lines), part("2", back_lines)
