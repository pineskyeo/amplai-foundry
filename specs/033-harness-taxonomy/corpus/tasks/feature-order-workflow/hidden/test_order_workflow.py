from __future__ import annotations

from datetime import date

import pytest

from stockroom.errors import ParseError, StockError, StockroomError
from stockroom.inventory import Inventory
from stockroom.models import STATUSES, Order, OrderLine
from stockroom.orders import OrderBook
from stockroom.workflow import advance, apply_transition, can_transition


def order(order_id: str, status: str = "open", lines: tuple[OrderLine, ...] | None = None,
          customer: str = "Hana Kim", day: int = 1) -> Order:
    if lines is None:
        lines = (OrderLine("MUG-001", 2, 1250),)
    return Order(order_id, customer, date(2024, 3, day), lines, status)


def test_can_transition_table() -> None:
    allowed = {("open", "paid"), ("open", "cancelled"), ("paid", "shipped"), ("paid", "cancelled")}
    for old in STATUSES:
        for new in STATUSES:
            assert can_transition(old, new) is ((old, new) in allowed), (old, new)


def test_can_transition_unknown_names() -> None:
    assert can_transition("open", "archived") is False
    assert can_transition("archived", "paid") is False
    assert can_transition("", "") is False
    assert can_transition("Open", "paid") is False


def test_advance_returns_copy() -> None:
    original = order("A-1", lines=(OrderLine("MUG-001", 2, 1250), OrderLine("TEA-010", 1, 895)))
    paid = advance(original, "paid")
    assert paid is not original and paid.status == "paid" and original.status == "open"
    assert (paid.order_id, paid.customer, paid.placed, paid.lines) == (
        original.order_id,
        original.customer,
        original.placed,
        original.lines,
    )
    assert advance(paid, "cancelled").status == "cancelled"
    assert advance(paid, "shipped").status == "shipped"


def test_advance_errors() -> None:
    with pytest.raises(ParseError):
        advance(order("A-1"), "archived")
    with pytest.raises(ParseError):
        advance(order("A-1", "shipped"), "archived")
    from stockroom.errors import TransitionError

    for status, target in (
        ("open", "shipped"),
        ("open", "open"),
        ("paid", "open"),
        ("paid", "paid"),
        ("shipped", "cancelled"),
        ("cancelled", "open"),
        ("cancelled", "paid"),
    ):
        with pytest.raises(TransitionError):
            advance(order("A-1", status), target)


def test_transition_error_is_defined_in_errors() -> None:
    from stockroom import errors

    assert issubclass(errors.TransitionError, StockroomError)


def test_replace_keeps_position() -> None:
    book = OrderBook([order("A-1", day=3), order("A-2", day=2), order("A-3", day=1)])
    book.replace(order("A-2", "paid", day=2))
    assert [o.order_id for o in book] == ["A-1", "A-2", "A-3"]
    assert [o.status for o in book] == ["open", "paid", "open"]
    assert book.get("A-2").status == "paid" and len(book) == 3
    # ties in by_customer keep insertion order: A-1 and A-2 share a date here
    tied = OrderBook([order("B-1", day=5), order("B-2", day=5), order("B-3", day=5)])
    tied.replace(order("B-1", "paid", day=5))
    assert [o.order_id for o in tied.by_customer("Hana Kim")] == ["B-1", "B-2", "B-3"]


def test_replace_unknown_id() -> None:
    book = OrderBook([order("A-1")])
    with pytest.raises(StockroomError):
        book.replace(order("A-9", "paid"))
    assert [o.order_id for o in book] == ["A-1"]
    with pytest.raises(StockroomError):
        book.history("A-9")


def test_history_records_status_changes() -> None:
    book = OrderBook([order("A-1"), order("A-2")])
    assert book.history("A-1") == [] and book.history("A-2") == []
    book.replace(order("A-1", "open", day=9))  # same status: no entry
    assert book.history("A-1") == []
    book.replace(order("A-1", "paid"))
    book.replace(order("A-1", "shipped"))
    assert book.history("A-1") == [("open", "paid"), ("paid", "shipped")]
    assert book.history("A-2") == []
    late = OrderBook()
    late.add(order("A-5", "shipped"))
    assert late.history("A-5") == []


def test_history_is_a_copy() -> None:
    book = OrderBook([order("A-1")])
    book.replace(order("A-1", "paid"))
    first = book.history("A-1")
    first.append(("x", "y"))
    assert book.history("A-1") == [("open", "paid")]
    assert book.history("A-1") is not book.history("A-1")


def test_apply_without_inventory() -> None:
    book = OrderBook([order("A-1", day=1), order("A-2", day=2), order("A-3", day=3)])
    updated = apply_transition(book, "A-2", "paid")
    assert updated.status == "paid" and updated.order_id == "A-2"
    assert book.get("A-2") == updated
    assert [o.order_id for o in book] == ["A-1", "A-2", "A-3"]
    assert book.history("A-2") == [("open", "paid")]
    assert apply_transition(book, "A-2", "cancelled").status == "cancelled"
    assert book.history("A-2") == [("open", "paid"), ("paid", "cancelled")]
    assert [o.status for o in book] == ["open", "cancelled", "open"]


def test_apply_failures_change_nothing() -> None:
    from stockroom.errors import TransitionError

    book = OrderBook([order("A-1"), order("A-2", "shipped")])
    inv = Inventory.from_levels([("MUG-001", 5, 2)])
    with pytest.raises(StockroomError):
        apply_transition(book, "A-9", "paid", inv)
    with pytest.raises(ParseError):
        apply_transition(book, "A-1", "archived", inv)
    with pytest.raises(TransitionError):
        apply_transition(book, "A-1", "shipped", inv)
    with pytest.raises(TransitionError):
        apply_transition(book, "A-2", "cancelled", inv)
    assert [o.status for o in book] == ["open", "shipped"]
    assert book.history("A-1") == [] and book.history("A-2") == []
    assert inv.snapshot() == {"MUG-001": (5, 2)}


def test_apply_cancel_releases_stock() -> None:
    lines = (OrderLine("MUG-001", 2, 1250), OrderLine("TEA-010", 3, 895), OrderLine("MUG-001", 1, 1250))
    book = OrderBook([order("A-1", lines=lines), order("A-2", "paid", lines=lines)])
    inv = Inventory.from_levels([("MUG-001", 10, 7), ("TEA-010", 8, 6)])
    apply_transition(book, "A-1", "cancelled", inv)
    assert inv.snapshot() == {"MUG-001": (10, 4), "TEA-010": (8, 3)}
    apply_transition(book, "A-2", "cancelled", inv)
    assert inv.snapshot() == {"MUG-001": (10, 1), "TEA-010": (8, 0)}
    assert [o.status for o in book] == ["cancelled", "cancelled"]


def test_apply_ship_ships_stock() -> None:
    lines = (OrderLine("MUG-001", 2, 1250), OrderLine("TEA-010", 3, 895), OrderLine("MUG-001", 1, 1250))
    book = OrderBook([order("A-1", "paid", lines=lines)])
    inv = Inventory.from_levels([("MUG-001", 10, 4), ("TEA-010", 8, 3), ("NB-200", 1, 1)])
    updated = apply_transition(book, "A-1", "shipped", inv)
    assert updated.status == "shipped"
    assert inv.snapshot() == {"MUG-001": (7, 1), "NB-200": (1, 1), "TEA-010": (5, 0)}
    assert book.history("A-1") == [("paid", "shipped")]


def test_apply_paid_leaves_stock() -> None:
    book = OrderBook([order("A-1")])
    inv = Inventory.from_levels([("MUG-001", 10, 4)])
    apply_transition(book, "A-1", "paid", inv)
    assert inv.snapshot() == {"MUG-001": (10, 4)}
    assert book.get("A-1").status == "paid"
    # an inventory that knows nothing about the order's SKUs is fine for paid
    other = OrderBook([order("B-1")])
    apply_transition(other, "B-1", "paid", Inventory())
    assert other.get("B-1").status == "paid"


def test_apply_is_all_or_nothing_on_cancel() -> None:
    lines = (OrderLine("MUG-001", 2, 1250), OrderLine("TEA-010", 3, 895))
    book = OrderBook([order("A-0"), order("A-1", "paid", lines=lines)])
    inv = Inventory.from_levels([("MUG-001", 10, 4), ("TEA-010", 8, 2)])
    with pytest.raises(StockError):
        apply_transition(book, "A-1", "cancelled", inv)
    assert inv.snapshot() == {"MUG-001": (10, 4), "TEA-010": (8, 2)}
    assert book.get("A-1").status == "paid" and book.history("A-1") == []
    assert [o.order_id for o in book] == ["A-0", "A-1"]


def test_apply_is_all_or_nothing_on_ship() -> None:
    lines = (
        OrderLine("MUG-001", 2, 1250),
        OrderLine("TEA-010", 1, 895),
        OrderLine("MUG-001", 3, 1250),
    )
    book = OrderBook([order("A-1", "paid", lines=lines)])
    inv = Inventory.from_levels([("MUG-001", 10, 4), ("TEA-010", 8, 1)])
    with pytest.raises(StockError):
        apply_transition(book, "A-1", "shipped", inv)  # MUG-001: 2 + 3 > 4 reserved
    assert inv.snapshot() == {"MUG-001": (10, 4), "TEA-010": (8, 1)}
    assert book.get("A-1").status == "paid" and book.history("A-1") == []
    inv2 = Inventory.from_levels([("MUG-001", 10, 5), ("TEA-010", 8, 1)])
    apply_transition(book, "A-1", "shipped", inv2)
    assert inv2.snapshot() == {"MUG-001": (5, 0), "TEA-010": (7, 0)}
