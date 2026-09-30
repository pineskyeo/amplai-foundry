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
    needed: dict[str, int] = {}
    for line in order.lines:
        needed[line.sku] = needed.get(line.sku, 0) + line.quantity
    for sku, quantity in needed.items():
        if quantity > inventory.reserved(sku):
            raise StockError(f"{sku}: only {inventory.reserved(sku)} reserved")
    for line in order.lines:
        inventory.ship(line.sku, line.quantity)
    return tuple(order.lines)
