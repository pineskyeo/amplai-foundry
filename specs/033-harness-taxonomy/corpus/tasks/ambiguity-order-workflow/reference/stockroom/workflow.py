"""The order status workflow and what each change does to the stock."""

from __future__ import annotations

from dataclasses import replace

from .errors import ParseError, StockError, StockroomError
from .inventory import Inventory
from .models import STATUSES, Order

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "open": ("paid", "cancelled"),
    "paid": ("shipped", "cancelled"),
    "shipped": (),
    "cancelled": (),
}


def allowed_next(status: str) -> tuple[str, ...]:
    """The statuses an order in ``status`` can change to."""
    if status not in STATUSES:
        raise ParseError(f"unknown status {status!r}")
    return TRANSITIONS[status]


def _totals(order: Order) -> dict[str, int]:
    totals: dict[str, int] = {}
    for line in order.lines:
        totals[line.sku] = totals.get(line.sku, 0) + line.quantity
    return totals


def advance(order: Order, new_status: str, inventory: Inventory | None = None) -> Order:
    """The order with ``new_status``, moving its stock when an ``inventory`` is given."""
    if new_status not in STATUSES:
        raise ParseError(f"{order.order_id}: unknown status {new_status!r}")
    if new_status not in allowed_next(order.status):
        raise StockroomError(
            f"{order.order_id}: cannot change status from {order.status} to {new_status}"
        )
    if inventory is not None:
        totals = _totals(order)
        if (order.status, new_status) == ("paid", "shipped"):
            for sku, quantity in totals.items():
                if quantity > inventory.reserved(sku):
                    raise StockError(f"{sku}: only {inventory.reserved(sku)} reserved")
            for sku, quantity in totals.items():
                inventory.ship(sku, quantity)
        elif (order.status, new_status) == ("paid", "cancelled"):
            for sku, quantity in totals.items():
                held = min(quantity, inventory.reserved(sku))
                if held > 0:
                    inventory.release(sku, held)
    return replace(order, status=new_status)
