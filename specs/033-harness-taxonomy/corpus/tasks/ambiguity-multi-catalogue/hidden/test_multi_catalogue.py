import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
from stockroom.csvio import merge_catalogues, read_items, write_items
from stockroom.errors import ParseError
from stockroom.models import Item

ROOT = Path(__file__).resolve().parents[3]
HEADER = "sku,name,price,tags,reorder_level\n"
MAIN = (
    HEADER
    + "MUG-001,Blue Mug,12.50,kitchen;gift,5\n"
    + "TEA-010,Green Tea 100g,8.95,food;tea,10\n"
    + "PEN-100,Fountain Pen,34.00,office,2\n"
)
EXTRA = (
    HEADER
    + "LMP-030,Desk Lamp,49.99,office,1\n"
    + "MUG-001,Blue Mug,12.50,kitchen;gift,5\n"
    + "NB-200,Notebook A5,6.25,office;paper,20\n"
)
SALE = (
    HEADER + "TEA-010,Green Tea 100g,7.95,food;tea;sale,4\n" + "MUG-002,Red Mug,12.50,kitchen,5\n"
)
STOCK = "sku,on_hand,reserved\nMUG-001,14,2\nTEA-010,9,0\nNB-200,25,10\nLMP-030,3,0\nPEN-100,1,0\n"


def run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    for name, text in (
        ("main.csv", MAIN),
        ("extra.csv", EXTRA),
        ("sale.csv", SALE),
        ("stock.csv", STOCK),
        ("bad.csv", "sku,name\nMUG-001,Blue Mug\n"),
    ):
        (tmp_path / name).write_text(text, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def skus(items: list[Item]) -> list[str]:
    return [item.sku for item in items]


def test_catalogues_merge_in_order_of_first_appearance() -> None:
    assert merge_catalogues([MAIN]) == read_items(MAIN)
    merged = merge_catalogues([MAIN, EXTRA])
    assert skus(merged) == ["MUG-001", "TEA-010", "PEN-100", "LMP-030", "NB-200"]
    assert merged[3] == Item("LMP-030", "Desk Lamp", 4999, ("office",), 1)
    assert skus(merge_catalogues([EXTRA, MAIN])) == [
        "LMP-030",
        "MUG-001",
        "NB-200",
        "TEA-010",
        "PEN-100",
    ]
    assert skus(merge_catalogues((MAIN, HEADER))) == ["MUG-001", "TEA-010", "PEN-100"]
    with pytest.raises(ValueError):
        merge_catalogues([])


def test_catalogues_keep_identical_records_once() -> None:
    twice = MAIN + "MUG-001,Blue Mug,12.50,kitchen;gift,5\n"
    assert merge_catalogues([twice]) == read_items(MAIN)
    assert merge_catalogues([MAIN, MAIN, MAIN]) == read_items(MAIN)
    merged = merge_catalogues([EXTRA, MAIN, EXTRA])
    assert len(merged) == 5
    assert merged.count(read_items(MAIN)[0]) == 1


def test_catalogues_pass_on_read_errors() -> None:
    with pytest.raises(ParseError) as caught:
        merge_catalogues([MAIN, "sku,name\nMUG-001,Blue Mug\n"])
    assert str(caught.value) == "item header must be sku,name,price,tags,reorder_level"
    with pytest.raises(ParseError):
        merge_catalogues([MAIN, HEADER + "MUG-001,Blue Mug,cheap,,5\n"])


def test_catalogue_options_take_several_files(tmp_path: Path) -> None:
    done = run(tmp_path, "items", "--file", "main.csv", "--file", "extra.csv")
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0].split() == ["SKU", "Name", "Price"]
    assert [line.split()[0] for line in lines[2:]] == [
        "MUG-001",
        "TEA-010",
        "PEN-100",
        "LMP-030",
        "NB-200",
    ]
    assert lines[5].endswith("$49.99")
    tagged = run(tmp_path, "items", "--file", "main.csv", "--file", "extra.csv", "--tag", "Office")
    assert [line.split()[0] for line in tagged.stdout.splitlines()[2:]] == [
        "PEN-100",
        "LMP-030",
        "NB-200",
    ]
    stock = run(
        tmp_path, "stock", "--items", "main.csv", "--items", "extra.csv", "--stock", "stock.csv"
    )
    assert stock.returncode == 0, stock.stderr
    assert stock.stdout.splitlines() == [
        "LOW TEA-010 Green Tea 100g: 9/10",
        "LOW PEN-100 Fountain Pen: 1/2",
        "LOW NB-200 Notebook A5: 15/20",
    ]
    price = run(
        tmp_path,
        "price",
        "--items",
        "main.csv",
        "--items",
        "extra.csv",
        "--sku",
        "LMP-030",
        "--quantity",
        "2",
    )
    assert price.stdout.splitlines()[0] == "2 x LMP-030 Desk Lamp: $99.98"
    bad = run(tmp_path, "items", "--file", "main.csv", "--file", "bad.csv")
    assert bad.returncode == 1 and bad.stdout == ""
    assert bad.stderr == "stockroom: error: item header must be sku,name,price,tags,reorder_level\n"


def test_catalogue_conflicts_keep_the_record_read_last(tmp_path: Path) -> None:
    merged = merge_catalogues([MAIN, SALE])
    assert skus(merged) == ["MUG-001", "TEA-010", "PEN-100", "MUG-002"]
    assert merged[1] == Item("TEA-010", "Green Tea 100g", 795, ("food", "tea", "sale"), 4)
    assert merge_catalogues([SALE, MAIN])[0] == read_items(MAIN)[1]
    inside = MAIN + "PEN-100,Fountain Pen,30.00,office,2\n"
    assert merge_catalogues([inside])[2].price_cents == 3000
    rng = random.Random(5150)
    pool = ["MUG-001", "MUG-002", "TEA-010", "NB-200", "PEN-100", "LMP-030"]
    for _ in range(300):
        files = []
        expected: dict[str, Item] = {}
        for _ in range(rng.randrange(1, 4)):
            rows = [
                Item(sku, f"Name {rng.randrange(3)}", rng.randrange(1, 9000), (), rng.randrange(4))
                for sku in (rng.choice(pool) for _ in range(rng.randrange(0, 6)))
            ]
            files.append(write_items(rows))
            for row in rows:
                expected[row.sku] = row
        assert merge_catalogues(files) == list(expected.values())
    price = run(
        tmp_path,
        "price",
        "--items",
        "main.csv",
        "--items",
        "sale.csv",
        "--sku",
        "TEA-010",
        "--quantity",
        "2",
    )
    assert price.stdout.splitlines()[0] == "2 x TEA-010 Green Tea 100g: $15.90"
    stock = run(
        tmp_path, "stock", "--items", "main.csv", "--items", "sale.csv", "--stock", "stock.csv"
    )
    assert stock.stdout.splitlines() == [
        "LOW PEN-100 Fountain Pen: 1/2",
        "LOW MUG-002 Red Mug: 0/5",
    ]
