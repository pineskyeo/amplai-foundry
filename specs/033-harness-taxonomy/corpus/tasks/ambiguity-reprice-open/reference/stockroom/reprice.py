"""Repricing: open orders follow the current catalogue prices."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from .errors import ParseError
from .models import Item, Order, OrderLine


@dataclass(frozen=True)
class Repricing:
    orders: tuple[Order, ...]
    changes: tuple[tuple[str, str, int, int], ...]
    missing: tuple[tuple[str, str], ...]


def reprice_open(orders: Sequence[Order], items: Sequence[Item]) -> Repricing:
    """Open orders' lines at the catalogue price; other orders unchanged."""
    prices: dict[str, int] = {}
    for item in items:
        if item.sku in prices:
            raise ParseError(f"duplicate sku {item.sku} in catalogue")
        prices[item.sku] = item.price_cents
    result: list[Order] = []
    changes: list[tuple[str, str, int, int]] = []
    missing: list[tuple[str, str]] = []
    for order in orders:
        if order.status != "open":
            result.append(order)
            continue
        lines = []
        for line in order.lines:
            if line.sku not in prices:
                missing.append((order.order_id, line.sku))
                lines.append(line)
                continue
            new = prices[line.sku]
            if new != line.unit_price_cents:
                changes.append((order.order_id, line.sku, line.unit_price_cents, new))
            lines.append(OrderLine(line.sku, line.quantity, new))
        result.append(replace(order, lines=tuple(lines)))
    return Repricing(tuple(result), tuple(changes), tuple(missing))
