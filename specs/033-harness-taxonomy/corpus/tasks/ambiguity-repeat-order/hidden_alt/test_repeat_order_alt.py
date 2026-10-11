import os
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from stockroom.errors import ParseError
from stockroom.models import Item, Order, OrderLine
from stockroom.reorder import repeat_order

ROOT = Path(__file__).resolve().parents[3]
HEADER = "order_id,customer,placed,status,sku,quantity,unit_price\n"
ORDERS = (
    HEADER
    + "R-1,Hana Kim,2024-01-08,shipped,MUG-001,2,11.00\n"
    + "R-1,Hana Kim,2024-01-08,shipped,TEA-010,1,8.95\n"
    + "R-2,Omar Diaz,2024-01-19,cancelled,PEN-100,1,34.00\n"
    + "R-3,Lena Berg,2024-02-03,paid,LMP-030,1,49.99\n"
    + "R-4,Ari Moss,2024-02-10,open,NB-200,3,6.25\n"
)
ITEMS = (
    "sku,name,price,tags,reorder_level\n"
    "MUG-001,Blue Mug,12.50,kitchen;gift,5\n"
    "TEA-010,Green Tea 100g,8.95,food;tea,10\n"
    "PEN-100,Fountain Pen,30.00,office,2\n"
    "NB-200,Notebook A5,6.25,office;paper,20\n"
)
CATALOGUE = [
    Item("MUG-001", "Blue Mug", 1250),
    Item("TEA-010", "Green Tea 100g", 895),
    Item("PEN-100", "Fountain Pen", 3000),
    Item("NB-200", "Notebook A5", 625),
]
FIRST = Order(
    "R-1",
    "Hana Kim",
    date(2024, 1, 8),
    (OrderLine("MUG-001", 2, 1100), OrderLine("TEA-010", 1, 895)),
    "shipped",
)


def run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "stockroom",
            "repeat",
            "--orders",
            "orders.csv",
            "--items",
            "items.csv",
            *args,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def setup_files(tmp_path: Path) -> None:
    (tmp_path / "orders.csv").write_text(ORDERS, encoding="utf-8")
    (tmp_path / "items.csv").write_text(ITEMS, encoding="utf-8")


def cells(text: str) -> list[list[str]]:
    return [line.split(",") for line in text.splitlines()]


def test_repeat_keeps_customer_lines_and_opens_the_order_alt() -> None:
    new = repeat_order(FIRST, CATALOGUE, order_id="R-9", placed=date(2024, 3, 1))
    assert (new.order_id, new.customer, new.placed, new.status) == (
        "R-9",
        "Hana Kim",
        date(2024, 3, 1),
        "open",
    )
    assert [(line.sku, line.quantity) for line in new.lines] == [("MUG-001", 2), ("TEA-010", 1)]
    same_day = repeat_order(FIRST, CATALOGUE, order_id="R-10", placed=date(2024, 1, 8))
    assert same_day.placed == date(2024, 1, 8)
    paid = Order("P-1", "Ari Moss", date(2024, 2, 10), (OrderLine("NB-200", 3, 625),), "paid")
    assert repeat_order(paid, CATALOGUE, order_id="P-2", placed=date(2024, 2, 11)).status == "open"


def test_repeat_refuses_cancelled_early_and_unsold_orders_alt() -> None:
    cancelled = Order(
        "C-1", "Omar Diaz", date(2024, 1, 19), (OrderLine("LMP-030", 1, 4999),), "cancelled"
    )
    with pytest.raises(ParseError) as caught:
        repeat_order(cancelled, CATALOGUE, order_id="C-2", placed=date(2023, 1, 1))
    assert str(caught.value) == "order C-1 is cancelled"
    with pytest.raises(ParseError) as caught:
        repeat_order(FIRST, [], order_id="R-9", placed=date(2024, 1, 7))
    assert str(caught.value) == "repeat of R-1 cannot be placed before 2024-01-08"
    mixed = Order(
        "M-1",
        "Lena Berg",
        date(2024, 2, 3),
        (
            OrderLine("MUG-001", 1, 1250),
            OrderLine("LMP-030", 1, 4999),
            OrderLine("CND-007", 2, 980),
        ),
    )
    with pytest.raises(ParseError) as caught:
        repeat_order(mixed, CATALOGUE, order_id="M-2", placed=date(2024, 2, 3))
    assert str(caught.value) == "LMP-030 is no longer sold"


def test_repeat_command_prints_the_new_order_rows_alt(tmp_path: Path) -> None:
    setup_files(tmp_path)
    done = run(tmp_path, "--order-id", "R-1", "--new-id", "R-9", "--placed", "2024-03-01")
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    rows = cells(done.stdout)
    assert rows[0] == HEADER.strip().split(",")
    assert [row[:6] for row in rows[1:]] == [
        ["R-9", "Hana Kim", "2024-03-01", "open", "MUG-001", "2"],
        ["R-9", "Hana Kim", "2024-03-01", "open", "TEA-010", "1"],
    ]
    assert (tmp_path / "orders.csv").read_text(encoding="utf-8") == ORDERS


def test_repeat_command_write_adds_the_order_to_the_file_alt(tmp_path: Path) -> None:
    setup_files(tmp_path)
    done = run(
        tmp_path, "--order-id", "R-4", "--new-id", "R-10", "--placed", "2024-04-01", "--write"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "added R-10 for Ari Moss: 1 line\n"
    text = (tmp_path / "orders.csv").read_text(encoding="utf-8")
    assert text.startswith(ORDERS)
    assert [row[:6] for row in cells(text[len(ORDERS) :])] == [
        ["R-10", "Ari Moss", "2024-04-01", "open", "NB-200", "3"],
    ]
    done = run(
        tmp_path, "--order-id", "R-1", "--new-id", "R-11", "--placed", "2024-04-02", "--write"
    )
    assert done.stdout == "added R-11 for Hana Kim: 2 lines\n"
    assert len(cells((tmp_path / "orders.csv").read_text(encoding="utf-8"))) == 1 + 5 + 1 + 2


def test_repeat_command_refuses_bad_requests_alt(tmp_path: Path) -> None:
    setup_files(tmp_path)
    for args, message in (
        (("--order-id", "R-7", "--new-id", "R-9", "--placed", "2024-03-01"), "no order R-7"),
        (
            ("--order-id", "R-2", "--new-id", "R-9", "--placed", "2024-03-01"),
            "order R-2 is cancelled",
        ),
        (
            ("--order-id", "R-3", "--new-id", "R-9", "--placed", "2024-03-01"),
            "LMP-030 is no longer sold",
        ),
        (
            ("--order-id", "R-1", "--new-id", "R-4", "--placed", "2024-03-01", "--write"),
            "duplicate order id R-4",
        ),
        (
            ("--order-id", "R-1", "--new-id", "R-9", "--placed", "2024-01-07"),
            "repeat of R-1 cannot be placed before 2024-01-08",
        ),
        (
            ("--order-id", "R-1", "--new-id", "R-9", "--placed", "2024-02-30"),
            "not a date: '2024-02-30'",
        ),
    ):
        done = run(tmp_path, *args)
        assert done.returncode == 1
        assert done.stdout == ""
        assert done.stderr == f"stockroom: error: {message}\n"
        assert (tmp_path / "orders.csv").read_text(encoding="utf-8") == ORDERS


def test_repeat_lines_keep_the_price_they_were_ordered_at(tmp_path: Path) -> None:
    new = repeat_order(FIRST, CATALOGUE, order_id="R-9", placed=date(2024, 3, 1))
    assert new.lines == (OrderLine("MUG-001", 2, 1100), OrderLine("TEA-010", 1, 895))
    rng = random.Random(6161)
    skus = ["MUG-001", "TEA-010", "PEN-100", "NB-200"]
    for _ in range(300):
        catalogue = [Item(sku, "Thing", rng.randrange(0, 9000)) for sku in skus]
        lines = tuple(
            OrderLine(rng.choice(skus), rng.randrange(1, 9), rng.randrange(0, 9000))
            for _ in range(rng.randrange(1, 5))
        )
        old = Order(
            "X-1", "Kai Lund", date(2024, 1, 1), lines, rng.choice(["open", "paid", "shipped"])
        )
        new = repeat_order(old, catalogue, order_id="X-2", placed=date(2024, 1, 2))
        assert new.lines == lines
    setup_files(tmp_path)
    done = run(tmp_path, "--order-id", "R-1", "--new-id", "R-9", "--placed", "2024-03-01")
    assert done.stdout == (
        HEADER
        + "R-9,Hana Kim,2024-03-01,open,MUG-001,2,11.00\n"
        + "R-9,Hana Kim,2024-03-01,open,TEA-010,1,8.95\n"
    )
