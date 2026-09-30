import math
import os
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from stockroom.errors import ParseError
from stockroom.inventory import Inventory
from stockroom.models import Item, Order, OrderLine
from stockroom.restock import RestockLine, plan_restock

ROOT = Path(__file__).resolve().parents[3]


def _item(sku: str, level: int, price: int = 100, tags: tuple[str, ...] = ()) -> Item:
    return Item(sku, f"Item {sku}", price, tags, level)


def _order(order_id: str, status: str, *lines: tuple[str, int]) -> Order:
    return Order(
        order_id, "Hana Kim", date(2024, 7, 1), tuple(OrderLine(s, q, 100) for s, q in lines), status
    )


def _stock(*levels: tuple[str, int, int]) -> Inventory:
    return Inventory.from_levels(list(levels))


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



def test_the_quantity_is_twice_the_reorder_level_plus_demand_minus_on_hand() -> None:
    items = [_item("MUG-001", 10), _item("TEA-010", 10), _item("PEN-100", 10), _item("NB-200", 10)]
    inventory = _stock(("MUG-001", 5, 4), ("TEA-010", 20, 0), ("PEN-100", 25, 0), ("NB-200", 0, 0))
    orders = [
        _order("O-1", "open", ("MUG-001", 7), ("TEA-010", 3)),
        _order("O-2", "paid", ("MUG-001", 3), ("ZZZ-999", 50)),
        _order("O-3", "shipped", ("MUG-001", 100), ("PEN-100", 100)),
        _order("O-4", "cancelled", ("MUG-001", 100), ("NB-200", 100)),
    ]
    assert plan_restock(items, inventory, orders) == [
        RestockLine("MUG-001", 25, 1),
        RestockLine("NB-200", 20, 1),
        RestockLine("TEA-010", 3, 1),
    ]


def test_nothing_is_ordered_when_nothing_is_missing_or_the_reorder_level_is_zero() -> None:
    items = [_item("MUG-001", 10), _item("TEA-010", 0), _item("PEN-100", 4)]
    inventory = _stock(("MUG-001", 20, 0), ("TEA-010", 0, 0), ("PEN-100", 9, 9))
    orders = [_order("O-1", "open", ("TEA-010", 12), ("PEN-100", 1))]
    assert plan_restock(items, inventory, orders) == []
    assert plan_restock(items, inventory, [_order("O-2", "paid", ("PEN-100", 2))]) == [
        RestockLine("PEN-100", 1, 1)
    ]
    assert plan_restock([], inventory, orders) == []
    assert plan_restock(items, Inventory(), []) == [
        RestockLine("MUG-001", 20, 1),
        RestockLine("PEN-100", 8, 1),
    ]
    assert plan_restock(items, Inventory(), iter([])) == [
        RestockLine("MUG-001", 20, 1),
        RestockLine("PEN-100", 8, 1),
    ]


def test_quantities_are_rounded_up_to_whole_packs_of_the_first_pack_tag() -> None:
    # reorder level 8 and nothing on hand: 16 units are missing, plus the demand given
    cases = [
        (("pack:12",), 0, 24, 12),
        (("pack:12",), 8, 24, 12),
        (("pack:12",), 9, 36, 12),
        (("pack:12",), -4, 12, 12),
        (("gift", "pack:6", "pack:4"), 0, 18, 6),
        (("pack:1",), 0, 16, 1),
        (("PACK:12",), 0, 16, 1),
        (("pack",), 0, 16, 1),
        (("packs:12",), 0, 16, 1),
        (("xpack:12",), 0, 16, 1),
        (("pack:100",), 0, 100, 100),
        (("pack:16",), 0, 16, 16),
    ]
    for tags, extra_demand, quantity, size in cases:
        orders = [_order("O-1", "open", ("MUG-001", extra_demand))] if extra_demand > 0 else []
        stock = Inventory() if extra_demand >= 0 else _stock(("MUG-001", 4, 0))
        plan = plan_restock([_item("MUG-001", 8, tags=tags)], stock, orders)
        assert plan == [RestockLine("MUG-001", quantity, size)], (tags, extra_demand)


def test_an_invalid_pack_tag_is_refused_even_when_nothing_is_ordered() -> None:
    bad_tags = ["pack:0", "pack:x", "pack:-3", "pack:", "pack:+5", "pack: 5", "pack:5.0", "pack:1x"]
    full = _stock(("MUG-001", 99, 0), ("TEA-010", 99, 0))
    for bad in bad_tags:
        for tags in ((bad,), ("pack:6", bad), (bad, "pack:6"), ("gift", bad)):
            with pytest.raises(ParseError) as caught:
                plan_restock([_item("MUG-001", 1, tags=tags)], full, [])
            assert str(caught.value) == f"MUG-001: bad pack tag {bad!r}"
    with pytest.raises(ParseError) as caught:
        plan_restock(
            [_item("TEA-010", 1, tags=("pack:2",)), _item("ZZZ-001", 1, tags=("pack:a", "pack:b"))],
            full,
            [],
        )
    assert str(caught.value) == "ZZZ-001: bad pack tag 'pack:a'"
    with pytest.raises(ParseError) as caught:
        plan_restock(
            [_item("ZZZ-001", 1, tags=("pack:b",)), _item("AAA-001", 1, tags=("pack:a",))], full, []
        )
    assert str(caught.value) == "ZZZ-001: bad pack tag 'pack:b'"
    assert plan_restock([_item("MUG-001", 0, tags=("pack:6",))], full, []) == []


def test_lines_come_in_sku_order_and_the_budget_skips_what_does_not_fit() -> None:
    # every item is short by 20 units: costs are 3000, 1000 and 2000 cents
    items = [_item("CCC-100", 10, 100), _item("AAA-100", 10, 150), _item("BBB-100", 10, 50)]
    inventory = Inventory()
    line = {sku: RestockLine(sku, 20, 1) for sku in ("AAA-100", "BBB-100", "CCC-100")}

    def plan(budget: int | None) -> list[str]:
        return [ln.sku for ln in plan_restock(items, inventory, [], budget_cents=budget)]

    assert plan(None) == ["AAA-100", "BBB-100", "CCC-100"]
    assert plan(6000) == ["AAA-100", "BBB-100", "CCC-100"]
    assert plan(5999) == ["AAA-100", "BBB-100"]
    assert plan(4000) == ["AAA-100", "BBB-100"]
    assert plan(3500) == ["AAA-100"]
    assert plan(3000) == ["AAA-100"]
    assert plan(2999) == ["BBB-100"]
    assert plan(2500) == ["BBB-100"]
    assert plan(1000) == ["BBB-100"]
    assert plan(999) == []
    assert plan(0) == []
    # a dropped line does not stop the later ones: costs are now 1000, 3000 and 500 cents
    cheap = [_item("CCC-100", 10, 25), _item("AAA-100", 10, 50), _item("BBB-100", 10, 150)]
    assert [ln.sku for ln in plan_restock(cheap, inventory, [], budget_cents=2000)] == ["AAA-100", "CCC-100"]
    assert [ln.sku for ln in plan_restock(cheap, inventory, [], budget_cents=1500)] == ["AAA-100", "CCC-100"]
    assert [ln.sku for ln in plan_restock(cheap, inventory, [], budget_cents=1499)] == ["AAA-100"]
    assert [ln.sku for ln in plan_restock(cheap, inventory, [], budget_cents=3999)] == ["AAA-100", "CCC-100"]
    assert [ln.sku for ln in plan_restock(cheap, inventory, [], budget_cents=499)] == []
    assert plan_restock(items, inventory, [], budget_cents=4000)[0] == line["AAA-100"]
    with pytest.raises(ValueError):
        plan_restock(items, inventory, [], budget_cents=-1)


def test_random_plans_match_the_definition() -> None:
    rng = random.Random(17)
    skus = [f"SKU-{n:03d}" for n in range(8)]
    for _ in range(300):
        items = []
        for sku in rng.sample(skus, k=rng.randint(0, 8)):
            tags = rng.choice([(), ("pack:5",), ("food", "pack:12"), ("pack:3", "pack:9"), ("Pack:4",)])
            items.append(_item(sku, rng.randint(0, 9), rng.randint(1, 500), tags))
        inventory = _stock(*[(s, (h := rng.randint(0, 30)), rng.randint(0, h)) for s in skus if rng.random() < 0.7])
        orders = [
            _order(f"O-{n}", rng.choice(["open", "paid", "shipped", "cancelled"]), *[(rng.choice(skus), rng.randint(1, 9)) for _ in range(rng.randint(1, 3))])
            for n in range(rng.randint(0, 5))
        ]
        budget = rng.choice([None, 0, 500, 2000, 10000])
        expected = []
        remaining = budget
        for item in sorted(items, key=lambda i: i.sku):
            if item.reorder_level == 0:
                continue
            demand = sum(
                ln.quantity
                for o in orders
                if o.status in ("open", "paid")
                for ln in o.lines
                if ln.sku == item.sku
            )
            missing = 2 * item.reorder_level + demand - inventory.on_hand(item.sku)
            if missing <= 0:
                continue
            pack = 1
            for tag in item.tags:
                if tag.startswith("pack:"):
                    pack = int(tag[5:])
                    break
            quantity = math.ceil(missing / pack) * pack
            cost = quantity * item.price_cents
            if remaining is None or cost <= remaining:
                expected.append(RestockLine(item.sku, quantity, pack))
                if remaining is not None:
                    remaining -= cost
        assert plan_restock(items, inventory, orders, budget_cents=budget) == expected


ITEMS = (
    "sku,name,price,tags,reorder_level\n"
    "MUG-001,Blue Mug,12.50,kitchen;pack:12,8\n"
    "TEA-010,Green Tea 100g,8.95,food,10\n"
    "PEN-100,Fountain Pen,34.00,office,2\n"
)
STOCK = "sku,on_hand,reserved\nMUG-001,2,0\nTEA-010,30,5\nPEN-100,0,0\n"
ORDERS = (
    "order_id,customer,placed,status,sku,quantity,unit_price\n"
    "A-1,Hana Kim,2024-07-01,open,PEN-100,1,34.00\n"
    "A-2,Hana Kim,2024-07-02,paid,MUG-001,3,12.50\n"
)


def _files(tmp_path: Path, items: str = ITEMS, stock: str = STOCK, orders: str = ORDERS) -> list[str]:
    (tmp_path / "items.csv").write_text(items)
    (tmp_path / "stock.csv").write_text(stock)
    (tmp_path / "orders.csv").write_text(orders)
    return [
        "restock",
        "--items",
        str(tmp_path / "items.csv"),
        "--stock",
        str(tmp_path / "stock.csv"),
        "--orders",
        str(tmp_path / "orders.csv"),
    ]


def test_the_sample_data_needs_five_orders() -> None:
    done = run(
        "restock", "--items", "data/items.csv", "--stock", "data/stock.csv", "--orders", "data/orders.csv"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "RESTOCK CND-007 Beeswax Candle: 7",
        "RESTOCK MUG-002 Red Mug: 7",
        "RESTOCK NB-200 Notebook A5: 4",
        "RESTOCK PEN-100 Fountain Pen: 2",
        "RESTOCK TEA-011 Black Tea 100g: 13",
        "total: $345.30",
    ]
    assert done.stderr == ""


def test_the_command_shows_packs_and_the_budget_cut() -> None:
    sample = [
        "restock", "--items", "data/items.csv", "--stock", "data/stock.csv", "--orders", "data/orders.csv",
    ]
    cut = run(*sample, "--budget", "100.00")
    assert cut.returncode == 0, cut.stderr
    assert cut.stdout.splitlines() == [
        "RESTOCK CND-007 Beeswax Candle: 7",
        "RESTOCK NB-200 Notebook A5: 4",
        "total: $93.60",
        "over budget: MUG-002, PEN-100, TEA-011",
    ]
    none_fit = run(*sample, "--budget", "5")
    assert none_fit.stdout.splitlines() == ["over budget: CND-007, MUG-002, NB-200, PEN-100, TEA-011"]


def test_the_command_prints_pack_counts_and_honours_the_currency_setting(tmp_path: Path) -> None:
    args = _files(tmp_path)
    done = run(*args, env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "})
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "RESTOCK MUG-001 Blue Mug: 24 (2 x 12)",
        "RESTOCK PEN-100 Fountain Pen: 5",
        "total: EUR 470.00",
    ]


def test_the_command_says_when_nothing_is_needed(tmp_path: Path) -> None:
    args = _files(tmp_path, stock="sku,on_hand,reserved\nMUG-001,60,0\nTEA-010,30,5\nPEN-100,50,0\n")
    done = run(*args)
    assert done.returncode == 0 and done.stdout == "nothing to restock\n"
    done = run(*args, "--budget", "0.00")
    assert done.returncode == 0 and done.stdout == "nothing to restock\n"


def test_the_command_reports_bad_input_as_data_errors(tmp_path: Path) -> None:
    args = _files(tmp_path)
    negative = run(*args, "--budget", "-5.00")
    assert negative.returncode == 1 and negative.stdout == ""
    assert negative.stderr == "stockroom: error: budget must not be negative\n"
    bad_items = ITEMS.replace("kitchen;pack:12", "kitchen;pack:0")
    bad = run(*_files(tmp_path, items=bad_items))
    assert bad.returncode == 1 and bad.stdout == ""
    assert bad.stderr == "stockroom: error: MUG-001: bad pack tag 'pack:0'\n"
