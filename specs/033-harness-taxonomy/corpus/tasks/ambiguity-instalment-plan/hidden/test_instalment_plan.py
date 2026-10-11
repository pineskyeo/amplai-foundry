import os
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from stockroom.errors import ParseError
from stockroom.instalments import plan_instalments
from stockroom.models import Order, OrderLine

ROOT = Path(__file__).resolve().parents[3]
ORDERS = (
    "order_id,customer,placed,status,sku,quantity,unit_price\n"
    "I-1,Hana Kim,2023-01-20,paid,MUG-001,8,12.50\n"
    "I-2,Omar Diaz,2023-01-21,cancelled,PEN-100,1,34.00\n"
    "I-3,Lena Berg,2023-01-22,open,MUG-001,2,12.50\n"
    "I-3,Lena Berg,2023-01-22,open,TEA-010,1,8.95\n"
    "I-4,Ari Moss,2023-01-23,open,PEN-100,2,0.01\n"
)


def order(*lines: tuple[int, int], status: str = "open") -> Order:
    return Order(
        "P-1",
        "Mia Chen",
        date(2023, 1, 2),
        tuple(OrderLine("MUG-001", quantity, price) for quantity, price in lines),
        status,
    )


def amounts(plan: list[tuple[date, int]]) -> list[int]:
    return [cents for _, cents in plan]


def run(
    tmp_path: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "orders.csv").write_text(ORDERS, encoding="utf-8")
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", "instalments", "--orders", "orders.csv", *args],
        cwd=tmp_path,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_instalments_split_like_split_evenly() -> None:
    first = date(2023, 1, 15)
    assert amounts(plan_instalments(order((8, 1250)), 3, first)) == [3334, 3333, 3333]
    assert amounts(plan_instalments(order((1, 1001)), 4, first)) == [251, 250, 250, 250]
    assert amounts(plan_instalments(order((2, 1)), 3, first)) == [1, 1, 0]
    assert amounts(plan_instalments(order((1, 0)), 2, first)) == [0, 0]
    assert amounts(plan_instalments(order((8, 1250)), 1, first)) == [10000]
    rng = random.Random(1212)
    for _ in range(400):
        lines = [(rng.randrange(1, 6), rng.randrange(0, 20000)) for _ in range(rng.randrange(1, 4))]
        count = rng.randrange(1, 13)
        tax_bp = rng.randrange(0, 2500)
        first = date(2023, rng.randrange(1, 13), rng.randrange(1, 29))
        subtotal = sum(q * p for q, p in lines)
        total = subtotal + (subtotal * tax_bp + 5000) // 10000
        base, extra = divmod(total, count)
        months = [first.month - 1 + i for i in range(count)]
        expected = [
            (date(first.year + m // 12, m % 12 + 1, first.day), base + 1 if i < extra else base)
            for i, m in enumerate(months)
        ]
        assert plan_instalments(order(*lines), count, first, tax_bp=tax_bp) == expected


def test_instalments_cover_the_order_total_with_tax() -> None:
    plan = plan_instalments(order((2, 1250), (1, 895)), 2, date(2023, 3, 1), tax_bp=825)
    assert amounts(plan) == [1838, 1837]
    assert sum(amounts(plan)) == 3675
    assert amounts(plan_instalments(order((1, 10000)), 3, date(2023, 3, 1), tax_bp=1000)) == [
        3667,
        3667,
        3666,
    ]


def test_instalments_fall_due_monthly_from_the_first_date() -> None:
    plan = plan_instalments(order((1, 400)), 4, date(2023, 1, 31))
    assert [due for due, _ in plan] == [
        date(2023, 1, 31),
        date(2023, 2, 28),
        date(2023, 3, 31),
        date(2023, 4, 30),
    ]
    plan = plan_instalments(order((1, 300)), 3, date(2023, 11, 30))
    assert [due for due, _ in plan] == [date(2023, 11, 30), date(2023, 12, 30), date(2024, 1, 30)]
    plan = plan_instalments(order((1, 1200)), 12, date(2023, 1, 15))
    assert [due for due, _ in plan][-1] == date(2023, 12, 15)


def test_instalments_refuse_bad_plans() -> None:
    for bad in (0, 13, -1):
        with pytest.raises(ValueError):
            plan_instalments(order((1, 100)), bad, date(2023, 1, 1))
        with pytest.raises(ValueError):
            plan_instalments(order((1, 100), status="cancelled"), bad, date(2023, 1, 1))
    with pytest.raises(ValueError):
        plan_instalments(order((1, 100)), 2, date(2023, 1, 1), tax_bp=-1)
    with pytest.raises(ParseError) as caught:
        plan_instalments(order((1, 100), status="cancelled"), 2, date(2023, 1, 1))
    assert str(caught.value) == "order P-1 is cancelled"
    for status in ("open", "paid", "shipped"):
        assert len(plan_instalments(order((1, 100), status=status), 2, date(2023, 1, 1))) == 2


def test_instalments_command_prints_the_plan(tmp_path: Path) -> None:
    done = run(tmp_path, "--order-id", "I-1", "--count", "3", "--first-due", "2023-01-31")
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert done.stdout.splitlines() == [
        "instalment 1/3 due 2023-01-31: $33.34",
        "instalment 2/3 due 2023-02-28: $33.33",
        "instalment 3/3 due 2023-03-31: $33.33",
        "total: $100.00",
    ]
    env = {"STOCKROOM_TAX_BP": "825", "STOCKROOM_CURRENCY_SYMBOL": "EUR "}
    done = run(tmp_path, "--order-id", "I-3", "--count", "2", "--first-due", "2023-11-30", env=env)
    assert done.stdout.splitlines() == [
        "instalment 1/2 due 2023-11-30: EUR 18.38",
        "instalment 2/2 due 2023-12-30: EUR 18.37",
        "total: EUR 36.75",
    ]
    done = run(tmp_path, "--order-id", "I-4", "--count", "3", "--first-due", "2023-02-01")
    assert done.stdout.splitlines() == [
        "instalment 1/3 due 2023-02-01: $0.01",
        "instalment 2/3 due 2023-03-01: $0.01",
        "instalment 3/3 due 2023-04-01: $0.00",
        "total: $0.02",
    ]


def test_instalments_command_refuses_bad_requests(tmp_path: Path) -> None:
    for args, message in (
        (
            ("--order-id", "I-1", "--count", "13", "--first-due", "2023-01-31"),
            "--count must be between 1 and 12",
        ),
        (
            ("--order-id", "I-1", "--count", "0", "--first-due", "2023-01-31"),
            "--count must be between 1 and 12",
        ),
        (
            ("--order-id", "I-2", "--count", "2", "--first-due", "2023-01-31"),
            "order I-2 is cancelled",
        ),
        (("--order-id", "I-9", "--count", "2", "--first-due", "2023-01-31"), "no order I-9"),
        (
            ("--order-id", "I-1", "--count", "2", "--first-due", "2023-02-29"),
            "not a date: '2023-02-29'",
        ),
    ):
        done = run(tmp_path, *args)
        assert done.returncode == 1
        assert done.stdout == ""
        assert done.stderr == f"stockroom: error: {message}\n"
