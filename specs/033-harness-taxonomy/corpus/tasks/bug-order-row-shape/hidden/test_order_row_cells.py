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


def sample_lines() -> list[str]:
    return (DATA / "orders.csv").read_text(encoding="utf-8").splitlines(keepends=True)


def test_sales_reports_a_short_row_as_a_data_error(tmp_path: Path) -> None:
    lines = sample_lines()
    cases = [
        ([*lines[:8], "A-1006,Omar Diaz,2024-03-21\n"], 9),
        ([*lines[:8], "A-1006,Omar Diaz,2024-03-21,open,MUG-002,1"], 9),
        ([*lines[:3], "A-1002\n", *lines[3:]], 4),
        ([*lines[:1], "A-1001,Hana Kim,2024-01-08,shipped,MUG-001,2\n", *lines[2:]], 2),
    ]
    for text_lines, number in cases:
        path = tmp_path / "orders.csv"
        path.write_text("".join(text_lines), encoding="utf-8")
        done = run("sales", "--orders", str(path))
        assert done.returncode == 1, (done.returncode, done.stderr)
        assert done.stdout == ""
        assert done.stderr == f"stockroom: error: line {number}: expected 7 fields\n"


def test_reading_a_short_row_names_its_line() -> None:
    cases = [
        (HEADER + "A-1,Hana Kim,2024-01-08,paid,MUG-001,2\n", 2),
        (HEADER + "A-1,Hana Kim,2024-01-08,paid,MUG-001,2,1.00\nA-1\n", 3),
        (HEADER + "A-1,Hana Kim,2024-01-08,paid,MUG-001,2,1.00\nA-2,Omar,2024-01-09\n", 3),
        (HEADER + 'A-1,"Kim, Hana",2024-01-08,paid,MUG-001,2\n', 2),
        (HEADER + "A-1,Hana Kim,2024-01-08,paid,MUG-001,1,2.00\nA-2,Omar,2024-01-09,paid\n", 3),
        (HEADER + "A-1\n", 2),
    ]
    for text, number in cases:
        with pytest.raises(ParseError) as caught:
            read_orders(text)
        assert str(caught.value) == f"line {number}: expected 7 fields"


def test_a_row_with_extra_cells_is_refused(tmp_path: Path) -> None:
    cases = [
        (HEADER + "A-1,Hana Kim,2024-01-08,paid,MUG-001,2,1.00,\n", 2),
        (
            HEADER
            + "A-1,Hana Kim,2024-01-08,paid,MUG-001,2,1.00\n"
            + "A-2,O,2024-01-09,paid,MUG-001,1,1.00,x,y\n",
            3,
        ),
        (HEADER + 'A-1,"Kim, Hana",2024-01-08,paid,MUG-001,2,1.00,"a,b"\n', 2),
    ]
    for text, number in cases:
        with pytest.raises(ParseError) as caught:
            read_orders(text)
        assert str(caught.value) == f"line {number}: expected 7 fields"
    path = tmp_path / "orders.csv"
    path.write_text(
        "".join(sample_lines()[:3]) + "A-1002,Omar Diaz,2024-01-19,paid,PEN-100,1,34.00,9\n"
    )
    done = run("sales", "--orders", str(path))
    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == "stockroom: error: line 4: expected 7 fields\n"


def test_lines_without_content_are_skipped(tmp_path: Path) -> None:
    lines = sample_lines()
    padded = [*lines[:2], "   \n", *lines[2:5], " , ,  ,\n", "\t\n", *lines[5:], ",,,,,,\n", "  "]
    text = "".join(padded)
    assert read_orders(text) == read_orders("".join(lines))
    path = tmp_path / "orders.csv"
    path.write_text(text, encoding="utf-8")
    done = run("sales", "--orders", str(path))
    original = run("sales", "--orders", "data/orders.csv")
    assert done.returncode == 0, done.stderr
    assert done.stdout == original.stdout and done.stderr == ""
    assert read_orders(HEADER + "   \n") == []
    assert read_orders(HEADER + ",,,,,,,,,\n") == []


def _write(rows: list[list[str]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(HEADER.strip().split(","))
    writer.writerows(rows)
    return out.getvalue()


def test_generated_files_with_one_malformed_row() -> None:
    rng = random.Random(1717)
    customers = ["Hana Kim", "Kim, Hana", 'Omar "O" Diaz', "Lena", "Ravi, Jr."]
    statuses = ["open", "paid", "shipped", "cancelled"]
    for _ in range(300):
        orders: list[Order] = []
        rows: list[list[str]] = []
        for n in range(rng.randint(1, 6)):
            customer = rng.choice(customers)
            placed = date(2024, rng.randint(1, 12), rng.randint(1, 28))
            status = rng.choice(statuses)
            lines = []
            for k in range(rng.randint(1, 3)):
                line = OrderLine(f"SKU-{100 + k}", rng.randint(1, 9), rng.randint(0, 99999))
                lines.append(line)
                cents = line.unit_price_cents
                price = f"{cents // 100}.{cents % 100:02d}"
                rows.append(
                    [
                        f"B-{n}",
                        customer,
                        placed.isoformat(),
                        status,
                        line.sku,
                        str(line.quantity),
                        price,
                    ]
                )
            orders.append(Order(f"B-{n}", customer, placed, tuple(lines), status))
        assert read_orders(_write(rows)) == orders
        bad = rng.randrange(len(rows))
        if rng.random() < 0.5:
            rows[bad] = rows[bad][: rng.randint(1, 6)]
        else:
            rows[bad] = rows[bad] + [
                rng.choice(["", "x", "1.00"]) for _ in range(rng.randint(1, 3))
            ]
        with pytest.raises(ParseError) as caught:
            read_orders(_write(rows))
        assert str(caught.value) == f"line {bad + 2}: expected 7 fields"
