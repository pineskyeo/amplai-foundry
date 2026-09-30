import os
from datetime import date
from pathlib import Path

import pytest

from stockroom.cli import main
from stockroom.csvio import read_orders
from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook


@pytest.fixture(autouse=True)
def _clean_stockroom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("STOCKROOM_"):
            monkeypatch.delenv(name)


DATA = Path(__file__).resolve().parents[3] / "data"
HEADER = "order_id,customer,placed,status,sku,quantity,unit_price\n"


def _book() -> OrderBook:
    return OrderBook(read_orders((DATA / "orders.csv").read_text()))


def _ids(orders: list[Order]) -> list[str]:
    return [o.order_id for o in orders]


def test_between_includes_orders_placed_on_either_end_date() -> None:
    book = _book()
    assert _ids(book.between(date(2024, 1, 8), date(2024, 1, 19))) == ["A-1001", "A-1002"]
    assert _ids(book.between(date(2024, 1, 9), date(2024, 2, 14))) == [
        "A-1002",
        "A-1003",
        "A-1004",
    ]
    assert _ids(book.between(date(2024, 3, 21), date(2024, 3, 21))) == ["A-1006"]
    assert _ids(book.between(date(2024, 3, 2), date(2024, 12, 31))) == ["A-1005", "A-1006"]


def test_a_one_day_range_returns_that_days_orders_in_insertion_order() -> None:
    lines = (OrderLine("MUG-001", 1, 100),)
    day = date(2024, 5, 5)
    book = OrderBook(
        [
            Order("Z-1", "a", day, lines),
            Order("A-1", "b", date(2024, 5, 4), lines),
            Order("M-1", "c", day, lines),
            Order("B-1", "d", date(2024, 5, 6), lines),
        ]
    )
    assert _ids(book.between(day, day)) == ["Z-1", "M-1"]
    assert _ids(book.between(date(2024, 5, 4), day)) == ["A-1", "Z-1", "M-1"]
    assert _ids(book.between(day, date(2024, 5, 6))) == ["Z-1", "M-1", "B-1"]


def test_empty_and_reversed_ranges_find_nothing() -> None:
    book = _book()
    assert book.between(date(2024, 3, 22), date(2024, 12, 31)) == []
    assert book.between(date(2023, 1, 1), date(2024, 1, 7)) == []
    assert book.between(date(2024, 2, 14), date(2024, 1, 8)) == []
    assert book.between(date(2024, 1, 9), date(2024, 1, 18)) == []


def test_cli_to_date_is_the_last_day_included(capsys: pytest.CaptureFixture[str]) -> None:
    orders = str(DATA / "orders.csv")
    assert main(["sales", "--orders", orders, "--to", "2024-02-14"]) == 0
    assert capsys.readouterr().out.splitlines()[:2] == [
        "3 orders, 1 cancelled",
        "Revenue: $122.35",
    ]
    assert main(["sales", "--orders", orders, "--to", "2024-02-13"]) == 0
    assert capsys.readouterr().out.splitlines()[:2] == [
        "2 orders, 1 cancelled",
        "Revenue: $67.95",
    ]
    assert main(["sales", "--orders", orders, "--to", "2024-03-21"]) == 0
    assert capsys.readouterr().out.splitlines()[:2] == ["5 orders, 1 cancelled", "Revenue: $149.65"]


def test_cli_single_day_range(capsys: pytest.CaptureFixture[str]) -> None:
    orders = str(DATA / "orders.csv")
    day = "2024-03-21"
    assert main(["sales", "--orders", orders, "--from", day, "--to", day]) == 0
    assert capsys.readouterr().out.splitlines()[:2] == ["1 order, 0 cancelled", "Revenue: $12.50"]
    assert main(["sales", "--orders", orders, "--from", "2024-01-08", "--to", "2024-01-08"]) == 0
    assert capsys.readouterr().out.splitlines()[:2] == ["1 order, 0 cancelled", "Revenue: $33.95"]


def test_cli_without_to_has_no_upper_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "orders.csv"
    path.write_text(
        HEADER
        + "A-1,Hana Kim,2024-01-08,paid,MUG-001,1,5.00\n"
        + "A-2,Hana Kim,9999-12-31,paid,MUG-001,1,7.00\n"
    )
    assert main(["sales", "--orders", str(path), "--from", "2024-01-01"]) == 0
    assert capsys.readouterr().out.splitlines()[:2] == ["2 orders, 0 cancelled", "Revenue: $12.00"]
    assert main(["sales", "--orders", str(path), "--from", "9999-12-31"]) == 0
    assert capsys.readouterr().out.splitlines()[:2] == ["1 order, 0 cancelled", "Revenue: $7.00"]
    assert main(["sales", "--orders", str(path), "--to", "2024-01-08"]) == 0
    assert capsys.readouterr().out.splitlines()[:2] == ["1 order, 0 cancelled", "Revenue: $5.00"]
    assert main(["sales", "--orders", str(path), "--to", "2024-01-07"]) == 0
    assert capsys.readouterr().out.splitlines()[:2] == ["0 orders, 0 cancelled", "Revenue: $0.00"]
