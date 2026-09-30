import json
from datetime import date

import pytest

from stockroom.errors import ParseError
from stockroom.jsonio import dump_orders, load_orders, order_to_dict
from stockroom.models import Order, OrderLine

ORDER = Order("A-1", "Hana Kim", date(2024, 1, 8), (OrderLine("MUG-001", 2, 1250),))


def test_order_to_dict() -> None:
    assert order_to_dict(ORDER) == {
        "order_id": "A-1",
        "customer": "Hana Kim",
        "placed": "2024-01-08",
        "status": "open",
        "lines": [{"sku": "MUG-001", "quantity": 2, "unit_price_cents": 1250}],
    }


def test_dump_is_sorted_json() -> None:
    text = dump_orders([ORDER])
    assert text.endswith("\n")
    assert json.loads(text)[0]["customer"] == "Hana Kim"
    assert text.index('"customer"') < text.index('"lines"') < text.index('"order_id"')


def test_round_trip_of_an_open_order() -> None:
    assert load_orders(dump_orders([ORDER])) == [ORDER]


def test_load_errors() -> None:
    with pytest.raises(ParseError):
        load_orders("{not json")
    with pytest.raises(ParseError):
        load_orders('{"order_id": "A-1"}')
    with pytest.raises(ParseError):
        load_orders('[{"order_id": "A-1"}]')
