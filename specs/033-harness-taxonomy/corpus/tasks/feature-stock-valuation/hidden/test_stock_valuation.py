from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest

from stockroom.inventory import Inventory
from stockroom.models import Item
from stockroom.valuation import ValuationRow, stock_valuation

ROOT = Path(__file__).resolve().parents[3]

ITEMS = [
    Item("MUG-001", "Blue Mug", 1250, ("kitchen", "gift"), 5),
    Item("MUG-002", "Red Mug", 1250, ("kitchen",), 5),
    Item("TEA-010", "Green Tea", 895, ("food", "tea"), 10),
    Item("PEN-100", "Fountain Pen", 3400, ("office", "gift"), 2),
    Item("NB-200", "Notebook", 625, (), 20),
    Item("LMP-030", "Desk Lamp", 4999, ("office", "home"), 1),
]


def stock() -> Inventory:
    # LMP-030 is not in the inventory at all; ZZZ-999 is not in the catalogue
    return Inventory.from_levels(
        [
            ("MUG-001", 14, 2),
            ("MUG-002", 4, 0),
            ("TEA-010", 30, 5),
            ("PEN-100", 3, 2),
            ("NB-200", 40, 4),
            ("ZZZ-999", 9, 0),
        ]
    )


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_row_dataclass_and_units() -> None:
    assert [f.name for f in dataclasses.fields(ValuationRow)] == [
        "key",
        "units",
        "value_cents",
        "share_bp",
    ]
    rows, total = stock_valuation(ITEMS, stock(), by="sku")
    row = rows[0]
    assert row == ValuationRow("TEA-010", 30, 26850, 3176)  # on hand 30, not available 25
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.units = 1  # type: ignore[misc]
    assert isinstance(rows, list) and isinstance(total, ValuationRow)
    assert all(r.key != "ZZZ-999" for r in rows)
    assert total.units == 91 and total.value_cents == 84550


def test_by_sku() -> None:
    rows, total = stock_valuation(ITEMS, stock(), by="sku")
    assert rows == [
        ValuationRow("TEA-010", 30, 26850, 3176),
        ValuationRow("NB-200", 40, 25000, 2957),
        ValuationRow("MUG-001", 14, 17500, 2070),
        ValuationRow("PEN-100", 3, 10200, 1206),
        ValuationRow("MUG-002", 4, 5000, 591),
        ValuationRow("LMP-030", 0, 0, 0),
    ]
    assert total == ValuationRow("TOTAL", 91, 84550, 10000)


def test_invalid_grouping() -> None:
    for bad in ("SKU", "tags", "", "item"):
        with pytest.raises(ValueError):
            stock_valuation(ITEMS, stock(), by=bad)
    with pytest.raises(ValueError):
        stock_valuation([], Inventory(), by="name")


def test_by_tag_counts_items_in_each_tag() -> None:
    rows, total = stock_valuation(ITEMS, stock())
    assert rows == [
        ValuationRow("gift", 17, 27700, 3276),
        ValuationRow("food", 30, 26850, 3176),
        ValuationRow("tea", 30, 26850, 3176),
        ValuationRow("(untagged)", 40, 25000, 2957),
        ValuationRow("kitchen", 18, 22500, 2661),
        ValuationRow("office", 3, 10200, 1206),
        ValuationRow("home", 0, 0, 0),
    ]
    assert total == ValuationRow("TOTAL", 91, 84550, 10000)
    assert stock_valuation(ITEMS, stock(), by="tag") == (rows, total)


def test_repeated_tag_and_untagged() -> None:
    items = [
        Item("MUG-001", "Mug", 100, ("a", "a", "b"), 0),
        Item("MUG-002", "Mug", 300, (), 0),
        Item("MUG-003", "Mug", 200, (), 0),
    ]
    inv = Inventory.from_levels([("MUG-001", 2, 0), ("MUG-002", 1, 0), ("MUG-003", 1, 1)])
    rows, total = stock_valuation(items, inv)
    assert rows == [
        ValuationRow("(untagged)", 2, 500, 7143),
        ValuationRow("a", 2, 200, 2857),
        ValuationRow("b", 2, 200, 2857),
    ]
    assert total == ValuationRow("TOTAL", 4, 700, 10000)


def test_sorting_and_total_counts_items_once() -> None:
    items = [Item("MUG-001", "Mug", 100, ("x", "y", "z"), 0), Item("MUG-002", "Mug", 100, ("z",), 0)]
    inv = Inventory.from_levels([("MUG-001", 1, 0), ("MUG-002", 1, 0)])
    rows, total = stock_valuation(items, inv)
    assert [(r.key, r.value_cents) for r in rows] == [("z", 200), ("x", 100), ("y", 100)]
    assert [r.share_bp for r in rows] == [10000, 5000, 5000]
    assert sum(r.value_cents for r in rows) == 400 and total.value_cents == 200
    by_sku, _ = stock_valuation(items, inv, by="sku")
    assert [r.key for r in by_sku] == ["MUG-001", "MUG-002"]  # equal values: key ascending
    many = [Item(f"MUG-00{n}", "Mug", 100, (), 0) for n in (3, 1, 2)]
    full = Inventory.from_levels([(f"MUG-00{n}", 1, 0) for n in (1, 2, 3)])
    assert [r.key for r in stock_valuation(many, full, by="sku")[0]] == ["MUG-001", "MUG-002", "MUG-003"]


def test_share_rounding_half_up() -> None:
    items = [Item("MUG-001", "Mug", 1, (), 0), Item("MUG-002", "Mug", 19999, (), 0)]
    inv = Inventory.from_levels([("MUG-001", 1, 0), ("MUG-002", 1, 0)])
    rows, total = stock_valuation(items, inv, by="sku")
    # 19999 / 20000 = 99.995 % -> 9999.5 bp -> 10000; 1 / 20000 = 0.5 bp -> 1
    assert [(r.key, r.share_bp) for r in rows] == [("MUG-002", 10000), ("MUG-001", 1)]
    assert total.share_bp == 10000
    three = [Item(f"MUG-00{n}", "Mug", 1, (), 0) for n in (1, 2, 3)]
    inv3 = Inventory.from_levels([(f"MUG-00{n}", 1, 0) for n in (1, 2, 3)])
    assert [r.share_bp for r in stock_valuation(three, inv3, by="sku")[0]] == [3333, 3333, 3333]
    two_thirds = [Item("MUG-001", "Mug", 2, (), 0), Item("MUG-002", "Mug", 1, (), 0)]
    inv2 = Inventory.from_levels([("MUG-001", 1, 0), ("MUG-002", 1, 0)])
    assert [r.share_bp for r in stock_valuation(two_thirds, inv2, by="sku")[0]] == [6667, 3333]


def test_zero_total_and_no_items() -> None:
    rows, total = stock_valuation([], Inventory())
    assert rows == [] and total == ValuationRow("TOTAL", 0, 0, 0)
    rows, total = stock_valuation([], Inventory(), by="sku")
    assert rows == [] and total == ValuationRow("TOTAL", 0, 0, 0)
    free = [Item("MUG-001", "Mug", 0, ("a",), 0), Item("MUG-002", "Mug", 100, ("b",), 0)]
    inv = Inventory.from_levels([("MUG-001", 5, 0)])
    rows, total = stock_valuation(free, inv)
    assert rows == [ValuationRow("a", 5, 0, 0), ValuationRow("b", 0, 0, 0)]
    assert total == ValuationRow("TOTAL", 5, 0, 0)


def test_arguments_not_modified() -> None:
    items = list(ITEMS)
    inv = stock()
    stock_valuation(iter(items), inv)
    stock_valuation((i for i in items), inv, by="sku")
    assert items == ITEMS and inv.snapshot() == stock().snapshot()
    assert [i.tags for i in items] == [i.tags for i in ITEMS]


def test_cli_by_tag_default() -> None:
    done = run("valuation", "--items", "data/items.csv", "--stock", "data/stock.csv")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "office  45  $451.98  40.01%",
        "gift  29  $394.60  34.93%",
        "food  39  $335.10  29.66%",
        "tea  39  $335.10  29.66%",
        "kitchen  18  $225.00  19.92%",
        "home  14  $217.58  19.26%",
        "TOTAL  114  $1,129.68  100.00%",
    ]
    same = run("valuation", "--items", "data/items.csv", "--stock", "data/stock.csv", "--by", "tag")
    assert same.stdout == done.stdout


def test_cli_by_sku_and_symbol() -> None:
    done = run("valuation", "--items", "data/items.csv", "--stock", "data/stock.csv", "--by", "sku")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "TEA-010  30  $268.50  23.77%",
        "NB-200  40  $250.00  22.13%",
        "MUG-001  14  $175.00  15.49%",
        "CND-007  12  $117.60  10.41%",
        "PEN-100  3  $102.00  9.03%",
        "LMP-030  2  $99.98  8.85%",
        "TEA-011  9  $66.60  5.90%",
        "MUG-002  4  $50.00  4.43%",
        "TOTAL  114  $1,129.68  100.00%",
    ]
    done = run(
        "valuation", "--items", "data/items.csv", "--stock", "data/stock.csv", "--by", "sku",
        env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "},
    )  # fmt: skip
    assert done.stdout.splitlines()[0] == "TEA-010  30  EUR 268.50  23.77%"
    assert done.stdout.splitlines()[-1] == "TOTAL  114  EUR 1,129.68  100.00%"


def test_cli_bad_grouping(tmp_path: Path) -> None:
    done = run("valuation", "--items", "data/items.csv", "--stock", "data/stock.csv", "--by", "name")
    assert done.returncode == 2 and done.stdout == ""
    items = tmp_path / "items.csv"
    items.write_text("sku,name,price,tags,reorder_level\nMUG-001,Blue Mug,0.05,,5\n")
    stock_file = tmp_path / "stock.csv"
    stock_file.write_text("sku,on_hand,reserved\nMUG-001,1,0\nZZZ-999,4,0\n")
    done = run("valuation", "--items", str(items), "--stock", str(stock_file))
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["(untagged)  1  $0.05  100.00%", "TOTAL  1  $0.05  100.00%"]
    stock_file.write_text("sku,on_hand,reserved\n")
    done = run("valuation", "--items", str(items), "--stock", str(stock_file), "--by", "sku")
    assert done.stdout.splitlines() == ["MUG-001  0  $0.00  0.00%", "TOTAL  0  $0.00  0.00%"]
