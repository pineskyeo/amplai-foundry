import os
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook
from stockroom.report import dormant_customers

ROOT = Path(__file__).resolve().parents[3]
AS_OF = date(2024, 6, 30)

ORDERS = (
    "order_id,customer,placed,status,sku,quantity,unit_price\n"
    "D-1,Hana Kim,2024-01-08,shipped,MUG-001,1,12.50\n"
    "D-2,Hana Kim,2024-03-15,paid,TEA-010,1,8.95\n"
    "D-3,Omar Diaz,2024-02-01,paid,PEN-100,1,34.00\n"
    "D-4,Omar Diaz,2024-06-01,cancelled,PEN-100,1,34.00\n"
    "D-5,Lena Berg,2024-06-30,open,NB-200,2,6.25\n"
    "D-6,Ari Moss,2024-03-15,open,CND-007,1,9.80\n"
    "D-7,Zoe Park,2024-02-20,paid,MUG-002,1,12.50\n"
    "D-8,Zoe Park,2024-07-05,paid,MUG-002,1,12.50\n"
    "D-9,Kai Lund,2024-05-30,cancelled,TEA-011,1,7.40\n"
    "D-10,Bo Ek,2024-08-01,paid,TEA-011,1,7.40\n"
    "D-11,bea Ruiz,2024-01-31,shipped,NB-200,1,6.25\n"
    "D-12,Hana Kim,2024-02-02,paid,NB-200,1,6.25\n"
)


def make(order_id: str, customer: str, placed: date, status: str = "paid") -> Order:
    return Order(order_id, customer, placed, (OrderLine("MUG-001", 1, 1250),), status)


def sample_book() -> OrderBook:
    rows = [line.split(",") for line in ORDERS.splitlines()[1:]]
    return OrderBook(make(r[0], r[1], date.fromisoformat(r[2]), r[3]) for r in rows)


def dormant(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "orders.csv").write_text(ORDERS, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", "dormant", "--orders", "orders.csv", *extra],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_dormant_days_must_be_at_least_one() -> None:
    for bad in (0, -5):
        with pytest.raises(ValueError):
            dormant_customers(sample_book(), AS_OF, bad)
    with pytest.raises(ValueError):
        dormant_customers(OrderBook(), AS_OF, 0)


def test_dormant_ignores_cancelled_and_later_orders() -> None:
    found = dict(dormant_customers(sample_book(), AS_OF))
    assert "Kai Lund" not in found
    assert "Bo Ek" not in found
    assert "Lena Berg" not in found
    assert found["Omar Diaz"] == date(2024, 2, 1)
    assert found["Zoe Park"] == date(2024, 2, 20)
    assert found["Hana Kim"] == date(2024, 3, 15)
    assert dormant_customers(OrderBook(), AS_OF) == []
    recent = OrderBook([make("R-1", "Ivy Holm", AS_OF), make("R-2", "Ivy Holm", date(2023, 1, 1))])
    assert dormant_customers(recent, AS_OF, 1) == []


def test_dormant_list_is_oldest_first_with_ties_by_name() -> None:
    assert dormant_customers(sample_book(), AS_OF) == [
        ("bea Ruiz", date(2024, 1, 31)),
        ("Omar Diaz", date(2024, 2, 1)),
        ("Zoe Park", date(2024, 2, 20)),
        ("Ari Moss", date(2024, 3, 15)),
        ("Hana Kim", date(2024, 3, 15)),
    ]
    assert dormant_customers(sample_book(), AS_OF, 140) == [
        ("bea Ruiz", date(2024, 1, 31)),
        ("Omar Diaz", date(2024, 2, 1)),
    ]
    tied = OrderBook(
        [
            make("T-1", "ella", date(2024, 1, 5)),
            make("T-2", "Ella", date(2024, 1, 5)),
            make("T-3", "Ella ", date(2024, 1, 5)),
            make("T-4", "Adam", date(2024, 1, 6)),
        ]
    )
    assert [name for name, _ in dormant_customers(tied, AS_OF)] == ["Ella", "Ella ", "ella", "Adam"]


def test_dormant_command_prints_a_count_and_one_line_per_customer(tmp_path: Path) -> None:
    done = dormant(tmp_path, "--as-of", "2024-06-30", "--days", "100")
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert done.stdout.splitlines() == [
        "5 dormant customers",
        "bea Ruiz: last order 2024-01-31, 151 days ago",
        "Omar Diaz: last order 2024-02-01, 150 days ago",
        "Zoe Park: last order 2024-02-20, 131 days ago",
        "Ari Moss: last order 2024-03-15, 107 days ago",
        "Hana Kim: last order 2024-03-15, 107 days ago",
    ]
    done = dormant(tmp_path, "--as-of", "2024-06-30", "--days", "140")
    assert done.stdout.splitlines()[0] == "2 dormant customers"
    done = dormant(tmp_path, "--as-of", "2024-02-02", "--days", "200")
    assert done.stdout == "0 dormant customers\n"


def test_dormant_command_refuses_bad_options(tmp_path: Path) -> None:
    for args, message in (
        (("--as-of", "2024-13-01"), "not a date: '2024-13-01'"),
        (("--as-of", "2024-06-30", "--days", "0"), "--days must be at least 1"),
        (("--as-of", "2024-06-30", "--days", "-3"), "--days must be at least 1"),
    ):
        done = dormant(tmp_path, *args)
        assert done.returncode == 1
        assert done.stdout == ""
        assert done.stderr == f"stockroom: error: {message}\n"


def test_dormant_window_reaches_back_exactly_days(tmp_path: Path) -> None:
    edge = OrderBook(
        [
            make("E-1", "Ninety One", date(2024, 3, 31)),
            make("E-2", "Ninety", date(2024, 4, 1)),
            make("E-3", "Eighty Nine", date(2024, 4, 2)),
        ]
    )
    assert dormant_customers(edge, AS_OF) == [("Ninety One", date(2024, 3, 31))]
    assert dormant_customers(edge, AS_OF, 89) == [
        ("Ninety One", date(2024, 3, 31)),
        ("Ninety", date(2024, 4, 1)),
    ]
    rng = random.Random(9090)
    for _ in range(300):
        days = rng.randrange(1, 60)
        orders = [
            make(f"P-{i}", f"C{rng.randrange(6)}", AS_OF - timedelta(rng.randrange(0, 80)))
            for i in range(8)
        ]
        latest: dict[str, date] = {}
        for order in orders:
            latest[order.customer] = max(order.placed, latest.get(order.customer, date.min))
        expected = sorted(
            ((name, placed) for name, placed in latest.items() if (AS_OF - placed).days > days),
            key=lambda pair: (pair[1], pair[0]),
        )
        assert dormant_customers(OrderBook(orders), AS_OF, days) == expected
    done = dormant(tmp_path, "--as-of", "2024-01-09", "--days", "1")
    assert done.stdout == "0 dormant customers\n"
    done = dormant(tmp_path, "--as-of", "2024-01-10", "--days", "1")
    assert done.stdout.splitlines() == [
        "1 dormant customer",
        "Hana Kim: last order 2024-01-08, 2 days ago",
    ]
