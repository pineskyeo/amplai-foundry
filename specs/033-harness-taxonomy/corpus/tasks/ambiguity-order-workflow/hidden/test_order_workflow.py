import random
from datetime import date

import pytest

from stockroom.errors import ParseError, StockError, StockroomError
from stockroom.inventory import Inventory
from stockroom.models import STATUSES, Order, OrderLine
from stockroom.orders import OrderBook
from stockroom.workflow import advance, allowed_next

ALLOWED = {
    "open": ("paid", "cancelled"),
    "paid": ("shipped", "cancelled"),
    "shipped": (),
    "cancelled": (),
}


def _order(status: str = "paid", *lines: tuple[str, int], order_id: str = "A-1") -> Order:
    return Order(
        order_id, "Hana Kim", date(2024, 8, 1), tuple(OrderLine(s, q, 100) for s, q in lines), status
    )


def test_each_status_allows_only_its_next_statuses() -> None:
    for status, expected in ALLOWED.items():
        assert allowed_next(status) == expected
    with pytest.raises(ParseError) as caught:
        allowed_next("done")
    assert str(caught.value) == "unknown status 'done'"


def test_every_pair_of_statuses_is_either_done_or_refused_with_the_stated_message() -> None:
    for old in STATUSES:
        for new in STATUSES:
            order = _order(old, ("MUG-001", 1))
            if new in ALLOWED[old]:
                done = advance(order, new)
                assert done.status == new and done.lines == order.lines
                assert done.order_id == order.order_id and done.customer == order.customer
                assert done.placed == order.placed
                assert order.status == old
            else:
                with pytest.raises(StockroomError) as caught:
                    advance(order, new)
                assert str(caught.value) == f"A-1: cannot change status from {old} to {new}"
                assert order.status == old


def test_an_unknown_new_status_is_refused_before_anything_else() -> None:
    inventory = Inventory.from_levels([("MUG-001", 5, 1)])
    for status in STATUSES:
        with pytest.raises(ParseError) as caught:
            advance(_order(status, ("MUG-001", 9)), "done", inventory)
        assert str(caught.value) == "A-1: unknown status 'done'"
    with pytest.raises(ParseError) as caught:
        advance(_order("paid", order_id="Z-9"), "Shipped")
    assert str(caught.value) == "Z-9: unknown status 'Shipped'"
    assert inventory.snapshot() == {"MUG-001": (5, 1)}


def test_shipping_a_paid_order_takes_the_units_of_each_sku_out_of_the_stock() -> None:
    inventory = Inventory.from_levels([("MUG-001", 9, 5), ("TEA-010", 4, 1), ("PEN-100", 3, 3)])
    order = _order("paid", ("MUG-001", 2), ("TEA-010", 1), ("MUG-001", 3))
    shipped = advance(order, "shipped", inventory)
    assert shipped.status == "shipped" and order.status == "paid"
    assert inventory.snapshot() == {"MUG-001": (4, 0), "PEN-100": (3, 3), "TEA-010": (3, 0)}


def test_a_shortage_on_any_sku_refuses_the_shipment_and_changes_nothing() -> None:
    inventory = Inventory.from_levels([("MUG-001", 9, 4), ("TEA-010", 4, 4), ("PEN-100", 3, 0)])
    before = inventory.snapshot()
    order = _order("paid", ("TEA-010", 1), ("MUG-001", 2), ("MUG-001", 3), ("PEN-100", 1))
    with pytest.raises(StockError) as caught:
        advance(order, "shipped", inventory)
    assert str(caught.value) == "MUG-001: only 4 reserved"
    assert inventory.snapshot() == before
    order = _order("paid", ("PEN-100", 1), ("MUG-001", 5), ("TEA-010", 1))
    with pytest.raises(StockError) as caught:
        advance(order, "shipped", inventory)
    assert str(caught.value) == "PEN-100: only 0 reserved"
    assert inventory.snapshot() == before
    unknown = _order("paid", ("NB-200", 1))
    with pytest.raises(StockError) as caught:
        advance(unknown, "shipped", inventory)
    assert str(caught.value) == "NB-200: only 0 reserved"
    assert order.status == "paid"


def test_cancelling_a_paid_order_releases_what_is_reserved_up_to_its_quantities() -> None:
    inventory = Inventory.from_levels([("MUG-001", 9, 3), ("TEA-010", 4, 4), ("PEN-100", 3, 2)])
    order = _order("paid", ("MUG-001", 2), ("TEA-010", 1), ("MUG-001", 3), ("NB-200", 7))
    cancelled = advance(order, "cancelled", inventory)
    assert cancelled.status == "cancelled"
    assert inventory.snapshot() == {"MUG-001": (9, 0), "PEN-100": (3, 2), "TEA-010": (4, 3)}
    assert "NB-200" not in inventory.snapshot()


def test_open_orders_and_a_missing_inventory_do_not_touch_any_stock() -> None:
    inventory = Inventory.from_levels([("MUG-001", 9, 3), ("TEA-010", 4, 4)])
    before = inventory.snapshot()
    order = _order("open", ("MUG-001", 2), ("TEA-010", 9))
    assert advance(order, "paid", inventory).status == "paid"
    assert advance(order, "cancelled", inventory).status == "cancelled"
    assert inventory.snapshot() == before
    paid = _order("paid", ("MUG-001", 50))
    assert advance(paid, "shipped").status == "shipped"
    assert advance(paid, "cancelled", None).status == "cancelled"
    assert advance(_order("paid"), "shipped", inventory).status == "shipped"
    assert advance(_order("paid"), "cancelled", inventory).status == "cancelled"
    assert inventory.snapshot() == before


def test_a_refused_change_leaves_the_stock_alone() -> None:
    inventory = Inventory.from_levels([("MUG-001", 9, 3)])
    for old, new in (("shipped", "cancelled"), ("cancelled", "paid"), ("open", "shipped"), ("paid", "paid")):
        with pytest.raises(StockroomError):
            advance(_order(old, ("MUG-001", 1)), new, inventory)
    assert inventory.snapshot() == {"MUG-001": (9, 3)}


def test_the_order_book_keeps_the_position_and_returns_the_changed_order() -> None:
    book = OrderBook(
        [
            _order("open", ("MUG-001", 1), order_id="A-1"),
            _order("paid", ("MUG-001", 2), order_id="A-2"),
            _order("paid", ("TEA-010", 9), order_id="A-3"),
        ]
    )
    inventory = Inventory.from_levels([("MUG-001", 5, 2), ("TEA-010", 5, 5)])
    done = book.advance("A-2", "shipped", inventory)
    assert done.status == "shipped" and book.get("A-2") is done
    assert [(o.order_id, o.status) for o in book] == [("A-1", "open"), ("A-2", "shipped"), ("A-3", "paid")]
    assert inventory.snapshot() == {"MUG-001": (3, 0), "TEA-010": (5, 5)}
    assert book.advance("A-1", "paid").status == "paid"
    with pytest.raises(StockError):
        book.advance("A-3", "shipped", inventory)
    assert book.get("A-3").status == "paid" and inventory.snapshot() == {"MUG-001": (3, 0), "TEA-010": (5, 5)}
    with pytest.raises(StockroomError):
        book.advance("A-2", "cancelled", inventory)
    assert book.get("A-2").status == "shipped"
    with pytest.raises(StockroomError) as caught:
        book.advance("A-9", "paid")
    assert str(caught.value) == "no order A-9"
    assert len(book) == 3


def test_random_workflows_match_a_model_of_the_rules() -> None:
    rng = random.Random(29)
    skus = ["MUG-001", "TEA-010", "PEN-100"]
    for _ in range(400):
        levels = []
        for sku in skus:
            on_hand = rng.randint(0, 8)
            levels.append((sku, on_hand, rng.randint(0, on_hand)))
        inventory = Inventory.from_levels(levels)
        model = {sku: [on_hand, reserved] for sku, on_hand, reserved in levels}
        status = rng.choice(["open", "paid"])
        lines = [(rng.choice(skus), rng.randint(1, 4)) for _ in range(rng.randint(0, 4))]
        order = _order(status, *lines)
        for _step in range(3):
            new = rng.choice(list(STATUSES))
            before = inventory.snapshot()
            totals: dict[str, int] = {}
            for sku, quantity in lines:
                totals[sku] = totals.get(sku, 0) + quantity
            if new not in ALLOWED[order.status]:
                with pytest.raises(StockroomError):
                    advance(order, new, inventory)
                assert inventory.snapshot() == before
                continue
            short = [s for s, q in totals.items() if q > model[s][1]]
            if (order.status, new) == ("paid", "shipped") and short:
                with pytest.raises(StockError) as caught:
                    advance(order, new, inventory)
                assert str(caught.value) == f"{short[0]}: only {model[short[0]][1]} reserved"
                assert inventory.snapshot() == before
                continue
            if (order.status, new) == ("paid", "shipped"):
                for sku, quantity in totals.items():
                    model[sku][0] -= quantity
                    model[sku][1] -= quantity
            if (order.status, new) == ("paid", "cancelled"):
                for sku, quantity in totals.items():
                    model[sku][1] -= min(quantity, model[sku][1])
            order = advance(order, new, inventory)
            assert order.status == new
            assert inventory.snapshot() == {s: tuple(v) for s, v in sorted(model.items())}
