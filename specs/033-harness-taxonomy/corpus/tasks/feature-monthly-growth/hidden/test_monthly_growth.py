from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook
from stockroom.report import format_growth, monthly_growth

ROOT = Path(__file__).resolve().parents[3]


def sale(order_id: str, year: int, month: int, cents: int, status: str = "paid", day: int = 15) -> Order:
    return Order(
        order_id, "Hana Kim", date(year, month, day), (OrderLine("MUG-001", 1, cents),), status
    )


def book(*orders: Order) -> OrderBook:
    return OrderBook(orders)


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


def test_months_fill_gaps_and_cross_years() -> None:
    rows = monthly_growth(
        book(
            sale("A-1", 2023, 11, 1000, day=30),
            sale("A-2", 2024, 2, 2000, day=29),
            sale("A-3", 2024, 2, 500, day=1),
        )
    )
    assert [r[0] for r in rows] == ["2023-11", "2023-12", "2024-01", "2024-02"]
    assert [r[1] for r in rows] == [1000, 0, 0, 2500]
    long = monthly_growth(book(sale("B-1", 2022, 12, 100, day=31), sale("B-2", 2024, 1, 100, day=31)))
    assert [r[0] for r in long][:3] == ["2022-12", "2023-01", "2023-02"]
    assert len(long) == 14 and long[-1][0] == "2024-01"
    assert len(monthly_growth(book(sale("C-1", 2024, 5, 100, day=31)))) == 1


def test_range_ignores_cancelled_only_edges() -> None:
    rows = monthly_growth(
        book(
            sale("A-1", 2024, 1, 9999, "cancelled"),
            sale("A-2", 2024, 2, 1000),
            sale("A-3", 2024, 3, 7777, "cancelled"),
            sale("A-4", 2024, 4, 1500, "shipped"),
            sale("A-5", 2024, 5, 4242, "cancelled"),
        )
    )
    assert [(r[0], r[1]) for r in rows] == [("2024-02", 1000), ("2024-03", 0), ("2024-04", 1500)]


def test_empty_and_cancelled_only_books() -> None:
    assert monthly_growth(OrderBook()) == []
    assert monthly_growth(book(sale("A-1", 2024, 1, 500, "cancelled"))) == []


def test_tuple_shape_and_revenue() -> None:
    rows = monthly_growth(book(sale("A-1", 2024, 1, 1000, "open"), sale("A-2", 2024, 1, 250)))
    assert rows == [("2024-01", 1250, None)]
    assert isinstance(rows, list) and isinstance(rows[0], tuple)
    two = OrderBook(
        [
            Order(
                "A-1",
                "Hana Kim",
                date(2024, 1, 2),
                (OrderLine("MUG-001", 2, 1250), OrderLine("TEA-010", 3, 895)),
                "paid",
            )
        ]
    )
    assert monthly_growth(two)[0][1] == 5185


def test_growth_values() -> None:
    rows = monthly_growth(
        book(
            sale("A-1", 2024, 1, 6795),
            sale("A-2", 2024, 2, 5440),
            sale("A-3", 2024, 3, 2730),
            sale("A-4", 2024, 4, 5460),
            sale("A-5", 2024, 5, 5460),
            sale("A-6", 2024, 6, 10000),
        )
    )
    assert [r[2] for r in rows] == [None, -1994, -4982, 10000, 0, 8315]


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        (20000, 20001, 1),  # +0.5 -> 1
        (20000, 19999, 0),  # -0.5 -> 0
        (20000, 19997, -1),  # -1.5 -> -1
        (20000, 19995, -2),  # -2.5 -> -2
        (20000, 20005, 3),  # +2.5 -> 3
        (30000, 30001, 0),  # +0.333 -> 0
        (30000, 29999, 0),  # -0.333 -> 0
        (30000, 30002, 1),  # +0.667 -> 1
        (30000, 29998, -1),  # -0.667 -> -1
        (10000, 10100, 100),
        (10000, 9900, -100),
    ],
)
def test_growth_rounds_half_up(previous: int, current: int, expected: int) -> None:
    rows = monthly_growth(book(sale("A-1", 2024, 1, previous), sale("A-2", 2024, 2, current)))
    assert rows[1][2] == expected


def test_growth_after_zero_month_is_none() -> None:
    rows = monthly_growth(
        book(sale("A-1", 2024, 1, 1000), sale("A-2", 2024, 3, 3000), sale("A-3", 2024, 4, 3300))
    )
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("2024-01", 1000, None),
        ("2024-02", 0, -10000),
        ("2024-03", 3000, None),
        ("2024-04", 3300, 1000),
    ]


def test_format_growth() -> None:
    cases = {
        None: "n/a",
        1250: "+12.50%",
        -307: "-3.07%",
        5: "+0.05%",
        -5: "-0.05%",
        0: "0.00%",
        -10000: "-100.00%",
        123456: "+1234.56%",
        100: "+1.00%",
        -1: "-0.01%",
        99: "+0.99%",
        -101: "-1.01%",
    }
    for value, text in cases.items():
        assert format_growth(value) == text


def test_cli_sample_data() -> None:
    done = run("growth", "--orders", "data/orders.csv")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "2024-01  $67.95  n/a",
        "2024-02  $54.40  -19.94%",
        "2024-03  $27.30  -49.82%",
    ]


def test_cli_no_revenue_and_symbol(tmp_path: Path) -> None:
    orders = tmp_path / "orders.csv"
    orders.write_text(
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        "A-1,Hana Kim,2023-12-05,cancelled,MUG-001,1,12.50\n"
    )
    done = run("growth", "--orders", str(orders))
    assert done.returncode == 0 and done.stdout.splitlines() == ["no revenue"]
    orders.write_text(
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        "A-1,Hana Kim,2023-12-05,paid,MUG-001,2,1250.00\n"
        "A-2,Hana Kim,2024-02-05,paid,MUG-001,1,1000.00\n"
    )
    done = run("growth", "--orders", str(orders), env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "})
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "2023-12  EUR 2,500.00  n/a",
        "2024-01  EUR 0.00  -100.00%",
        "2024-02  EUR 1,000.00  n/a",
    ]
