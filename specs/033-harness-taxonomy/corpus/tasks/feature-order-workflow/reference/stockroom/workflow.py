"""The order status workflow and its effect on stock."""

from __future__ import annotations

from dataclasses import replace

from .errors import ParseError, TransitionError
from .inventory import Inventory
from .models import STATUSES, Order
from .orders import OrderBook

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "open": ("paid", "cancelled"),
    "paid": ("shipped", "cancelled"),
    "shipped": (),
    "cancelled": (),
}


def can_transition(old: str, new: str) -> bool:
    """Whether an order may go from status ``old`` to status ``new``."""
    return new in TRANSITIONS.get(old, ())


def advance(order: Order, new_status: str) -> Order:
    """A copy of ``order`` with ``new_status``; the original is untouched."""
    if new_status not in STATUSES:
        raise ParseError(f"unknown status {new_status!r}")
    if not can_transition(order.status, new_status):
        raise TransitionError(f"{order.order_id}: cannot go from {order.status} to {new_status}")
    return replace(order, status=new_status)


def _stock_effect(inventory: Inventory, order: Order, new_status: str) -> None:
    if new_status == "cancelled":
        for line in order.lines:
            inventory.release(line.sku, line.quantity)
    elif new_status == "shipped":
        for line in order.lines:
            inventory.ship(line.sku, line.quantity)


def apply_transition(
    book: OrderBook, order_id: str, new_status: str, inventory: Inventory | None = None
) -> Order:
    """Move an order of ``book`` to ``new_status`` and return the updated order.

    With an ``inventory``, cancelling releases and shipping ships every line's quantity. All or
    nothing: when the stock change fails, ``book`` and ``inventory`` are left as they were."""
    order = book.get(order_id)
    updated = advance(order, new_status)
    if inventory is not None:
        _stock_effect(inventory.copy(), order, new_status)
        _stock_effect(inventory, order, new_status)
    book.replace(updated)
    return updated
