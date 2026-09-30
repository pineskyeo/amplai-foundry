import os
import random
import subprocess
import sys
from pathlib import Path

from stockroom.inventory import Inventory
from stockroom.models import Item

ROOT = Path(__file__).resolve().parents[3]


def _items(*rows: tuple[str, int]) -> list[Item]:
    return [Item(sku, f"Item {sku}", 100, (), level) for sku, level in rows]


def run(*args: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env=base,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_the_queue_holds_exactly_the_low_stock_items() -> None:
    items = _items(
        ("AAA-100", 5), ("BBB-100", 5), ("CCC-100", 5), ("DDD-100", 0), ("EEE-100", 5), ("FFF-100", 20)
    )
    inventory = Inventory.from_levels(
        [
            ("AAA-100", 4, 0),
            ("BBB-100", 5, 0),
            ("CCC-100", 20, 18),
            ("DDD-100", 0, 0),
            ("EEE-100", 9, 0),
            ("FFF-100", 30, 15),
        ]
    )
    queue = inventory.restock_queue(items)
    assert sorted(i.sku for i in queue) == sorted(i.sku for i in inventory.low_stock(items))
    assert sorted(i.sku for i in queue) == ["AAA-100", "CCC-100", "FFF-100"]
    assert all(isinstance(i, Item) for i in queue)
    assert inventory.restock_queue([]) == []
    assert [i.sku for i in items][:2] == ["AAA-100", "BBB-100"]


def test_equally_urgent_items_are_ordered_by_sku_whatever_the_input_order() -> None:
    rng = random.Random(4)
    skus = ["MUG-002", "NB-200", "PEN-100", "TEA-011"]
    for _ in range(20):
        shuffled = skus[:]
        rng.shuffle(shuffled)
        items = _items(*[(sku, 10) for sku in shuffled])
        inventory = Inventory.from_levels([(sku, 5, 0) for sku in skus])
        assert [i.sku for i in inventory.restock_queue(items)] == skus
    items = _items(("ZZZ-100", 8), ("AAA-100", 8), ("MMM-100", 8))
    inventory = Inventory.from_levels([("ZZZ-100", 3, 1), ("AAA-100", 2, 0), ("MMM-100", 4, 2)])
    assert [i.sku for i in inventory.restock_queue(items)] == ["AAA-100", "MMM-100", "ZZZ-100"]


def test_an_item_with_nothing_available_comes_before_one_that_is_almost_fine() -> None:
    items = _items(("CCC-100", 10), ("AAA-100", 10), ("BBB-100", 10), ("DDD-100", 10))
    inventory = Inventory.from_levels(
        [("AAA-100", 9, 0), ("BBB-100", 4, 0), ("CCC-100", 0, 0), ("DDD-100", 6, 0)]
    )
    assert [i.sku for i in inventory.restock_queue(items)] == [
        "CCC-100",
        "BBB-100",
        "DDD-100",
        "AAA-100",
    ]


def test_stock_urgent_prints_the_same_low_items_and_the_same_warning() -> None:
    plain = run("stock", "--items", "data/items.csv", "--stock", "data/stock.csv")
    urgent = run("stock", "--items", "data/items.csv", "--stock", "data/stock.csv", "--urgent")
    assert plain.returncode == 0 and urgent.returncode == 0, urgent.stderr
    assert sorted(urgent.stdout.splitlines()) == sorted(plain.stdout.splitlines())
    assert len(urgent.stdout.splitlines()) == 3
    assert urgent.stderr == plain.stderr
    assert "3 item(s) below reorder level" in urgent.stderr


def test_stock_urgent_says_stock_ok_when_nothing_is_low(tmp_path: Path) -> None:
    items = tmp_path / "items.csv"
    items.write_text("sku,name,price,tags,reorder_level\nMUG-001,Blue Mug,12.50,kitchen,5\n")
    stock = tmp_path / "stock.csv"
    stock.write_text("sku,on_hand,reserved\nMUG-001,14,2\n")
    done = run("stock", "--items", str(items), "--stock", str(stock), "--urgent")
    assert done.returncode == 0 and done.stdout == "stock ok\n" and done.stderr == ""


def test_the_item_furthest_below_its_level_in_units_comes_first() -> None:
    for small, large in (("AAA-100", "BBB-100"), ("BBB-100", "AAA-100")):
        items = _items((small, 10), (large, 100))
        inventory = Inventory.from_levels([(small, 5, 0), (large, 80, 0)])
        assert [i.sku for i in inventory.restock_queue(items)] == [large, small]
    plain = run("stock", "--items", "data/items.csv", "--stock", "data/stock.csv", "--urgent")
    assert [line.split()[1] for line in plain.stdout.splitlines()] == [
        "MUG-002",
        "PEN-100",
        "TEA-011",
    ]
