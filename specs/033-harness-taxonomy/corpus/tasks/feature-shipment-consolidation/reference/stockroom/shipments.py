"""Shipment consolidation: paid orders of one customer travel together, sharing one shipping fee."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from .errors import StockroomError
from .models import Order
from .money import allocate
from .pricing import order_subtotal


@dataclass(frozen=True)
class Shipment:
    shipment_id: str
    customer: str
    placed: date
    order_ids: tuple[str, ...]
    units: int
    subtotal_cents: int
    fee_cents: int
    fee_shares: tuple[int, ...]


def consolidate(
    orders: Iterable[Order],
    *,
    window_days: int = 3,
    max_units: int = 20,
    fee_cents: int = 600,
    free_from_cents: int = 5000,
) -> list[Shipment]:
    if window_days < 0 or max_units < 1 or fee_cents < 0 or free_from_cents < 0:
        raise ValueError("window_days, fee_cents, free_from_cents >= 0 and max_units >= 1")
    given = list(orders)
    if len({o.order_id for o in given}) != len(given):
        raise StockroomError("duplicate order id")
    paid = sorted(
        (o for o in given if o.status == "paid"), key=lambda o: (o.customer, o.placed, o.order_id)
    )
    groups: list[list[Order]] = []
    for order in paid:
        current = groups[-1] if groups else None
        if (
            current is not None
            and current[0].customer == order.customer
            and (order.placed - current[0].placed).days <= window_days
            and sum(o.quantity for o in current) + order.quantity <= max_units
        ):
            current.append(order)
        else:
            groups.append([order])
    groups.sort(key=lambda g: (g[0].placed, g[0].customer, g[0].order_id))
    shipments = []
    for number, group in enumerate(groups, start=1):
        subtotals = [order_subtotal(o) for o in group]
        subtotal = sum(subtotals)
        fee = 0 if subtotal >= free_from_cents else fee_cents
        shipments.append(
            Shipment(
                f"SHP-{number}",
                group[0].customer,
                group[0].placed,
                tuple(o.order_id for o in group),
                sum(o.quantity for o in group),
                subtotal,
                fee,
                tuple(allocate(fee, subtotals)),
            )
        )
    return shipments
