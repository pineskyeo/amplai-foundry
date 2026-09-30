"""Stock valuation: what the on-hand stock is worth, per item or per tag."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .inventory import Inventory
from .models import Item

UNTAGGED = "(untagged)"
TOTAL = "TOTAL"


@dataclass(frozen=True)
class ValuationRow:
    key: str
    units: int
    value_cents: int
    share_bp: int


def _share(value: int, total: int) -> int:
    if total == 0:
        return 0
    return (2 * value * 10000 + total) // (2 * total)


def stock_valuation(
    items: Iterable[Item], inventory: Inventory, *, by: str = "tag"
) -> tuple[list[ValuationRow], ValuationRow]:
    """``(rows, total)``: on-hand units and their value (units times catalogue price) per SKU or
    per tag, largest value first (ties by key), and a ``TOTAL`` row counting every item once."""
    if by not in ("sku", "tag"):
        raise ValueError(f"unknown grouping: {by!r}")
    groups: dict[str, list[int]] = {}
    total_units = total_value = 0
    for item in items:
        units = inventory.on_hand(item.sku)
        value = units * item.price_cents
        total_units += units
        total_value += value
        if by == "sku":
            keys = [item.sku]
        else:
            keys = sorted(set(item.tags)) or [UNTAGGED]
        for key in keys:
            entry = groups.setdefault(key, [0, 0])
            entry[0] += units
            entry[1] += value
    rows = [
        ValuationRow(key, units, value, _share(value, total_value))
        for key, (units, value) in groups.items()
    ]
    rows.sort(key=lambda row: (-row.value_cents, row.key))
    total = ValuationRow(TOTAL, total_units, total_value, 10000 if total_value else 0)
    return rows, total
