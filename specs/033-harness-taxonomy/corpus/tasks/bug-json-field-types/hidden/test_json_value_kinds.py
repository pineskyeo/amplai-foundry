import copy
import json
import random
from datetime import date
from typing import Any

import pytest
from stockroom.errors import ParseError
from stockroom.jsonio import dump_orders, load_orders
from stockroom.models import Order, OrderLine


def good() -> dict[str, Any]:
    return {
        "order_id": "A-1",
        "customer": "Hana Kim",
        "placed": "2024-01-08",
        "lines": [
            {"sku": "MUG-001", "quantity": 2, "unit_price_cents": 1250},
            {"sku": "TEA-010", "quantity": 1, "unit_price_cents": 895},
        ],
    }


EXPECTED = Order(
    "A-1",
    "Hana Kim",
    date(2024, 1, 8),
    (OrderLine("MUG-001", 2, 1250), OrderLine("TEA-010", 1, 895)),
)


def load_one(obj: Any) -> list[Order]:
    return load_orders(json.dumps([obj]))


def refused(obj: Any) -> None:
    with pytest.raises(ParseError):
        load_one(obj)


def test_a_placed_value_that_is_not_text_is_refused() -> None:
    assert load_one(good()) == [EXPECTED]
    for placed in (20240108, 2024.0108, None, True, ["2024-01-08"], {"y": 2024}):
        obj = good()
        obj["placed"] = placed
        refused(obj)
    refused({k: v for k, v in good().items() if k != "placed"})


def test_ids_and_customers_must_be_text() -> None:
    for key in ("order_id", "customer"):
        for wrong in (1001, 0, 1.5, None, False, ["A-1"], {"id": "A-1"}):
            obj = good()
            obj[key] = wrong
            refused(obj)
    obj = good()
    obj["customer"] = ""
    assert load_one(obj)[0].customer == ""
    obj = good()
    obj["order_id"] = "   "
    refused(obj)


def test_lines_must_be_a_list_of_objects() -> None:
    for wrong in ("", "MUG-001", {}, {"sku": "MUG-001"}, None, 3, True):
        obj = good()
        obj["lines"] = wrong
        refused(obj)
    for wrong_line in ("MUG-001", 5, None, [], ["MUG-001", 2, 1250], True):
        obj = good()
        obj["lines"] = [obj["lines"][0], wrong_line]
        refused(obj)
    obj = good()
    obj["lines"] = []
    assert load_one(obj)[0].lines == ()


def test_quantities_and_unit_prices_must_be_whole_numbers() -> None:
    for key in ("quantity", "unit_price_cents"):
        for wrong in (2.5, 2.0, 1e3, True, False, "2", "1250", None, [2], {"n": 2}):
            obj = good()
            obj["lines"][1][key] = wrong
            refused(obj)
    for text in (
        '"quantity": 2.0',
        '"quantity": 2e0',
        '"quantity": true',
        '"unit_price_cents": 1250.0',
    ):
        raw = (
            '[{"order_id": "A-1", "customer": "H", "placed": "2024-01-08", "lines": '
            '[{"sku": "MUG-001", "quantity": 2, "unit_price_cents": 1250, ' + text + "}]}]"
        )
        with pytest.raises(ParseError):
            load_orders(raw)
    obj = good()
    obj["lines"][0]["quantity"] = 10**30
    obj["lines"][1]["unit_price_cents"] = 0
    order = load_one(obj)[0]
    assert order.lines[0].quantity == 10**30 and order.lines[1].unit_price_cents == 0


def test_well_formed_objects_still_load_and_round_trip() -> None:
    obj = good()
    obj["note"] = {"anything": [1, 2.5, None]}
    obj["lines"][0]["colour"] = "blue"
    assert load_one(obj) == [EXPECTED]
    orders = [
        EXPECTED,
        Order("B-2", "Omar", date(1, 1, 1), ()),
        Order("C-3", "", date(9999, 12, 31), (OrderLine("NB-200", 10**12, 0),)),
    ]
    assert load_orders(dump_orders(orders)) == orders


POOL: list[Any] = [
    None,
    True,
    False,
    0,
    1,
    7,
    -3,
    2.5,
    3.0,
    "",
    "x",
    "2024-01-08",
    "MUG-001",
    "A-9",
    [],
    {},
    ["MUG-001"],
    {"a": 1},
]


def valid(field: str, value: Any) -> bool:
    if field == "order_id":
        return isinstance(value, str) and bool(value.strip())
    if field == "customer":
        return isinstance(value, str)
    if field == "placed":
        return value == "2024-01-08"
    if field == "lines":
        return value == []
    if field == "sku":
        return value == "MUG-001"
    is_int = type(value) is int
    if field == "quantity":
        return is_int and value > 0
    return is_int and value >= 0


def test_generated_field_values_are_accepted_exactly_when_well_typed() -> None:
    rng = random.Random(1999)
    fields = ["order_id", "customer", "placed", "lines", "sku", "quantity", "unit_price_cents"]
    for _ in range(1500):
        obj = good()
        field = rng.choice(fields)
        target = obj if field in fields[:4] else obj["lines"][rng.randrange(2)]
        missing = rng.random() < 0.08
        value = copy.deepcopy(rng.choice(POOL))
        if missing:
            del target[field]
        else:
            target[field] = value
        if missing or not valid(field, value):
            refused(obj)
            continue
        order = load_one(obj)[0]
        expected_lines = tuple(
            OrderLine(line["sku"], line["quantity"], line["unit_price_cents"])
            for line in obj["lines"]
        )
        assert order == Order(obj["order_id"], obj["customer"], date(2024, 1, 8), expected_lines)
