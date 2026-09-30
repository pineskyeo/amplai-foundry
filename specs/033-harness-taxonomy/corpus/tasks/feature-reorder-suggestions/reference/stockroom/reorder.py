"""Reorder suggestions: how much of each low item to buy."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .inventory import Inventory
from .models import Item


def suggest_orders(
    items: Iterable[Item],
    inventory: Inventory,
    *,
    multiplier: int = 2,
    pack_sizes: Mapping[str, int] | None = None,
    incoming: Mapping[str, int] | None = None,
) -> list[tuple[str, int]]:
    """``(sku, quantity)`` to order for the items that are below their reorder level.

    An item with a reorder level above 0 is below it when its available stock plus the units
    already on order (``incoming``) is under the level. The quantity brings that position up to
    ``reorder_level * multiplier``, rounded up to a whole number of packs.
    """
    if multiplier < 1:
        raise ValueError("multiplier must be at least 1")
    packs = dict(pack_sizes or {})
    arriving = dict(incoming or {})
    if any(size < 1 for size in packs.values()):
        raise ValueError("pack sizes must be at least 1")
    if any(units < 0 for units in arriving.values()):
        raise ValueError("incoming units must not be negative")
    result: list[tuple[str, int]] = []
    for item in items:
        if item.reorder_level <= 0:
            continue
        position = inventory.available(item.sku) + arriving.get(item.sku, 0)
        if position >= item.reorder_level:
            continue
        need = item.reorder_level * multiplier - position
        pack = packs.get(item.sku, 1)
        result.append((item.sku, -(-need // pack) * pack))
    return result
