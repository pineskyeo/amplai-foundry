import random
from datetime import date

import pytest

from stockroom.errors import StockError
from stockroom.fulfilment import fulfil_order
from stockroom.inventory import Inventory
from stockroom.models import Order, OrderLine

SKUS = ("MUG-001", "TEA-010", "PEN-100", "NB-200")


def _order(lines: list[tuple[str, int]], status: str = "paid") -> Order:
    return Order(
        "A-7", "Hana Kim", date(2024, 5, 6), tuple(OrderLine(s, q, 100) for s, q in lines), status
    )


def test_a_paid_order_with_covered_lines_ships_them_all() -> None:
    inventory = Inventory.from_levels([("MUG-001", 10, 4), ("TEA-010", 20, 6), ("PEN-100", 3, 3)])
    order = _order([("MUG-001", 3), ("TEA-010", 6)])
    shipped = fulfil_order(inventory, order)
    assert shipped == order.lines
    assert inventory.snapshot() == {"MUG-001": (7, 1), "TEA-010": (14, 0), "PEN-100": (3, 3)}
    assert order.status == "paid" and len(order.lines) == 2


def test_an_order_that_is_not_paid_raises_and_changes_nothing() -> None:
    for status in ("open", "shipped", "cancelled"):
        inventory = Inventory.from_levels([("MUG-001", 10, 4), ("TEA-010", 20, 6)])
        before = inventory.snapshot()
        with pytest.raises(StockError):
            fulfil_order(inventory, _order([("MUG-001", 1)], status))
        assert inventory.snapshot() == before


def test_stock_stays_consistent_and_only_shipped_lines_leave_it() -> None:
    rng = random.Random(23)
    for _ in range(300):
        levels = []
        for sku in SKUS:
            on_hand = rng.randint(0, 9)
            levels.append((sku, on_hand, rng.randint(0, on_hand)))
        inventory = Inventory.from_levels(levels)
        before = inventory.snapshot()
        picked = rng.sample(SKUS, k=rng.randint(1, 3))
        order = _order([(sku, rng.randint(1, 6)) for sku in picked])
        try:
            shipped = fulfil_order(inventory, order)
        except StockError:
            shipped = ()
        after = inventory.snapshot()
        positions = [order.lines.index(line) for line in shipped]
        assert positions == sorted(positions)
        for sku in SKUS:
            units = sum(line.quantity for line in shipped if line.sku == sku)
            assert after[sku] == (before[sku][0] - units, before[sku][1] - units)
            assert 0 <= after[sku][1] <= after[sku][0]
            assert units <= before[sku][1]


def test_a_shortage_raises_and_the_whole_order_stays_unshipped() -> None:
    inventory = Inventory.from_levels([("MUG-001", 10, 4), ("TEA-010", 20, 2)])
    before = inventory.snapshot()
    with pytest.raises(StockError):
        fulfil_order(inventory, _order([("MUG-001", 3), ("TEA-010", 5)]))
    assert inventory.snapshot() == before
    with pytest.raises(StockError):
        fulfil_order(inventory, _order([("TEA-010", 5), ("MUG-001", 3)]))
    assert inventory.snapshot() == before
