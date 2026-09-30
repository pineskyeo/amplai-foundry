from datetime import date

import pytest

from stockroom.errors import ParseError
from stockroom.models import Item, Order, OrderLine, check_sku


def test_check_sku() -> None:
    assert check_sku("MUG-001") == "MUG-001"
    for bad in ("mug-001", "M-001", "MUG001", "MUG-01", "MUGGY-001"):
        with pytest.raises(ParseError):
            check_sku(bad)


def test_item_validation() -> None:
    item = Item("TEA-010", "Green Tea", 895, ("food",), 10)
    assert item.price_cents == 895
    with pytest.raises(ParseError):
        Item("TEA-010", "  ", 895)
    with pytest.raises(ParseError):
        Item("TEA-010", "Tea", -1)


def test_order_line_and_order() -> None:
    lines = (OrderLine("MUG-001", 2, 1250), OrderLine("TEA-010", 1, 895))
    order = Order("A-1", "Hana Kim", date(2024, 1, 8), lines)
    assert order.status == "open" and order.quantity == 3
    with pytest.raises(ParseError):
        OrderLine("MUG-001", 0, 1250)
    with pytest.raises(ParseError):
        Order("A-2", "Hana Kim", date(2024, 1, 8), lines, status="lost")
