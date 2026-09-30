"""Sales analysis: revenue per SKU and ABC classification."""

from __future__ import annotations

from collections.abc import Mapping

from .orders import OrderBook
from .pricing import line_total


def sku_revenue(book: OrderBook) -> dict[str, int]:
    """Cents sold per SKU over the orders that are not cancelled."""
    revenue: dict[str, int] = {}
    for order in book:
        if order.status == "cancelled":
            continue
        for line in order.lines:
            revenue[line.sku] = revenue.get(line.sku, 0) + line_total(line)
    return revenue


def abc_classes(
    revenue: Mapping[str, int], *, a_pct: int = 80, b_pct: int = 95
) -> dict[str, str]:
    """``{sku: "A" | "B" | "C"}`` in ranking order (largest revenue first, ties by SKU).

    A SKU is ``A`` while the revenue ranked before it is below ``a_pct`` % of the total, else ``B``
    while it is below ``b_pct`` %, else ``C``. With a total of 0 every SKU is ``C``.
    """
    if not 0 < a_pct < b_pct <= 100:
        raise ValueError("thresholds must satisfy 0 < a_pct < b_pct <= 100")
    if any(cents < 0 for cents in revenue.values()):
        raise ValueError("revenue must not be negative")
    total = sum(revenue.values())
    classes: dict[str, str] = {}
    before = 0
    for sku, cents in sorted(revenue.items(), key=lambda pair: (-pair[1], pair[0])):
        if total == 0:
            classes[sku] = "C"
        elif before * 100 < a_pct * total:
            classes[sku] = "A"
        elif before * 100 < b_pct * total:
            classes[sku] = "B"
        else:
            classes[sku] = "C"
        before += cents
    return classes
