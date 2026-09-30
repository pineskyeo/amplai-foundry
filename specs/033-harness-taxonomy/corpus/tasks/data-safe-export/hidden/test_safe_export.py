from __future__ import annotations

import csv
import io
import os
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from stockroom.csvio import csv_safe, csv_unsafe, read_items, read_orders, write_items, write_orders
from stockroom.models import Item, Order, OrderLine

ROOT = Path(__file__).resolve().parents[3]
DANGEROUS = "=+-@\t\r"


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


def test_cell_guard_and_its_inverse() -> None:
    for cell in ("=SUM(A1)", "+1", "-1", "@home", "\tx", "\rx", "'quoted", "=", "-", "'", "''", "=+-@", "'=x"):
        assert csv_safe(cell) == "'" + cell, cell
    for cell in ("", "plain", "a=b", "a-b", " =x", " -1", "x'y", "1+1", "é=", "日本", "a'"):
        assert csv_safe(cell) == cell, cell
    assert csv_unsafe("'=SUM(A1)") == "=SUM(A1)"
    assert csv_unsafe("''quoted") == "'quoted"
    assert csv_unsafe("'") == ""
    assert csv_unsafe("plain") == "plain"
    assert csv_unsafe("") == ""
    assert csv_unsafe("a'b") == "a'b"
    assert csv_unsafe("=x") == "=x"
    assert csv_unsafe(" 'x") == " 'x"
    rng = random.Random(3)
    alphabet = list("=+-@'\t\r ab1é")
    for _ in range(3000):
        cell = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 7)))
        assert csv_unsafe(csv_safe(cell)) == cell, repr(cell)
        assert csv_safe(cell)[:1] not in tuple(DANGEROUS), repr(cell)
        if cell[:1] in ("'", *DANGEROUS):
            assert csv_safe(cell) == "'" + cell
        else:
            assert csv_safe(cell) == cell


def test_items_writer_guards_name_and_tags_only() -> None:
    items = [
        Item("MUG-001", "=SUM(A1)", 1250, ("-x", "a"), 5),
        Item("TEA-010", "@home", 895, (), 0),
        Item("PEN-100", "+1 mug", 300, ("a", "=b"), 0),
        Item("NB-200", "'quoted", 625, ("'t",), 0),
        Item("LMP-030", "-", 4999, ("=x",), 2),
        Item("CND-007", "plain", 980, ("a", "b"), 1),
    ]
    text = write_items(items, safe=True)
    assert text.splitlines() == [
        "sku,name,price,tags,reorder_level",
        "MUG-001,'=SUM(A1),12.50,'-x;a,5",
        "TEA-010,'@home,8.95,,0",
        "PEN-100,'+1 mug,3.00,a;=b,0",
        "NB-200,''quoted,6.25,''t,0",
        "LMP-030,'-,49.99,'=x,2",
        "CND-007,plain,9.80,a;b,1",
    ]
    assert write_items(items) == write_items(items, safe=False)
    assert write_items(items).splitlines()[1] == "MUG-001,=SUM(A1),12.50,-x;a,5"
    assert read_items(text, safe=True) == items
    plain = read_items(text)
    assert [i.name for i in plain] == ["'=SUM(A1)", "'@home", "'+1 mug", "''quoted", "'-", "plain"]
    assert [i.tags for i in plain] == [("'-x", "a"), (), ("a", "=b"), ("''t",), ("'=x",), ("a", "b")]
    assert [i.sku for i in plain] == [i.sku for i in items]
    assert [i.price_cents for i in plain] == [i.price_cents for i in items]


def test_orders_writer_guards_order_id_and_customer() -> None:
    orders = [
        Order("=1", "-Bob", date(2024, 1, 8), (OrderLine("MUG-001", 2, 1250),), "paid"),
        Order("A-2", "@Ann", date(2024, 1, 9), (OrderLine("MUG-001", 1, 1250), OrderLine("TEA-010", 3, 895))),
        Order("+3", "'Cy, Jr", date(2024, 1, 10), (OrderLine("PEN-100", 1, 300),), "shipped"),
        Order("A-4", "Dee", date(2024, 1, 11), (OrderLine("PEN-100", 1, 300),)),
    ]
    text = write_orders(orders, safe=True)
    assert text.splitlines() == [
        "order_id,customer,placed,status,sku,quantity,unit_price",
        "'=1,'-Bob,2024-01-08,paid,MUG-001,2,12.50",
        "A-2,'@Ann,2024-01-09,open,MUG-001,1,12.50",
        "A-2,'@Ann,2024-01-09,open,TEA-010,3,8.95",
        "'+3,\"''Cy, Jr\",2024-01-10,shipped,PEN-100,1,3.00",
        "A-4,Dee,2024-01-11,open,PEN-100,1,3.00",
    ]
    assert write_orders(orders) == write_orders(orders, safe=False)
    assert read_orders(text, safe=True) == orders
    plain = read_orders(text)
    assert [o.order_id for o in plain] == ["'=1", "A-2", "'+3", "A-4"]
    assert [o.customer for o in plain] == ["'-Bob", "'@Ann", "''Cy, Jr", "Dee"]
    assert read_orders(write_orders(orders)) == orders


def _text(rng: random.Random, pool: str) -> str:
    while True:
        value = "".join(rng.choice(pool) for _ in range(rng.randrange(1, 9))).strip()
        if value:
            return value


def test_generated_files_round_trip_and_never_start_with_a_formula() -> None:
    rng = random.Random(20261001)
    name_pool = "=+-@'abc XYZ019é日-_."
    for _ in range(400):
        items = [
            Item(
                f"GEN-{100 + n}",
                _text(rng, name_pool),
                rng.randrange(0, 99999),
                tuple(_text(rng, "=+-@'abc019").lower() for _ in range(rng.randrange(0, 3))),
                rng.randrange(0, 9),
            )
            for n in range(rng.randrange(1, 7))
        ]
        items = [
            Item(i.sku, i.name, i.price_cents, tuple(t for t in i.tags if t.strip() == t and t), i.reorder_level)
            for i in items
        ]
        text = write_items(items, safe=True)
        assert read_items(text, safe=True) == items, text
        rows = list(csv.reader(io.StringIO(text)))[1:]
        for row in rows:
            assert row[1][:1] not in tuple(DANGEROUS) and row[3][:1] not in tuple(DANGEROUS), row
            assert row[0][:1] not in tuple(DANGEROUS) and row[2][:1] not in tuple(DANGEROUS)
        for item, row in zip(items, rows, strict=True):
            assert row[1] == ("'" + item.name if item.name[:1] in ("'", *DANGEROUS) else item.name)
        orders = [
            Order(
                f"{_text(rng, name_pool)}-{n}" if rng.random() < 0.5 else _text(rng, "=+-@'a1"),
                _text(rng, name_pool + ',"'),
                date(2024, 1, 1) + timedelta(days=rng.randrange(0, 300)),
                (OrderLine("MUG-001", rng.randrange(1, 9), rng.randrange(0, 99999)),),
                rng.choice(["open", "paid", "shipped", "cancelled"]),
            )
            for n in range(rng.randrange(1, 6))
        ]
        ids = {o.order_id for o in orders}
        if len(ids) != len(orders):
            continue
        text = write_orders(orders, safe=True)
        assert read_orders(text, safe=True) == orders, text
        for row in list(csv.reader(io.StringIO(text)))[1:]:
            assert row[0][:1] not in tuple(DANGEROUS) and row[1][:1] not in tuple(DANGEROUS), row


def test_safe_reading_changes_nothing_else() -> None:
    plain = "sku,name,price,tags,reorder_level\nMUG-001,Blue Mug,12.50,kitchen;gift,5\nNB-200,Notebook,6.25,,0\n"
    assert read_items(plain, safe=True) == read_items(plain)
    quoted = "sku,name,price,tags,reorder_level\nMUG-001, 'Blue Mug ,12.50, kitchen,5\n"
    assert read_items(quoted, safe=True)[0].name == "'Blue Mug"
    assert read_items(quoted, safe=True)[0].tags == ("kitchen",)
    orders = "order_id,customer,placed,status,sku,quantity,unit_price\nA-1,Hana Kim,2024-01-08,paid,MUG-001,2,12.50\n"
    assert read_orders(orders, safe=True) == read_orders(orders)
    assert read_items("sku,name,price,tags,reorder_level\nMUG-001,'',1.00,,0\n", safe=True)[0].name == "'"


def test_export_command_and_safe_listing(tmp_path: Path) -> None:
    source = tmp_path / "items.csv"
    source.write_text(
        "sku,name,price,tags,reorder_level\n"
        "MUG-001,=SUM,12.50,-x;a,5\n"
        "TEA-010,@home,8.95,,0\n"
        "PEN-100,'quoted,3.00,'t,0\n"
        "NB-200,plain,6.25,=b,0\n"
    )
    out = tmp_path / "safe.csv"
    done = run("export", "--items", str(source), "--out", str(out))
    assert done.returncode == 0, done.stderr
    assert done.stdout == "exported 4 items\n"
    assert out.read_text().splitlines() == [
        "sku,name,price,tags,reorder_level",
        "MUG-001,'=SUM,12.50,'-x;a,5",
        "TEA-010,'@home,8.95,,0",
        "PEN-100,''quoted,3.00,''t,0",
        "NB-200,plain,6.25,'=b,0",
    ]
    listed = run("items", "--file", str(out), "--safe")
    assert listed.returncode == 0, listed.stderr
    assert [line.split()[:2] for line in listed.stdout.splitlines()[2:]] == [
        ["MUG-001", "=SUM"],
        ["TEA-010", "@home"],
        ["PEN-100", "'quoted"],
        ["NB-200", "plain"],
    ]
    raw = run("items", "--file", str(out))
    assert [line.split()[1] for line in raw.stdout.splitlines()[2:]] == ["'=SUM", "'@home", "''quoted", "plain"]
    tagged = run("items", "--file", str(out), "--safe", "--tag=-x")
    assert [line.split()[0] for line in tagged.stdout.splitlines()[2:]] == ["MUG-001"]
    tagged = run("items", "--file", str(out), "--safe", "--tag", "'t")
    assert [line.split()[0] for line in tagged.stdout.splitlines()[2:]] == ["PEN-100"]
    single = tmp_path / "one.csv"
    single.write_text("sku,name,price,tags,reorder_level\nMUG-001,Mug,1.00,,0\n")
    done = run("export", "--items", str(single), "--out", str(tmp_path / "one-out.csv"))
    assert done.stdout == "exported 1 item\n"
    bad = tmp_path / "bad.csv"
    bad.write_text("sku,name\nMUG-001,Mug\n")
    done = run("export", "--items", str(bad), "--out", str(tmp_path / "bad-out.csv"))
    assert done.returncode == 1 and done.stdout == "" and done.stderr.startswith("stockroom: error:")
    assert not (tmp_path / "bad-out.csv").exists()
