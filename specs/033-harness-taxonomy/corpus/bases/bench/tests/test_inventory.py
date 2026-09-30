import pytest

from stockroom.errors import StockError
from stockroom.inventory import Inventory
from stockroom.models import Item


def test_receive_reserve_ship() -> None:
    inv = Inventory()
    inv.receive("MUG-001", 10)
    inv.reserve("MUG-001", 4)
    assert (inv.on_hand("MUG-001"), inv.reserved("MUG-001"), inv.available("MUG-001")) == (
        10,
        4,
        6,
    )
    inv.ship("MUG-001", 3)
    assert inv.snapshot() == {"MUG-001": (7, 1)}
    inv.release("MUG-001", 1)
    assert inv.available("MUG-001") == 7


def test_refusals() -> None:
    inv = Inventory()
    inv.receive("MUG-001", 2)
    with pytest.raises(StockError):
        inv.reserve("MUG-001", 3)
    with pytest.raises(StockError):
        inv.ship("MUG-001", 1)
    with pytest.raises(StockError):
        inv.release("MUG-001", 1)
    with pytest.raises(StockError):
        inv.receive("MUG-001", 0)


def test_unknown_sku_has_nothing() -> None:
    assert Inventory().available("TEA-010") == 0


def test_from_levels() -> None:
    inv = Inventory.from_levels([("MUG-001", 5, 2), ("TEA-010", 0, 0)])
    assert inv.snapshot() == {"MUG-001": (5, 2), "TEA-010": (0, 0)}
    with pytest.raises(StockError):
        Inventory.from_levels([("MUG-001", 1, 2)])


def test_low_stock() -> None:
    inv = Inventory.from_levels([("MUG-001", 3, 0), ("TEA-010", 20, 0)])
    items = [Item("MUG-001", "Mug", 100, (), 5), Item("TEA-010", "Tea", 100, (), 5)]
    assert [i.sku for i in inv.low_stock(items)] == ["MUG-001"]
