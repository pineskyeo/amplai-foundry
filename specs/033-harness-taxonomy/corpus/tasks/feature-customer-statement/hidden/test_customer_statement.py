from __future__ import annotations

import inspect
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from stockroom.errors import StockroomError
from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook
from stockroom.report import customer_statement

ROOT = Path(__file__).resolve().parents[3]


def order(
    order_id: str,
    customer: str,
    placed: tuple[int, int, int],
    status: str,
    *amounts: tuple[int, int],
) -> Order:
    lines = tuple(OrderLine("MUG-001", q, price) for q, price in amounts)
    return Order(order_id, customer, date(*placed), lines, status)


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_customers_sorted_distinct() -> None:
    assert OrderBook().customers() == []
    book = OrderBook(
        [
            order("A-1", "omar", (2024, 1, 1), "open", (1, 100)),
            order("A-2", "Zed", (2024, 1, 2), "open", (1, 100)),
            order("A-3", "Hana Kim", (2024, 1, 3), "open", (1, 100)),
            order("A-4", "omar", (2024, 1, 4), "open", (1, 100)),
            order("A-5", "Omar", (2024, 1, 5), "open", (1, 100)),
        ]
    )
    assert book.customers() == ["Hana Kim", "Omar", "Zed", "omar"]


def test_header_and_unknown_customer() -> None:
    book = OrderBook(
        [
            order("A-1", "Hana Kim", (2024, 1, 8), "paid", (2, 1250)),
            order("A-2", "Hana Kim", (2024, 1, 9), "cancelled", (1, 1250)),
            order("A-3", "Hana Kim", (2024, 1, 10), "open", (1, 500)),
            order("A-4", "Omar Diaz", (2024, 1, 11), "cancelled", (1, 500)),
        ]
    )
    lines = customer_statement(book, "Hana Kim").split("\n")
    assert lines[0] == "Statement for Hana Kim"
    assert lines[1] == "2 orders, 1 cancelled"
    lines = customer_statement(book, "Omar Diaz").split("\n")
    assert lines[1] == "0 orders, 1 cancelled"
    single = OrderBook([order("B-1", "Lena Berg", (2024, 1, 1), "shipped", (1, 100))])
    assert customer_statement(single, "Lena Berg").split("\n")[1] == "1 order, 0 cancelled"
    for name in ("hana kim", "Nobody", ""):
        with pytest.raises(StockroomError) as caught:
            customer_statement(book, name)
        assert type(caught.value) is StockroomError
    assert not customer_statement(book, "Hana Kim").endswith("\n")


def test_order_lines_running_total_and_cancelled() -> None:
    book = OrderBook(
        [
            order("A-3", "Hana Kim", (2024, 3, 2), "open", (1, 1999)),
            order("A-1", "Hana Kim", (2024, 1, 8), "cancelled", (5, 1000)),
            order("A-2", "Hana Kim", (2024, 2, 14), "paid", (3, 980), (4, 625)),
            order("A-4", "Hana Kim", (2024, 3, 9), "cancelled", (1, 100)),
            order("A-5", "Hana Kim", (2024, 12, 31), "shipped", (2, 50000)),
            order("X-1", "Other", (2024, 1, 1), "open", (9, 9999)),
        ]
    )
    lines = customer_statement(book, "Hana Kim").split("\n")
    assert lines[2:7] == [
        "2024-01-08  A-1  cancelled  $50.00  $0.00",
        "2024-02-14  A-2  paid  $54.40  $54.40",
        "2024-03-02  A-3  open  $19.99  $74.39",
        "2024-03-09  A-4  cancelled  $1.00  $74.39",
        "2024-12-31  A-5  shipped  $1,000.00  $1,074.39",
    ]
    assert lines[7] == ""


def test_order_lines_same_date_keep_insertion_order() -> None:
    book = OrderBook(
        [
            order("B-9", "Hana Kim", (2024, 5, 5), "paid", (1, 100)),
            order("B-2", "Hana Kim", (2024, 5, 5), "paid", (1, 200)),
            order("B-5", "Hana Kim", (2024, 5, 4), "paid", (1, 300)),
        ]
    )
    lines = customer_statement(book, "Hana Kim").split("\n")
    assert [line.split("  ")[1] for line in lines[2:5]] == ["B-5", "B-9", "B-2"]
    assert lines[4].endswith("$6.00")


def test_footer_figures() -> None:
    book = OrderBook(
        [
            order("A-1", "Hana Kim", (2024, 1, 8), "shipped", (2, 1250), (1, 895)),
            order("A-2", "Hana Kim", (2024, 2, 14), "open", (3, 980)),
            order("A-3", "Hana Kim", (2024, 2, 20), "open", (1, 1000)),
            order("A-4", "Hana Kim", (2024, 3, 1), "cancelled", (9, 9999)),
            order("A-5", "Hana Kim", (2024, 3, 2), "paid", (1, 7000)),
        ]
    )
    lines = customer_statement(book, "Hana Kim").split("\n")
    assert lines[-5:] == [
        "",
        "Total: $143.35",
        "Due: $39.40",
        "Average: $35.84",
        "Largest: A-5 ($70.00)",
    ]
    assert len(lines) == 2 + 5 + 5


def test_average_rounds_half_up() -> None:
    def book(*cents: int) -> OrderBook:
        return OrderBook(
            [
                order(f"A-{i}", "Hana Kim", (2024, 1, 1 + i), "paid", (1, c))
                for i, c in enumerate(cents)
            ]
        )

    assert customer_statement(book(101, 100), "Hana Kim").endswith("Average: $1.01\nLargest: A-0 ($1.01)")
    assert "Average: $1.00\n" in customer_statement(book(100, 100, 101), "Hana Kim") + "\n"
    assert "Average: $0.02\n" in customer_statement(book(1, 2), "Hana Kim") + "\n"
    assert "Average: $0.01\n" in customer_statement(book(1, 1, 2), "Hana Kim") + "\n"
    assert "Average: $0.03\n" in customer_statement(book(1, 2, 3, 4), "Hana Kim") + "\n"
    assert "Average: $0.01\n" in customer_statement(book(0, 1), "Hana Kim") + "\n"
    assert "Average: $123.46\n" in customer_statement(book(12345, 12346), "Hana Kim") + "\n"


def test_largest_tie_and_no_active_orders() -> None:
    tie = OrderBook(
        [
            order("A-7", "Hana Kim", (2024, 1, 5), "paid", (1, 500)),
            order("A-1", "Hana Kim", (2024, 1, 6), "paid", (5, 100)),
            order("A-3", "Hana Kim", (2024, 1, 4), "paid", (1, 400)),
            order("A-9", "Hana Kim", (2024, 1, 3), "cancelled", (1, 90000)),
        ]
    )
    assert customer_statement(tie, "Hana Kim").split("\n")[-1] == "Largest: A-7 ($5.00)"
    none = OrderBook([order("A-1", "Hana Kim", (2024, 1, 5), "cancelled", (1, 500))])
    lines = customer_statement(none, "Hana Kim").split("\n")
    assert lines == [
        "Statement for Hana Kim",
        "0 orders, 1 cancelled",
        "2024-01-05  A-1  cancelled  $5.00  $0.00",
        "",
        "Total: $0.00",
        "Due: $0.00",
        "Average: -",
        "Largest: -",
    ]


def test_symbol_and_format() -> None:
    book = OrderBook([order("A-1", "Hana Kim", (2024, 1, 5), "open", (3, 123456))])
    text = customer_statement(book, "Hana Kim", symbol="EUR ")
    assert "2024-01-05  A-1  open  EUR 3,703.68  EUR 3,703.68" in text
    assert "Due: EUR 3,703.68" in text and "Largest: A-1 (EUR 3,703.68)" in text
    assert customer_statement(book, "Hana Kim", symbol="$") == customer_statement(book, "Hana Kim")
    with pytest.raises(TypeError):
        customer_statement(book, "Hana Kim", "$")  # type: ignore[misc]
    assert inspect.signature(customer_statement).parameters["symbol"].default == "$"


def test_cli_single_customer() -> None:
    done = run("statement", "--orders", "data/orders.csv", "--customer", "Hana Kim")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "Statement for Hana Kim",
        "2 orders, 0 cancelled",
        "2024-01-08  A-1001  shipped  $33.95  $33.95",
        "2024-02-14  A-1004  paid  $54.40  $88.35",
        "",
        "Total: $88.35",
        "Due: $0.00",
        "Average: $44.18",
        "Largest: A-1004 ($54.40)",
    ]


def test_cli_all_customers() -> None:
    done = run("statement", "--orders", "data/orders.csv")
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines.count("---") == 2
    starts = [line for line in lines if line.startswith("Statement for ")]
    assert starts == ["Statement for Hana Kim", "Statement for Lena Berg", "Statement for Omar Diaz"]
    first = lines.index("---")
    assert lines[first + 1] == "Statement for Lena Berg"
    assert lines[first - 1] == "Largest: A-1004 ($54.40)"
    assert "1 order, 1 cancelled" in lines
    assert lines[-1] == "Largest: A-1002 ($34.00)"
    omar = lines[lines.index("Statement for Omar Diaz") :]
    assert omar[2:4] == [
        "2024-01-19  A-1002  paid  $34.00  $34.00",
        "2024-03-21  A-1006  open  $12.50  $46.50",
    ]
    assert "Due: $12.50" in omar and "Average: $23.25" in omar and "Largest: A-1002 ($34.00)" in omar


def test_cli_unknown_customer_and_symbol_setting() -> None:
    done = run("statement", "--orders", "data/orders.csv", "--customer", "Nobody")
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error: ")
    done = run(
        "statement",
        "--orders",
        "data/orders.csv",
        "--customer",
        "Omar Diaz",
        env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "},
    )
    assert done.returncode == 0, done.stderr
    assert "2024-01-19  A-1002  paid  EUR 34.00  EUR 34.00" in done.stdout.splitlines()
