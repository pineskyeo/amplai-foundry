import random

import pytest

from stockroom.errors import StockError
from stockroom.inventory import Inventory
from stockroom.models import Item

SKU = "MUG-001"


def test_reserving_more_than_is_available_is_refused_and_changes_nothing() -> None:
    inv = Inventory()
    inv.receive(SKU, 10)
    inv.reserve(SKU, 6)
    with pytest.raises(StockError):
        inv.reserve(SKU, 5)
    assert (inv.on_hand(SKU), inv.reserved(SKU), inv.available(SKU)) == (10, 6, 4)
    assert inv.snapshot() == {SKU: (10, 6)}


def test_repeated_reservations_fill_the_stock_exactly_and_no_further() -> None:
    inv = Inventory()
    inv.receive(SKU, 10)
    for quantity in (3, 3, 4):
        inv.reserve(SKU, quantity)
    assert inv.available(SKU) == 0
    with pytest.raises(StockError):
        inv.reserve(SKU, 1)
    assert inv.reserved(SKU) == 10


def test_levels_loaded_with_reservations_count_against_new_reservations() -> None:
    inv = Inventory.from_levels([(SKU, 10, 8), ("TEA-010", 5, 5)])
    with pytest.raises(StockError):
        inv.reserve(SKU, 3)
    inv.reserve(SKU, 2)
    assert inv.snapshot() == {SKU: (10, 10), "TEA-010": (5, 5)}
    with pytest.raises(StockError):
        inv.reserve("TEA-010", 1)


def test_reserving_after_ship_and_release_uses_the_remaining_available_stock() -> None:
    inv = Inventory()
    inv.receive(SKU, 10)
    inv.reserve(SKU, 6)
    inv.ship(SKU, 2)
    assert (inv.on_hand(SKU), inv.reserved(SKU), inv.available(SKU)) == (8, 4, 4)
    with pytest.raises(StockError):
        inv.reserve(SKU, 5)
    inv.reserve(SKU, 4)
    assert inv.available(SKU) == 0
    inv.release(SKU, 3)
    assert inv.available(SKU) == 3
    with pytest.raises(StockError):
        inv.reserve(SKU, 4)
    inv.reserve(SKU, 3)
    assert inv.snapshot() == {SKU: (8, 8)}
    inv.receive(SKU, 2)
    inv.reserve(SKU, 2)
    assert inv.available(SKU) == 0


def test_unknown_skus_and_non_positive_quantities() -> None:
    inv = Inventory()
    with pytest.raises(StockError):
        inv.reserve("TEA-010", 1)
    assert inv.snapshot() == {}
    inv.receive(SKU, 3)
    for quantity in (0, -1):
        with pytest.raises(StockError):
            inv.reserve(SKU, quantity)
    assert inv.snapshot() == {SKU: (3, 0)}


def test_low_stock_reflects_what_was_reserved() -> None:
    items = [Item(SKU, "Mug", 100, (), 5), Item("TEA-010", "Tea", 100, (), 5)]
    inv = Inventory()
    inv.receive(SKU, 20)
    inv.receive("TEA-010", 20)
    inv.reserve(SKU, 10)
    assert inv.low_stock(items) == []
    inv.reserve(SKU, 5)
    assert inv.low_stock(items) == []
    inv.reserve(SKU, 1)
    assert [i.sku for i in inv.low_stock(items)] == [SKU]
    with pytest.raises(StockError):
        inv.reserve(SKU, 5)
    assert inv.available(SKU) == 4
    assert [i.sku for i in inv.low_stock(items)] == [SKU]


def test_random_operations_match_a_simple_model() -> None:
    rng = random.Random(7)
    inv = Inventory()
    on_hand = {"MUG-001": 0, "TEA-010": 0, "PEN-100": 0}
    reserved = {"MUG-001": 0, "TEA-010": 0, "PEN-100": 0}
    skus = list(on_hand)
    for _ in range(1500):
        sku = rng.choice(skus)
        quantity = rng.randint(1, 6)
        op = rng.choice(["receive", "reserve", "reserve", "release", "ship"])
        if op == "receive":
            inv.receive(sku, quantity)
            on_hand[sku] += quantity
        elif op == "reserve":
            if quantity <= on_hand[sku] - reserved[sku]:
                inv.reserve(sku, quantity)
                reserved[sku] += quantity
            else:
                with pytest.raises(StockError):
                    inv.reserve(sku, quantity)
        elif op == "release":
            if quantity <= reserved[sku]:
                inv.release(sku, quantity)
                reserved[sku] -= quantity
            else:
                with pytest.raises(StockError):
                    inv.release(sku, quantity)
        else:
            if quantity <= reserved[sku]:
                inv.ship(sku, quantity)
                reserved[sku] -= quantity
                on_hand[sku] -= quantity
            else:
                with pytest.raises(StockError):
                    inv.ship(sku, quantity)
        for name in skus:
            assert inv.on_hand(name) == on_hand[name]
            assert inv.reserved(name) == reserved[name]
            assert inv.available(name) == on_hand[name] - reserved[name] >= 0
