import json
from datetime import date
from pathlib import Path

import pytest

from stockroom.csvio import read_orders
from stockroom.errors import ParseError
from stockroom.jsonio import dump_orders, load_orders
from stockroom.models import STATUSES, Order, OrderLine
from stockroom.orders import OrderBook

DATA = Path(__file__).resolve().parents[3] / "data"


def _order(order_id: str, status: str) -> Order:
    return Order(
        order_id,
        "Hana Kim",
        date(2024, 1, 8),
        (OrderLine("MUG-001", 2, 1250), OrderLine("TEA-010", 1, 895)),
        status,
    )


def _text(status_field: str) -> str:
    return (
        '[{"order_id": "A-1", "customer": "Hana Kim", "placed": "2024-01-08", '
        f'{status_field}"lines": [{{"sku": "MUG-001", "quantity": 1, "unit_price_cents": 100}}]}}]'
    )


def test_every_status_survives_a_json_round_trip() -> None:
    orders = [_order(f"A-{i}", status) for i, status in enumerate(STATUSES)]
    loaded = load_orders(dump_orders(orders))
    assert loaded == orders
    assert [o.status for o in loaded] == ["open", "paid", "shipped", "cancelled"]
    for status in STATUSES:
        assert load_orders(dump_orders([_order("A-9", status)]))[0].status == status


def test_load_reads_the_status_key() -> None:
    assert load_orders(_text('"status": "shipped", '))[0].status == "shipped"
    assert load_orders(_text('"status": "cancelled", '))[0].status == "cancelled"
    assert load_orders(_text('"status": "open", '))[0].status == "open"
    assert load_orders(_text(""))[0].status == "open"


def test_a_status_outside_the_four_allowed_words_is_refused() -> None:
    for field in (
        '"status": "teleported", ',
        '"status": "Open", ',
        '"status": "", ',
        '"status": null, ',
        '"status": 5, ',
        '"status": ["open"], ',
        '"status": {"x": 1}, ',
    ):
        with pytest.raises(ParseError):
            load_orders(_text(field))


def test_the_written_json_keeps_its_status_key() -> None:
    written = json.loads(dump_orders([_order("A-1", "paid")]))
    assert written[0]["status"] == "paid"
    assert "state" not in written[0]
    assert sorted(written[0]) == ["customer", "lines", "order_id", "placed", "status"]


def test_the_sample_orders_keep_their_revenue_through_json() -> None:
    orders = read_orders((DATA / "orders.csv").read_text())
    loaded = load_orders(dump_orders(orders))
    assert loaded == orders
    before, after = OrderBook(orders), OrderBook(loaded)
    assert after.revenue_by_month() == before.revenue_by_month()
    assert after.revenue_by_month() == {"2024-01": 6795, "2024-02": 5440, "2024-03": 2730}
    assert [o.status for o in loaded] == ["shipped", "paid", "cancelled", "paid", "open", "open"]
