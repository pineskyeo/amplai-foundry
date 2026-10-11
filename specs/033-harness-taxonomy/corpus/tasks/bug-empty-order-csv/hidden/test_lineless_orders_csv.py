import os
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from stockroom.csvio import read_orders, write_orders
from stockroom.errors import ParseError
from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"
HEADER = "order_id,customer,placed,status,sku,quantity,unit_price\n"


@pytest.fixture(autouse=True)
def _clean_stockroom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("STOCKROOM_"):
            monkeypatch.delenv(name)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_saved_orders_without_lines_still_count_in_sales(tmp_path: Path) -> None:
    orders = read_orders((DATA / "orders.csv").read_text(encoding="utf-8"))
    orders.insert(2, Order("Q-1", "Ada Moss", date(2024, 1, 20), (), "paid"))
    orders.append(Order("Q-2", "Ravi Nair", date(2024, 3, 22), (), "cancelled"))
    path = tmp_path / "orders.csv"
    path.write_text(write_orders(orders), encoding="utf-8")
    assert read_orders(path.read_text(encoding="utf-8")) == orders
    done = run("sales", "--orders", str(path))
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[:2] == ["6 orders, 2 cancelled", "Revenue: $149.65"]
    assert len(OrderBook(read_orders(path.read_text(encoding="utf-8")))) == 8


def test_an_order_without_lines_is_one_row_with_three_empty_cells() -> None:
    assert write_orders([Order("A-1", "Hana Kim", date(2024, 1, 8))]) == (
        HEADER + "A-1,Hana Kim,2024-01-08,open,,,\n"
    )
    orders = [
        Order("A-1", "Kim, Hana", date(2024, 1, 8), (OrderLine("MUG-001", 2, 125000),), "paid"),
        Order("A-2", 'Omar "O"', date(2024, 1, 9), (), "shipped"),
        Order(
            "A-3", "", date(2024, 1, 10), (OrderLine("MUG-001", 1, 5), OrderLine("NB-200", 3, 0))
        ),
        Order("A-4", "Lena", date(2024, 1, 11), (), "cancelled"),
    ]
    assert write_orders(orders) == (
        HEADER
        + 'A-1,"Kim, Hana",2024-01-08,paid,MUG-001,2,"1,250.00"\n'
        + 'A-2,"Omar ""O""",2024-01-09,shipped,,,\n'
        + "A-3,,2024-01-10,open,MUG-001,1,0.05\n"
        + "A-3,,2024-01-10,open,NB-200,3,0.00\n"
        + "A-4,Lena,2024-01-11,cancelled,,,\n"
    )
    sample = (DATA / "orders.csv").read_text(encoding="utf-8")
    assert write_orders(read_orders(sample)) == sample


def test_hand_written_rows_without_a_line_are_read() -> None:
    text = (
        HEADER
        + "A-1,Hana,2024-01-08,paid,MUG-001,1,1.00\r\n"
        + "B-1, Omar ,2024-01-09, open ,  , ,   \r\n"
        + "C-1,Lena,2024-01-10,shipped,,,\r\n"
        + "A-1,Hana,2024-01-08,paid,MUG-002,2,1.50\r\n"
    )
    assert read_orders(text) == [
        Order(
            "A-1",
            "Hana",
            date(2024, 1, 8),
            (OrderLine("MUG-001", 1, 100), OrderLine("MUG-002", 2, 150)),
            "paid",
        ),
        Order("B-1", "Omar", date(2024, 1, 9), (), "open"),
        Order("C-1", "Lena", date(2024, 1, 10), (), "shipped"),
    ]


def test_mixed_or_partly_empty_rows_are_refused() -> None:
    line = "A-1,Hana,2024-01-08,paid,MUG-001,1,1.00\n"
    empty = "A-1,Hana,2024-01-08,paid,,,\n"
    other = "B-1,Omar,2024-01-09,open,MUG-001,1,1.00\n"
    for body in (
        line + empty,
        empty + line,
        empty + empty,
        empty + other + line,
        line + other + empty,
        empty + other + empty,
        "A-1,Hana,2024-01-08,paid,,1,1.00\n",
        "A-1,Hana,2024-01-08,paid,MUG-001,,1.00\n",
        "A-1,Hana,2024-01-08,paid,MUG-001,1,\n",
        "A-1,Hana,2024-01-08,paid,,,1.00\n",
        "A-1,Hana,2024-01-08,paid,MUG-001,,\n",
        "A-1,Hana,2024-01-08,paid,,1,\n",
    ):
        with pytest.raises(ParseError):
            read_orders(HEADER + body)
    assert len(read_orders(HEADER + empty + other)) == 2


def test_generated_order_lists_survive_the_csv_round_trip() -> None:
    rng = random.Random(5150)
    alphabet = ["a", "Z", "é", "7", " ", ",", '"', "\n", ";", "-"]
    statuses = ["open", "paid", "shipped", "cancelled"]
    for _ in range(250):
        orders = []
        for n in range(rng.randint(0, 6)):
            customer = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8))).strip()
            lines = tuple(
                OrderLine(
                    f"SKU-{rng.randint(100, 999)}",
                    rng.randint(1, 50),
                    rng.choice([0, 5, 99, 100000, rng.randint(0, 10**8)]),
                )
                for _ in range(rng.choice([0, 0, 1, 2, 3]))
            )
            placed = date(rng.randint(1, 9999), rng.randint(1, 12), rng.randint(1, 28))
            orders.append(Order(f"G-{n}", customer, placed, lines, rng.choice(statuses)))
        text = write_orders(orders)
        assert read_orders(text) == orders
        assert write_orders(read_orders(text)) == text
