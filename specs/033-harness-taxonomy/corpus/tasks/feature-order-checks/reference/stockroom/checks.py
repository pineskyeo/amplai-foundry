"""Consistency checks of orders against the catalogue."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .models import Item, Order


@dataclass(frozen=True)
class Issue:
    order_id: str
    line: int  # 0 for a whole-order issue, else the 1-based line number
    code: str


def check_orders(orders: Iterable[Order], items: Iterable[Item]) -> list[Issue]:
    """The problems found in ``orders`` (see the codes below), in a stable order.

    Order level (line 0): ``duplicate_order``, ``blank_customer``, ``no_lines``.
    Line level: ``unknown_sku``, ``price_mismatch``, ``duplicate_sku``.
    """
    catalogue = {item.sku: item for item in items}
    seen_ids: set[str] = set()
    issues: list[Issue] = []
    for order in orders:
        found: list[Issue] = []
        if order.order_id in seen_ids:
            found.append(Issue(order.order_id, 0, "duplicate_order"))
        seen_ids.add(order.order_id)
        if not order.customer.strip():
            found.append(Issue(order.order_id, 0, "blank_customer"))
        if not order.lines:
            found.append(Issue(order.order_id, 0, "no_lines"))
        seen_skus: set[str] = set()
        for number, line in enumerate(order.lines, start=1):
            item = catalogue.get(line.sku)
            if item is None:
                found.append(Issue(order.order_id, number, "unknown_sku"))
            elif order.status != "cancelled" and line.unit_price_cents != item.price_cents:
                found.append(Issue(order.order_id, number, "price_mismatch"))
            if line.sku in seen_skus:
                found.append(Issue(order.order_id, number, "duplicate_sku"))
            seen_skus.add(line.sku)
        issues.extend(sorted(found, key=lambda issue: (issue.line, issue.code)))
    return issues
