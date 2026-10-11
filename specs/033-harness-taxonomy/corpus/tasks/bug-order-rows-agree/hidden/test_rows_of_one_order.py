import csv
import io
import os
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from stockroom.csvio import read_orders
from stockroom.errors import ParseError
from stockroom.models import Order, OrderLine

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


def error_at(text: str, number: int) -> None:
    with pytest.raises(ParseError) as caught:
        read_orders(text)
    assert str(caught.value).startswith(f"line {number}: "), str(caught.value)


def sample_lines() -> list[str]:
    return (DATA / "orders.csv").read_text(encoding="utf-8").splitlines(keepends=True)


def test_sales_refuses_an_order_whose_rows_disagree(tmp_path: Path) -> None:
    lines = sample_lines()
    changes = [
        (7, "A-1004,Hana Kim,2024-02-14,cancelled,NB-200,4,6.25\n"),
        (3, "A-1001,Hana Kim,2024-01-09,shipped,TEA-010,1,8.95\n"),
        (7, "A-1004,Hana  Kim,2024-02-14,paid,NB-200,4,6.25\n"),
        (3, "A-1001,Hana Kim,2024-01-08,shipped,TEA-010,1,8.95\n".replace("01-08", "01-32")),
    ]
    for number, row in changes:
        text = lines.copy()
        text[number - 1] = row
        path = tmp_path / "orders.csv"
        path.write_text("".join(text), encoding="utf-8")
        done = run("sales", "--orders", str(path))
        assert done.returncode == 1, done.stdout
        assert done.stdout == ""
        assert "Traceback" not in done.stderr
        assert len(done.stderr.splitlines()) == 1
        assert done.stderr.startswith(f"stockroom: error: line {number}: ")


def test_every_row_needs_a_real_date() -> None:
    first = "A-1,Hana,2024-01-08,paid,MUG-001,1,1.00\n"
    for bad in ("2024-02-30", "soon", "", "2024-13-01"):
        error_at(HEADER + first + f"A-1,Hana,{bad},paid,MUG-002,1,1.00\n", 3)
        error_at(HEADER + f"A-1,Hana,{bad},paid,MUG-002,1,1.00\n" + first, 2)
        error_at(HEADER + first + f"B-2,Omar,{bad},paid,MUG-002,1,1.00\n", 3)
        error_at(
            HEADER
            + first
            + "B-2,Omar,2024-01-09,paid,MUG-001,1,1.00\n"
            + f"A-1,Hana,{bad},paid,MUG-002,1,1.00\n",
            4,
        )


def test_customer_date_and_status_are_compared_as_values() -> None:
    first = "A-1,Hana Kim,2024-01-08,paid,MUG-001,1,1.00\n"
    same = [
        "A-1, Hana Kim ,2024-01-08,paid,MUG-002,2,2.00\n",
        "A-1,Hana Kim, 2024-01-08 ,paid,MUG-002,2,2.00\n",
        "A-1,Hana Kim,2024-01-08,  paid ,MUG-002,2,2.00\n",
        " A-1 ,Hana Kim,2024-01-08,paid,MUG-002,2,2.00\n",
    ]
    for row in same:
        orders = read_orders(HEADER + first + row)
        assert len(orders) == 1 and len(orders[0].lines) == 2
        assert (orders[0].customer, orders[0].placed, orders[0].status) == (
            "Hana Kim",
            date(2024, 1, 8),
            "paid",
        )
    different = [
        "A-1,hana kim,2024-01-08,paid,MUG-002,2,2.00\n",
        "A-1,Hana,2024-01-08,paid,MUG-002,2,2.00\n",
        "A-1,Hana Kim,2024-01-09,paid,MUG-002,2,2.00\n",
        "A-1,Hana Kim,2023-01-08,paid,MUG-002,2,2.00\n",
        "A-1,Hana Kim,2024-01-08,shipped,MUG-002,2,2.00\n",
        "A-1,Hana Kim,2024-01-08,Paid,MUG-002,2,2.00\n",
        "A-1,Hana Kim,2024-01-08,teleported,MUG-002,2,2.00\n",
    ]
    for row in different:
        error_at(HEADER + first + row, 3)
        error_at(HEADER + first + "B-1,Omar,2024-02-01,open,MUG-001,1,1.00\n" + row, 4)


def test_consistent_files_read_as_before() -> None:
    orders = read_orders("".join(sample_lines()))
    assert [o.order_id for o in orders] == [
        "A-1001",
        "A-1002",
        "A-1003",
        "A-1004",
        "A-1005",
        "A-1006",
    ]
    assert [len(o.lines) for o in orders] == [2, 1, 1, 2, 1, 1]
    text = (
        HEADER
        + "A-1,Hana,2024-01-08,paid,MUG-001,1,1.00\n"
        + "B-1,Omar,2024-01-09,open,MUG-001,2,1.00\n"
        + "A-1,Hana,2024-01-08,paid,MUG-002,3,1.00\n"
    )
    assert read_orders(text) == [
        Order(
            "A-1",
            "Hana",
            date(2024, 1, 8),
            (OrderLine("MUG-001", 1, 100), OrderLine("MUG-002", 3, 100)),
            "paid",
        ),
        Order("B-1", "Omar", date(2024, 1, 9), (OrderLine("MUG-001", 2, 100),), "open"),
    ]


def _write(rows: list[list[str]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(HEADER.strip().split(","))
    writer.writerows(rows)
    return out.getvalue()


def test_generated_files_with_one_changed_row() -> None:
    rng = random.Random(606)
    customers = ["Hana Kim", "Kim, Hana", 'Omar "O" Diaz', "Lena", "lena"]
    statuses = ["open", "paid", "shipped", "cancelled"]
    for _ in range(400):
        rows: list[list[str]] = []
        orders: list[Order] = []
        for n in range(rng.randint(1, 5)):
            customer = rng.choice(customers)
            placed = date(rng.randint(2020, 2025), rng.randint(1, 12), rng.randint(1, 28))
            status = rng.choice(statuses)
            lines = tuple(
                OrderLine(f"MUG-{k:03d}", k + 1, 100 * k) for k in range(rng.randint(1, 4))
            )
            orders.append(Order(f"O-{n}", customer, placed, lines, status))
            for line in lines:
                rows.append(
                    [
                        f"O-{n}",
                        customer,
                        placed.isoformat(),
                        status,
                        line.sku,
                        str(line.quantity),
                        f"{line.unit_price_cents // 100}.00",
                    ]
                )
        if rng.random() < 0.5:
            rng.shuffle(rows)
        order_of_first = []
        for row in rows:
            if row[0] not in order_of_first:
                order_of_first.append(row[0])
        expected = sorted(orders, key=lambda o: order_of_first.index(o.order_id))
        expected = [
            Order(
                o.order_id,
                o.customer,
                o.placed,
                tuple(
                    OrderLine(r[4], int(r[5]), int(r[6][:-3]) * 100)
                    for r in rows
                    if r[0] == o.order_id
                ),
                o.status,
            )
            for o in expected
        ]
        assert read_orders(_write(rows)) == expected
        later = [i for i, row in enumerate(rows) if any(r[0] == row[0] for r in rows[:i])]
        if not later:
            continue
        index = rng.choice(later)
        column = rng.choice([1, 2, 3])
        old = rows[index][column]
        if rng.random() < 0.4:
            new = rng.choice([" ", "  "]) + old + rng.choice(["", " "])
            changed = False
        elif column == 1:
            new = rng.choice([c for c in customers if c != old])
            changed = True
        elif column == 2:
            new = date(2026, rng.randint(1, 12), rng.randint(1, 28)).isoformat()
            changed = True
        else:
            new = rng.choice([s for s in statuses if s != old])
            changed = True
        rows[index][column] = new
        if changed:
            error_at(_write(rows), index + 2)
        else:
            assert read_orders(_write(rows)) == expected
