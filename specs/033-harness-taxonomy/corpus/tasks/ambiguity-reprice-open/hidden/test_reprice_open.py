import os
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from stockroom.errors import ParseError
from stockroom.models import Item, Order, OrderLine
from stockroom.reprice import Repricing, reprice_open

ROOT = Path(__file__).resolve().parents[3]
HEADER = "order_id,customer,placed,status,sku,quantity,unit_price\n"
ORDERS = (
    HEADER
    + "O-1,Hana Kim,2024-01-08,open,MUG-001,2,11.00\n"
    + "O-1,Hana Kim,2024-01-08,open,TEA-010,1,8.95\n"
    + "O-1,Hana Kim,2024-01-08,open,LMP-030,1,49.99\n"
    + "O-2,Omar Diaz,2024-01-19,paid,MUG-001,1,11.00\n"
    + "O-3,Lena Berg,2024-02-03,open,PEN-100,1,34.00\n"
    + "O-4,Ari Moss,2024-02-10,cancelled,PEN-100,1,34.00\n"
    + "O-5,Bo Ek,2024-02-11,shipped,NB-200,1,7.00\n"
    + "O-6,Kai Lund,2024-02-12,open,NB-200,3,7.00\n"
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
]


def make(oid: str, status: str, *lines: tuple[str, int, int]) -> Order:
    return Order(
        oid, "Mia Chen", date(2024, 1, 8), tuple(OrderLine(s, q, p) for s, q, p in lines), status
    )


def run(tmp_path: Path, *args: str, items: str = ITEMS) -> subprocess.CompletedProcess[str]:
    (tmp_path / "orders.csv").write_text(ORDERS, encoding="utf-8")
    (tmp_path / "items.csv").write_text(items, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "stockroom",
            "reprice",
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


def test_reprice_moves_only_open_orders_to_catalogue_prices() -> None:
    orders = [
        make("A-1", "open", ("MUG-001", 2, 1100), ("PEN-100", 1, 3400)),
        make("A-2", "paid", ("MUG-001", 1, 1100)),
        make("A-3", "shipped", ("PEN-100", 1, 3400)),
        make("A-4", "cancelled", ("TEA-010", 1, 500)),
    ]
    done = reprice_open(orders, CATALOGUE)
    assert isinstance(done, Repricing)
    assert done.orders == (
        make("A-1", "open", ("MUG-001", 2, 1250), ("PEN-100", 1, 3000)),
        orders[1],
        orders[2],
        orders[3],
    )
    assert reprice_open([], CATALOGUE) == Repricing((), (), ())


def test_reprice_reports_only_lines_whose_price_changed() -> None:
    orders = [
        make("B-1", "open", ("MUG-001", 2, 1100), ("TEA-010", 1, 895), ("PEN-100", 3, 3100)),
        make("B-2", "paid", ("MUG-001", 1, 900)),
        make("B-3", "open", ("TEA-010", 1, 900)),
    ]
    done = reprice_open(orders, CATALOGUE)
    assert done.changes == (
        ("B-1", "MUG-001", 1100, 1250),
        ("B-1", "PEN-100", 3100, 3000),
        ("B-3", "TEA-010", 900, 895),
    )
    assert done.missing == ()
    again = reprice_open(list(done.orders), CATALOGUE)
    assert again.orders == done.orders
    assert again.changes == ()


def test_reprice_keeps_lines_missing_from_the_catalogue() -> None:
    orders = [
        make("C-1", "open", ("LMP-030", 1, 4999), ("MUG-001", 1, 1000), ("NB-200", 2, 700)),
        make("C-2", "paid", ("LMP-030", 1, 4999)),
    ]
    done = reprice_open(orders, CATALOGUE)
    assert done.orders[0] == make(
        "C-1", "open", ("LMP-030", 1, 4999), ("MUG-001", 1, 1250), ("NB-200", 2, 700)
    )
    assert done.missing == (("C-1", "LMP-030"), ("C-1", "NB-200"))
    assert done.changes == (("C-1", "MUG-001", 1000, 1250),)


def test_reprice_refuses_a_catalogue_with_a_repeated_sku() -> None:
    twice = [*CATALOGUE, Item("MUG-001", "Blue Mug", 1300)]
    with pytest.raises(ParseError) as caught:
        reprice_open([make("D-1", "paid", ("PEN-100", 1, 100))], twice)
    assert str(caught.value) == "duplicate sku MUG-001 in catalogue"


def test_reprice_follows_the_rule_on_random_orders() -> None:
    rng = random.Random(8282)
    skus = ["MUG-001", "TEA-010", "PEN-100", "NB-200", "CND-007"]
    statuses = ["open", "paid", "shipped", "cancelled"]
    for _ in range(300):
        catalogue = [
            Item(sku, "Thing", rng.randrange(0, 5000)) for sku in skus[:4] if rng.random() < 0.8
        ]
        prices = {item.sku: item.price_cents for item in catalogue}
        orders = [
            make(
                f"R-{i}",
                rng.choice(statuses),
                *[
                    (rng.choice(skus), rng.randrange(1, 5), rng.randrange(0, 5000))
                    for _ in range(rng.randrange(1, 4))
                ],
            )
            for i in range(rng.randrange(0, 6))
        ]
        done = reprice_open(orders, catalogue)
        changes, missing, expected = [], [], []
        for order in orders:
            if order.status != "open":
                expected.append(order)
                continue
            lines = []
            for line in order.lines:
                price = prices.get(line.sku)
                if price is None:
                    missing.append((order.order_id, line.sku))
                    price = line.unit_price_cents
                elif price != line.unit_price_cents:
                    changes.append((order.order_id, line.sku, line.unit_price_cents, price))
                lines.append(OrderLine(line.sku, line.quantity, price))
            expected.append(
                Order(order.order_id, order.customer, order.placed, tuple(lines), "open")
            )
        assert done == Repricing(tuple(expected), tuple(changes), tuple(missing))


def test_reprice_command_lists_changes_missing_lines_and_a_summary(tmp_path: Path) -> None:
    done = run(tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert done.stdout.splitlines() == [
        "O-1 MUG-001: $11.00 -> $12.50",
        "O-3 PEN-100: $34.00 -> $30.00",
        "O-6 NB-200: $7.00 -> $6.25",
        "O-1 LMP-030: not in catalogue",
        "repriced 3 lines in 3 orders",
    ]
    assert (tmp_path / "orders.csv").read_text(encoding="utf-8") == ORDERS
    one = ITEMS.replace("PEN-100,Fountain Pen,30.00", "PEN-100,Fountain Pen,34.00").replace(
        "NB-200,Notebook A5,6.25", "NB-200,Notebook A5,7.00"
    )
    done = run(tmp_path, items=one)
    assert done.stdout.splitlines()[-1] == "repriced 1 line in 1 order"


def test_reprice_command_write_rewrites_the_order_file(tmp_path: Path) -> None:
    done = run(tmp_path, "--write")
    assert done.returncode == 0, done.stderr
    expected = (
        ORDERS.replace("open,MUG-001,2,11.00", "open,MUG-001,2,12.50")
        .replace("open,PEN-100,1,34.00", "open,PEN-100,1,30.00")
        .replace("open,NB-200,3,7.00", "open,NB-200,3,6.25")
    )
    assert (tmp_path / "orders.csv").read_text(encoding="utf-8") == expected
    twice = run(
        tmp_path,
        "--write",
        items=ITEMS.replace("TEA-010,Green Tea 100g,8.95", "MUG-001,Blue Mug,1.00"),
    )
    assert twice.returncode == 1
    assert twice.stdout == ""
    assert twice.stderr == "stockroom: error: duplicate sku MUG-001 in catalogue\n"
    assert (tmp_path / "orders.csv").read_text(encoding="utf-8") == ORDERS
