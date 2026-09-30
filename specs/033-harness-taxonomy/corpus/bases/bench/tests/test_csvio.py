from datetime import date
from pathlib import Path

import pytest

from stockroom.csvio import (
    parse_tags,
    read_items,
    read_orders,
    read_stock,
    write_items,
    write_orders,
)
from stockroom.errors import ParseError
from stockroom.models import Item

DATA = Path(__file__).resolve().parents[1] / "data"


def test_parse_tags() -> None:
    assert parse_tags("Kitchen; gift") == ("kitchen", "gift")
    assert parse_tags("") == ()
    assert parse_tags("a;;b;") == ("a", "b")


def test_read_sample_items() -> None:
    items = read_items((DATA / "items.csv").read_text())
    assert len(items) == 8
    first = items[0]
    assert (first.sku, first.name, first.price_cents) == ("MUG-001", "Blue Mug", 1250)
    assert first.tags == ("kitchen", "gift") and first.reorder_level == 5


def test_read_items_errors() -> None:
    with pytest.raises(ParseError):
        read_items("")
    with pytest.raises(ParseError):
        read_items("sku,name\nMUG-001,Mug\n")
    with pytest.raises(ParseError):
        read_items("sku,name,price,tags,reorder_level\nMUG-001,Mug,1.00,,x\n")


def test_items_round_trip() -> None:
    items = [
        Item("MUG-001", "Blue Mug", 1250, ("kitchen", "gift"), 5),
        Item("NB-200", "Notebook A5", 625, (), 0),
    ]
    assert read_items(write_items(items)) == items


def test_read_sample_stock() -> None:
    levels = read_stock((DATA / "stock.csv").read_text())
    assert levels[0] == ("MUG-001", 14, 2)
    assert len(levels) == 8


def test_read_stock_rejects_bad_header() -> None:
    with pytest.raises(ParseError):
        read_stock("sku,count\nMUG-001,3\n")


def test_read_sample_orders() -> None:
    orders = read_orders((DATA / "orders.csv").read_text())
    assert [o.order_id for o in orders] == [
        "A-1001",
        "A-1002",
        "A-1003",
        "A-1004",
        "A-1005",
        "A-1006",
    ]
    first = orders[0]
    assert first.customer == "Hana Kim" and first.placed == date(2024, 1, 8)
    assert first.status == "shipped" and len(first.lines) == 2
    assert first.lines[1].unit_price_cents == 895


def test_orders_round_trip() -> None:
    orders = read_orders((DATA / "orders.csv").read_text())
    assert read_orders(write_orders(orders)) == orders
