import os
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from stockroom.cashback import cashback_by_customer, order_cashback
from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook

ROOT = Path(__file__).resolve().parents[3]
ORDERS = (
    "order_id,customer,placed,status,sku,quantity,unit_price\n"
    "K-1,Hana Kim,2024-01-08,paid,PEN-100,3,0.10\n"
    "K-2,Hana Kim,2024-01-09,shipped,PEN-100,3,0.10\n"
    "K-3,Omar Diaz,2024-01-10,shipped,MUG-001,1,33.34\n"
    "K-4,Omar Diaz,2024-01-11,paid,MUG-001,1,33.34\n"
    "K-5,Lena Berg,2024-01-12,open,LMP-030,1,100.00\n"
    "K-6,Lena Berg,2024-01-13,cancelled,LMP-030,1,100.00\n"
    "K-7,Bo Ek,2024-01-14,shipped,LMP-030,1,100.00\n"
    "K-8,Ari Moss,2024-01-15,paid,LMP-030,1,100.00\n"
    "K-9,Ari Moss,2024-01-16,open,NB-200,2,6.25\n"
)


def order(oid: str, customer: str, status: str, *lines: tuple[int, int]) -> Order:
    return Order(
        oid,
        customer,
        date(2024, 1, 8),
        tuple(OrderLine("MUG-001", quantity, price) for quantity, price in lines),
        status,
    )


def run(
    tmp_path: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "orders.csv").write_text(ORDERS, encoding="utf-8")
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", "cashback", "--orders", "orders.csv", *args],
        cwd=tmp_path,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_cashback_of_one_order_is_rounded_half_up() -> None:
    assert order_cashback(order("A", "x", "paid", (1, 100)), 50) == 1
    assert order_cashback(order("A", "x", "shipped", (1, 99)), 50) == 0
    assert order_cashback(order("A", "x", "paid", (2, 1250), (1, 895)), 825) == 280
    assert order_cashback(order("A", "x", "open", (1, 10000)), 150) == 0
    assert order_cashback(order("A", "x", "cancelled", (1, 10000)), 150) == 0
    assert order_cashback(order("A", "x", "paid", (1, 10000)), 10000) == 10000
    assert order_cashback(order("A", "x", "paid", (1, 10000)), 0) == 0
    for bad in (-1, 10001):
        with pytest.raises(ValueError):
            order_cashback(order("A", "x", "cancelled", (1, 100)), bad)
        with pytest.raises(ValueError):
            cashback_by_customer(OrderBook(), bad)


def test_cashback_is_rounded_per_order_and_then_added() -> None:
    hana = OrderBook(
        [order("H-1", "Hana Kim", "paid", (3, 10)), order("H-2", "Hana Kim", "shipped", (3, 10))]
    )
    assert cashback_by_customer(hana, 150) == [("Hana Kim", 0)]
    rng = random.Random(1500)
    for _ in range(300):
        orders = [
            order(
                f"R-{i}",
                rng.choice(["Ann", "Ben", "Cat", "ann"]),
                rng.choice(["open", "paid", "shipped", "cancelled"]),
                *[
                    (rng.randrange(1, 5), rng.randrange(0, 5000))
                    for _ in range(rng.randrange(1, 4))
                ],
            )
            for i in range(rng.randrange(0, 10))
        ]
        rate = rng.randrange(0, 10001)
        expected: dict[str, int] = {}
        for o in orders:
            if o.status in ("paid", "shipped"):
                subtotal = sum(line.quantity * line.unit_price_cents for line in o.lines)
                expected[o.customer] = (
                    expected.get(o.customer, 0) + (subtotal * rate + 5000) // 10000
                )
        got = cashback_by_customer(OrderBook(orders), rate)
        assert dict(got) == expected
        assert got == sorted(expected.items(), key=lambda pair: (-pair[1], pair[0]))


def test_cashback_lists_earning_customers_largest_first() -> None:
    book = OrderBook(
        [
            order("A-1", "Omar Diaz", "shipped", (1, 3334)),
            order("A-2", "Lena Berg", "open", (1, 10000)),
            order("A-3", "Lena Berg", "cancelled", (1, 10000)),
            order("A-4", "Bo Ek", "shipped", (1, 10000)),
            order("A-5", "Ari Moss", "paid", (1, 10000)),
            order("A-6", "Hana Kim", "paid", (3, 10)),
            order("A-7", "Omar Diaz", "paid", (1, 3334)),
        ]
    )
    assert cashback_by_customer(book, 150) == [
        ("Ari Moss", 150),
        ("Bo Ek", 150),
        ("Omar Diaz", 100),
        ("Hana Kim", 0),
    ]
    assert cashback_by_customer(OrderBook(), 150) == []
    only_open = OrderBook([order("O-1", "Kai Lund", "open", (1, 500))])
    assert cashback_by_customer(only_open, 150) == []


def test_cashback_command_prints_customers_and_total(tmp_path: Path) -> None:
    done = run(tmp_path, "--rate-bp", "150")
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert done.stdout.splitlines() == [
        "Ari Moss: $1.50",
        "Bo Ek: $1.50",
        "Omar Diaz: $1.00",
        "Hana Kim: $0.00",
        "total: $4.00",
    ]
    done = run(tmp_path, "--rate-bp", "10000", env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "})
    assert done.stdout.splitlines() == [
        "Ari Moss: EUR 100.00",
        "Bo Ek: EUR 100.00",
        "Omar Diaz: EUR 66.68",
        "Hana Kim: EUR 0.60",
        "total: EUR 267.28",
    ]
    done = run(tmp_path, "--rate-bp", "0")
    assert done.stdout.splitlines() == [
        "Ari Moss: $0.00",
        "Bo Ek: $0.00",
        "Hana Kim: $0.00",
        "Omar Diaz: $0.00",
        "total: $0.00",
    ]


def test_cashback_command_refuses_a_rate_out_of_range(tmp_path: Path) -> None:
    for rate in ("10001", "-1"):
        done = run(tmp_path, "--rate-bp", rate)
        assert done.returncode == 1
        assert done.stdout == ""
        assert done.stderr == "stockroom: error: --rate-bp must be between 0 and 10000\n"
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "orders.csv").write_text(
        "order_id,customer,placed,status,sku,quantity,unit_price\n", encoding="utf-8"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "stockroom",
            "cashback",
            "--orders",
            "orders.csv",
            "--rate-bp",
            "150",
        ],
        cwd=empty,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "total: $0.00\n"
