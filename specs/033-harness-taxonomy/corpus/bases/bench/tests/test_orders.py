from datetime import date
from pathlib import Path

import pytest

from stockroom.csvio import read_orders
from stockroom.errors import StockroomError
from stockroom.orders import OrderBook

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture
def book() -> OrderBook:
    return OrderBook(read_orders((DATA / "orders.csv").read_text()))


def test_lookup(book: OrderBook) -> None:
    assert len(book) == 6
    assert book.get("A-1002").customer == "Omar Diaz"
    with pytest.raises(StockroomError):
        book.get("A-9999")
    with pytest.raises(StockroomError):
        book.add(book.get("A-1001"))


def test_by_customer(book: OrderBook) -> None:
    assert [o.order_id for o in book.by_customer("Hana Kim")] == ["A-1001", "A-1004"]
    assert book.by_customer("Nobody") == []


def test_between(book: OrderBook) -> None:
    found = book.between(date(2024, 1, 10), date(2024, 2, 28))
    assert [o.order_id for o in found] == ["A-1002", "A-1003", "A-1004"]


def test_revenue_by_month(book: OrderBook) -> None:
    assert book.revenue_by_month() == {"2024-01": 6795, "2024-02": 5440, "2024-03": 2730}
