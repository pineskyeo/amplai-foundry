"""Restock planning: how much to order, in packs, within a budget."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .errors import ParseError
from .inventory import Inventory
from .models import Item, Order

_PACK = re.compile(r"pack:([0-9]+)")


@dataclass(frozen=True)
class RestockLine:
    sku: str
    quantity: int
    pack: int


def pack_size(item: Item) -> int:
    """The pack size of ``item``: the number of its first ``pack:N`` tag, else 1."""
    size: int | None = None
    for tag in item.tags:
        if not tag.startswith("pack:"):
            continue
        found = _PACK.fullmatch(tag)
        if found is None or int(found.group(1)) < 1:
            raise ParseError(f"{item.sku}: bad pack tag {tag!r}")
        if size is None:
            size = int(found.group(1))
    return size or 1


def plan_restock(
    items: Sequence[Item],
    inventory: Inventory,
    orders: Iterable[Order],
    *,
    budget_cents: int | None = None,
) -> list[RestockLine]:
    """What to order: see the task text."""
    if budget_cents is not None and budget_cents < 0:
        raise ValueError("budget must not be negative")
    sizes = {item.sku: pack_size(item) for item in items}
    demand: dict[str, int] = {}
    for order in orders:
        if order.status in ("open", "paid"):
            for line in order.lines:
                demand[line.sku] = demand.get(line.sku, 0) + line.quantity
    wanted: list[tuple[Item, RestockLine]] = []
    for item in sorted(items, key=lambda i: i.sku):
        if item.reorder_level <= 0:
            continue
        missing = 2 * item.reorder_level + demand.get(item.sku, 0) - inventory.on_hand(item.sku)
        if missing <= 0:
            continue
        size = sizes[item.sku]
        packs = -(-missing // size)
        wanted.append((item, RestockLine(item.sku, packs * size, size)))
    if budget_cents is None:
        return [line for _, line in wanted]
    remaining = budget_cents
    chosen: list[RestockLine] = []
    for item, line in wanted:
        cost = line.quantity * item.price_cents
        if cost <= remaining:
            remaining -= cost
            chosen.append(line)
    return chosen
