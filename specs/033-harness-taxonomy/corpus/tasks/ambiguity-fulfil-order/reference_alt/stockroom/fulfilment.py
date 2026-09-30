"""Order fulfilment: ship a paid order's lines out of the stock held for it."""

from __future__ import annotations

from .errors import StockError
from .inventory import Inventory
from .models import Order, OrderLine


def fulfil_order(inventory: Inventory, order: Order) -> tuple[OrderLine, ...]:
    """Ship the lines of a ``paid`` order from the stock reserved for it.

    Returns the lines that were shipped, in the order they appear in the order. Any other status
    raises ``StockError`` and changes nothing.
    """
    if order.status != "paid":
        raise StockError(f"{order.order_id}: only a paid order can be fulfilled, not {order.status}")
    shipped: list[OrderLine] = []
    for line in order.lines:
        if line.quantity <= inventory.reserved(line.sku):
            inventory.ship(line.sku, line.quantity)
            shipped.append(line)
    return tuple(shipped)
