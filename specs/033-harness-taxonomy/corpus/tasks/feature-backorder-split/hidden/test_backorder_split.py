from __future__ import annotations

from datetime import date

import pytest

from stockroom.errors import StockroomError
from stockroom.fulfilment import split_order
from stockroom.inventory import Inventory
from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook


def order(
    order_id: str,
    *lines: tuple[str, int, int],
    status: str = "open",
    customer: str = "Hana Kim",
    day: int = 1,
) -> Order:
    return Order(
        order_id,
        customer,
        date(2024, 3, day),
        tuple(OrderLine(sku, qty, price) for sku, qty, price in lines),
        status,
    )


def as_tuples(part: Order | None) -> list[tuple[str, int, int]] | None:
    if part is None:
        return None
    return [(line.sku, line.quantity, line.unit_price_cents) for line in part.lines]


def test_partial_split_lines() -> None:
    inv = Inventory.from_levels([("MUG-001", 10, 7), ("TEA-010", 5, 0), ("PEN-100", 0, 0)])
    original = order(
        "A-1",
        ("MUG-001", 5, 1250),
        ("TEA-010", 2, 895),
        ("PEN-100", 1, 3400),
        ("NB-200", 4, 625),
    )
    ready, back = split_order(original, inv)
    assert as_tuples(ready) == [("MUG-001", 3, 1250), ("TEA-010", 2, 895)]
    assert as_tuples(back) == [("MUG-001", 2, 1250), ("PEN-100", 1, 3400), ("NB-200", 4, 625)]
    assert original.quantity == 12  # the original is untouched


def test_lines_of_one_sku_share_stock() -> None:
    inv = Inventory.from_levels([("MUG-001", 4, 0), ("TEA-010", 9, 0)])
    original = order(
        "A-1",
        ("MUG-001", 2, 100),
        ("TEA-010", 1, 200),
        ("MUG-001", 3, 100),
        ("MUG-001", 5, 110),
        ("TEA-010", 8, 200),
    )
    ready, back = split_order(original, inv)
    assert as_tuples(ready) == [
        ("MUG-001", 2, 100),
        ("TEA-010", 1, 200),
        ("MUG-001", 2, 100),
        ("TEA-010", 8, 200),
    ]
    assert as_tuples(back) == [("MUG-001", 1, 100), ("MUG-001", 5, 110)]
    # the first lines are served first: the same lines in another order split differently
    swapped = order("A-2", ("MUG-001", 3, 100), ("MUG-001", 2, 100))
    ready, back = split_order(swapped, inv)
    assert as_tuples(ready) == [("MUG-001", 3, 100), ("MUG-001", 1, 100)]
    assert as_tuples(back) == [("MUG-001", 1, 100)]


def test_inventory_is_not_changed() -> None:
    inv = Inventory.from_levels([("MUG-001", 10, 7), ("TEA-010", 5, 0)])
    split_order(order("A-1", ("MUG-001", 5, 100), ("TEA-010", 9, 100)), inv)
    assert inv.snapshot() == {"MUG-001": (10, 7), "TEA-010": (5, 0)}
    assert inv.available("MUG-001") == 3


def test_fully_covered_returns_same_order() -> None:
    inv = Inventory.from_levels([("MUG-001", 10, 2), ("TEA-010", 5, 0)])
    original = order("A-1", ("MUG-001", 8, 100), ("TEA-010", 5, 100))
    ready, back = split_order(original, inv)
    assert ready is original and back is None
    empty = order("A-2")
    ready, back = split_order(empty, inv)
    assert ready is empty and back is None
    ready, back = split_order(empty, Inventory())
    assert ready is empty and back is None


def test_nothing_covered_returns_same_order() -> None:
    inv = Inventory.from_levels([("MUG-001", 3, 3), ("TEA-010", 0, 0)])
    original = order("A-1", ("MUG-001", 1, 100), ("TEA-010", 2, 100), ("ZZZ-999", 1, 100))
    ready, back = split_order(original, inv)
    assert ready is None and back is original
    assert split_order(original, Inventory()) == (None, original)


def test_part_attributes_and_ids() -> None:
    inv = Inventory.from_levels([("MUG-001", 1, 0)])
    original = order(
        "B-17", ("MUG-001", 3, 1250), customer="Omar Diaz", status="paid", day=9
    )
    ready, back = split_order(original, inv)
    assert ready is not None and back is not None
    assert (ready.order_id, back.order_id) == ("B-17-1", "B-17-2")
    for part in (ready, back):
        assert part.customer == "Omar Diaz" and part.placed == date(2024, 3, 9)
        assert part.status == "paid"
    assert ready.quantity + back.quantity == original.quantity
    assert isinstance(ready.lines, tuple) and isinstance(back.lines, tuple)


def test_book_split_errors() -> None:
    inv = Inventory.from_levels([("MUG-001", 10, 0)])
    book = OrderBook(
        [
            order("A-1", ("MUG-001", 1, 100), status="shipped"),
            order("A-2", ("MUG-001", 1, 100), status="cancelled"),
            order("A-3", ("MUG-001", 50, 100)),
        ]
    )
    with pytest.raises(StockroomError):
        book.split("A-9", inv)
    with pytest.raises(StockroomError):
        book.split("A-1", inv)
    with pytest.raises(StockroomError):
        book.split("A-2", inv)
    assert [o.order_id for o in book] == ["A-1", "A-2", "A-3"]
    assert book.split("A-3", inv)[1] is not None  # an open order with a shortage does split


def test_book_split_without_split_changes_nothing() -> None:
    inv = Inventory.from_levels([("MUG-001", 10, 0)])
    book = OrderBook(
        [
            order("A-1", ("MUG-001", 4, 100)),
            order("A-2", ("TEA-010", 4, 100), status="paid"),
            order("A-3"),
        ]
    )
    before = list(book)
    ready, back = book.split("A-1", inv)
    assert ready is before[0] and back is None
    ready, back = book.split("A-2", inv)
    assert ready is None and back is before[1]
    assert book.split("A-3", inv) == (before[2], None)
    assert list(book) == before and len(book) == 3
    assert all(a is b for a, b in zip(book, before, strict=True))


def test_book_split_replaces_in_place() -> None:
    inv = Inventory.from_levels([("MUG-001", 6, 0)])
    book = OrderBook(
        [
            order("A-1", ("MUG-001", 1, 100), day=5),
            order("A-2", ("MUG-001", 10, 200), status="paid", day=5),
            order("A-3", ("MUG-001", 1, 100), day=5),
        ]
    )
    ready, back = book.split("A-2", inv)
    assert ready is not None and back is not None
    assert [o.order_id for o in book] == ["A-1", "A-2-1", "A-2-2", "A-3"]
    assert book.get("A-2-1") is ready and book.get("A-2-2") is back
    assert (ready.quantity, back.quantity) == (6, 4)
    with pytest.raises(StockroomError):
        book.get("A-2")
    assert len(book) == 4
    assert [o.order_id for o in book.by_customer("Hana Kim")] == ["A-1", "A-2-1", "A-2-2", "A-3"]
    assert book.revenue_by_month() == {"2024-03": 100 + 6 * 200 + 4 * 200 + 100}
    # the inventory was not touched, and a part can be split again
    assert inv.snapshot() == {"MUG-001": (6, 0)}
    again = book.split("A-2-2", Inventory.from_levels([("MUG-001", 1, 0)]))
    assert [o.order_id for o in book] == ["A-1", "A-2-1", "A-2-2-1", "A-2-2-2", "A-3"]
    assert again[0] is not None and again[1] is not None


def test_book_split_id_collision() -> None:
    inv = Inventory.from_levels([("MUG-001", 1, 0)])
    for taken in ("A-1-1", "A-1-2"):
        book = OrderBook([order("A-1", ("MUG-001", 3, 100)), order(taken)])
        before = list(book)
        with pytest.raises(StockroomError):
            book.split("A-1", inv)
        assert all(a is b for a, b in zip(book, before, strict=True)) and len(book) == 2
        assert book.get("A-1") is before[0]
