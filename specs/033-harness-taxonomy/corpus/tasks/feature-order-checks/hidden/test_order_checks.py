from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from stockroom.checks import Issue, check_orders
from stockroom.models import Item, Order, OrderLine

ROOT = Path(__file__).resolve().parents[3]

ITEMS = [
    Item("MUG-001", "Blue Mug", 1250, ("kitchen",), 5),
    Item("TEA-010", "Green Tea", 895, ("food",), 10),
    Item("PEN-100", "Pen", 3400, (), 2),
]


def line(sku: str, quantity: int = 1, price: int | None = None) -> OrderLine:
    known = {item.sku: item.price_cents for item in ITEMS}
    return OrderLine(sku, quantity, known.get(sku, 100) if price is None else price)


def order(
    order_id: str,
    *lines: OrderLine,
    status: str = "open",
    customer: str = "Hana Kim",
) -> Order:
    return Order(order_id, customer, date(2024, 3, 1), tuple(lines), status)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_clean_orders_and_input_handling() -> None:
    orders = [order("A-1", line("MUG-001", 2), line("TEA-010")), order("A-2", line("PEN-100"))]
    assert check_orders(orders, ITEMS) == []
    assert check_orders([], ITEMS) == []
    assert check_orders(iter(orders), iter(ITEMS)) == []
    assert check_orders((o for o in orders), (i for i in ITEMS)) == []
    bad = [order("A-3", line("ZZZ-999"))]
    issue = check_orders(bad, ITEMS)[0]
    assert dataclasses.is_dataclass(issue) and isinstance(issue, Issue)
    assert dataclasses.astuple(issue) == ("A-3", 1, "unknown_sku")
    with pytest.raises(dataclasses.FrozenInstanceError):
        issue.line = 5  # type: ignore[misc]
    assert len(bad) == 1 and bad[0].lines == (line("ZZZ-999"),)


def test_unknown_sku() -> None:
    orders = [order("A-1", line("MUG-001"), line("ZZZ-999"), line("ABC-123"), line("TEA-010"))]
    assert check_orders(orders, ITEMS) == [
        Issue("A-1", 2, "unknown_sku"),
        Issue("A-1", 3, "unknown_sku"),
    ]
    assert check_orders(orders, []) == [
        Issue("A-1", n, "unknown_sku") for n in (1, 2, 3, 4)
    ]


def test_price_mismatch() -> None:
    orders = [
        order("A-1", line("MUG-001", 1, 1249), line("TEA-010", 1, 895), line("PEN-100", 3, 3401)),
        order("A-2", line("MUG-001", 1, 0), status="paid"),
        order("A-3", line("MUG-001", 1, 1250), status="shipped"),
    ]
    assert check_orders(orders, ITEMS) == [
        Issue("A-1", 1, "price_mismatch"),
        Issue("A-1", 3, "price_mismatch"),
        Issue("A-2", 1, "price_mismatch"),
    ]


def test_price_mismatch_skipped_for_cancelled_and_unknown() -> None:
    orders = [
        order("A-1", line("MUG-001", 1, 999), line("ZZZ-999", 1, 999), status="cancelled"),
        order("A-2", line("ZZZ-999", 1, 999)),
    ]
    assert check_orders(orders, ITEMS) == [
        Issue("A-1", 2, "unknown_sku"),
        Issue("A-2", 1, "unknown_sku"),
    ]


def test_duplicate_sku() -> None:
    orders = [
        order("A-1", line("MUG-001"), line("TEA-010"), line("MUG-001"), line("MUG-001")),
        order("A-2", line("MUG-001"), line("TEA-010")),
    ]
    assert check_orders(orders, ITEMS) == [
        Issue("A-1", 3, "duplicate_sku"),
        Issue("A-1", 4, "duplicate_sku"),
    ]


def test_duplicate_sku_any_status_and_unknown() -> None:
    orders = [
        order("A-1", line("MUG-001"), line("MUG-001", 2, 5), status="cancelled"),
        order("A-2", line("ZZZ-999"), line("ZZZ-999")),
    ]
    assert check_orders(orders, ITEMS) == [
        Issue("A-1", 2, "duplicate_sku"),
        Issue("A-2", 1, "unknown_sku"),
        Issue("A-2", 2, "duplicate_sku"),
        Issue("A-2", 2, "unknown_sku"),
    ]


def test_order_level_issues() -> None:
    orders = [
        order("A-1", customer=" \t "),
        order("A-2", line("MUG-001"), customer=""),
        order("A-3", line("MUG-001"), customer="Omar Diaz"),
    ]
    assert check_orders(orders, ITEMS) == [
        Issue("A-1", 0, "blank_customer"),
        Issue("A-1", 0, "no_lines"),
        Issue("A-2", 0, "blank_customer"),
    ]


def test_duplicate_order_still_checks_lines() -> None:
    orders = [
        order("A-1", line("MUG-001")),
        order("A-2", line("TEA-010")),
        order("A-1", line("ZZZ-999")),
        order("A-1"),
    ]
    assert check_orders(orders, ITEMS) == [
        Issue("A-1", 0, "duplicate_order"),
        Issue("A-1", 1, "unknown_sku"),
        Issue("A-1", 0, "duplicate_order"),
        Issue("A-1", 0, "no_lines"),
    ]


def test_issue_ordering() -> None:
    orders = [
        order("B-2", line("ZZZ-999"), customer=""),
        order("B-1", line("MUG-001", 1, 1), line("MUG-001", 1, 1), line("TEA-010", 1, 2)),
        order("B-2"),
    ]
    assert check_orders(orders, ITEMS) == [
        Issue("B-2", 0, "blank_customer"),
        Issue("B-2", 1, "unknown_sku"),
        Issue("B-1", 1, "price_mismatch"),
        Issue("B-1", 2, "duplicate_sku"),
        Issue("B-1", 2, "price_mismatch"),
        Issue("B-1", 3, "price_mismatch"),
        Issue("B-2", 0, "duplicate_order"),
        Issue("B-2", 0, "no_lines"),
    ]


def test_cli_sample_data_is_ok() -> None:
    done = run("check", "--items", "data/items.csv", "--orders", "data/orders.csv")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


def test_cli_reports_issues(tmp_path: Path) -> None:
    items = tmp_path / "items.csv"
    items.write_text(
        "sku,name,price,tags,reorder_level\n"
        "MUG-001,Blue Mug,12.50,kitchen,5\n"
        "TEA-010,Green Tea,8.95,food,10\n"
    )
    orders = tmp_path / "orders.csv"
    orders.write_text(
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        "A-1,Hana Kim,2024-03-01,paid,MUG-001,1,12.50\n"
        "A-1,Hana Kim,2024-03-01,paid,TEA-010,2,9.00\n"
        "A-2,Omar Diaz,2024-03-02,open,ZZZ-999,1,1.00\n"
        "A-2,Omar Diaz,2024-03-02,open,ZZZ-999,1,1.00\n"
        "A-3,Lena Berg,2024-03-03,cancelled,MUG-001,1,1.00\n"
    )
    done = run("check", "--items", str(items), "--orders", str(orders))
    assert done.returncode == 1, done.stderr
    assert done.stdout.splitlines() == [
        "A-1:2: price_mismatch",
        "A-2:1: unknown_sku",
        "A-2:2: duplicate_sku",
        "A-2:2: unknown_sku",
    ]
