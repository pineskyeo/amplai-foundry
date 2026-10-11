from __future__ import annotations

import dataclasses
import os
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from stockroom.errors import ParseError
from stockroom.models import Item, Order, OrderLine
from stockroom.money import format_signed
from stockroom.pricelist import (
    PriceRule,
    Repricing,
    best_rule,
    price_for,
    read_price_rules,
    reprice,
)

ROOT = Path(__file__).resolve().parents[3]
ITEMS = [
    Item("MUG-001", "Blue Mug", 1250, ("kitchen",), 5),
    Item("TEA-010", "Green Tea 100g", 895, ("tea",), 10),
]
ITEMS_CSV = (
    "sku,name,price,tags,reorder_level\n"
    "MUG-001,Blue Mug,12.50,kitchen,5\n"
    "TEA-010,Green Tea 100g,8.95,tea,10\n"
    "PEN-100,Fountain Pen,34.00,office,2\n"
)
ORDERS_CSV = (
    "order_id,customer,placed,status,sku,quantity,unit_price\n"
    "C-1,Hana Kim,2024-03-05,open,MUG-001,2,12.50\n"
    "C-1,Hana Kim,2024-03-05,open,TEA-010,3,8.95\n"
    "C-2,Omar Diaz,2024-03-15,open,TEA-010,1,9.25\n"
    "C-2,Omar Diaz,2024-03-15,open,PEN-100,1,34.00\n"
    "C-2,Omar Diaz,2024-03-15,open,MUG-001,4,10.00\n"
    "C-3,Lena Berg,2024-03-12,paid,MUG-001,1,12.50\n"
    "C-4,Lena Berg,2024-04-01,open,MUG-001,1,11.00\n"
)


def d(month: int, day: int) -> date:
    return date(2024, month, day)


def rule(sku: str, customer: str, start: date, end: date | None, cents: int) -> PriceRule:
    return PriceRule(sku, customer, start, end, cents)


def run(
    *args: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_records_and_signed_amounts() -> None:
    assert [f.name for f in dataclasses.fields(PriceRule)] == [
        "sku",
        "customer",
        "start",
        "end",
        "unit_price_cents",
    ]
    assert [f.name for f in dataclasses.fields(Repricing)] == [
        "order_id",
        "line",
        "sku",
        "quantity",
        "old_cents",
        "new_cents",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        rule("MUG-001", "", d(3, 1), None, 100).unit_price_cents = 5  # type: ignore[misc]
    assert format_signed(125) == "+$1.25"
    assert format_signed(-50) == "-$0.50"
    assert format_signed(0) == "$0.00"
    assert format_signed(123456, "EUR ") == "+EUR 1,234.56"
    assert format_signed(-123456, symbol="EUR ") == "-EUR 1,234.56"


def test_read_price_rules() -> None:
    text = (
        "sku, customer ,start,end,price\n"
        " MUG-001 ,,2024-03-01,,11.00\n"
        ",,,,\n"
        "MUG-001, Hana  Kim ,2024-02-01, 2024-04-01 ,$10\n"
    )
    assert read_price_rules(text) == [
        rule("MUG-001", "", d(3, 1), None, 1100),
        rule("MUG-001", "Hana  Kim", d(2, 1), d(4, 1), 1000),
    ]
    assert read_price_rules("sku,customer,start,end,price\n") == []
    header = "sku,customer,start,end,price\n"
    for bad in (
        "",
        "sku,customer,from,to,price\nMUG-001,,2024-03-01,,1.00\n",
        header + "MUG-001,,2024-03-01,1.00\n",
        header + "MUG-001,,2024-03-01,,1.00,x\n",
        header + "mug-001,,2024-03-01,,1.00\n",
        header + "MUG-001,,2024-03-32,,1.00\n",
        header + "MUG-001,,2024-03-01,2024-03-01,1.00\n",
        header + "MUG-001,,2024-03-02,2024-03-01,1.00\n",
        header + "MUG-001,,2024-03-01,,-2.00\n",
        header + "MUG-001,,2024-03-01,,cheap\n",
        header + "MUG-001,,2024-03-01,,1.005\n",
    ):
        with pytest.raises(ParseError):
            read_price_rules(bad)


def test_windows_include_the_start_and_exclude_the_end() -> None:
    window = rule("MUG-001", "", d(3, 1), d(3, 10), 900)
    rules = [window]
    assert best_rule(rules, "MUG-001", "Hana Kim", d(2, 29)) is None
    assert best_rule(rules, "MUG-001", "Hana Kim", d(3, 1)) is window
    assert best_rule(rules, "MUG-001", "Hana Kim", d(3, 9)) is window
    assert best_rule(rules, "MUG-001", "Hana Kim", d(3, 10)) is None
    assert best_rule(rules, "TEA-010", "Hana Kim", d(3, 5)) is None
    forever = rule("MUG-001", "", d(3, 5), None, 800)
    assert best_rule([forever], "MUG-001", "x", date(9000, 1, 1)) is forever
    assert best_rule([forever], "MUG-001", "x", d(3, 4)) is None


def test_customer_rules_beat_general_ones() -> None:
    mine = rule("MUG-001", "hana kim", d(1, 1), None, 1000)
    general = rule("MUG-001", "", d(3, 1), None, 900)
    blank = rule("MUG-001", "   ", d(3, 2), None, 950)
    rules = [general, mine, blank]
    assert best_rule(rules, "MUG-001", "Hana Kim", d(3, 5)) is mine
    assert best_rule(rules, "MUG-001", "  HANA\t kim ", d(3, 5)) is mine
    assert best_rule(rules, "MUG-001", "Hana Kimm", d(3, 5)) is blank
    assert best_rule(rules, "MUG-001", "Omar Diaz", d(3, 1)) is general
    assert best_rule(rules, "MUG-001", "", d(3, 5)) is blank
    straße = rule("TEA-010", "Lena Straße", d(1, 1), None, 700)
    assert best_rule([straße], "TEA-010", "LENA STRASSE", d(3, 5)) is straße


def test_tie_breaks() -> None:
    early = rule("MUG-001", "", d(3, 1), None, 100)
    late = rule("MUG-001", "", d(3, 3), None, 900)
    assert best_rule([late, early], "MUG-001", "x", d(3, 5)) is late
    open_ended = rule("MUG-001", "", d(3, 3), None, 100)
    bounded_far = rule("MUG-001", "", d(3, 3), d(4, 1), 800)
    bounded_near = rule("MUG-001", "", d(3, 3), d(3, 20), 900)
    found = best_rule([open_ended, bounded_far, bounded_near], "MUG-001", "x", d(3, 5))
    assert found is bounded_near
    assert best_rule([open_ended, bounded_far], "MUG-001", "x", d(3, 5)) is bounded_far
    cheap = rule("MUG-001", "", d(3, 3), d(3, 20), 500)
    assert best_rule([bounded_near, cheap], "MUG-001", "x", d(3, 5)) is cheap
    twin_a = rule("MUG-001", "", d(3, 3), d(3, 20), 500)
    twin_b = rule("MUG-001", "", d(3, 3), d(3, 20), 500)
    assert best_rule([bounded_near, twin_a, twin_b], "MUG-001", "x", d(3, 5)) is twin_a
    assert best_rule([twin_b, twin_a], "MUG-001", "x", d(3, 5)) is twin_b
    cheaper_open = rule("MUG-001", "", d(3, 3), None, 1)
    assert best_rule([cheaper_open, twin_a], "MUG-001", "x", d(3, 5)) is twin_a


def test_price_for_falls_back_to_the_catalogue() -> None:
    rules = [rule("TEA-010", "", d(3, 1), None, 950), rule("PEN-100", "", d(3, 1), None, 3000)]
    assert price_for(rules, ITEMS, "TEA-010", "Hana Kim", d(3, 1)) == 950
    assert price_for(rules, ITEMS, "TEA-010", "Hana Kim", d(2, 1)) == 895
    assert price_for(rules, iter(ITEMS), "MUG-001", "Hana Kim", d(3, 1)) == 1250
    assert price_for(rules, ITEMS, "PEN-100", "Hana Kim", d(3, 1)) == 3000
    with pytest.raises(ParseError):
        price_for(rules, ITEMS, "PEN-100", "Hana Kim", d(2, 1))
    with pytest.raises(ParseError):
        price_for([], [], "MUG-001", "Hana Kim", d(3, 1))


def test_reprice_changes_open_orders_only() -> None:
    rules = [
        rule("MUG-001", "", d(3, 1), None, 1100),
        rule("MUG-001", "hana kim", d(2, 1), d(4, 1), 1000),
        rule("TEA-010", "", d(3, 1), d(3, 15), 950),
    ]
    orders = [
        Order(
            "C-1",
            "Hana Kim",
            d(3, 5),
            (
                OrderLine("MUG-001", 2, 1250),
                OrderLine("TEA-010", 3, 950),
                OrderLine("TEA-010", 1, 900),
            ),
        ),
        Order("C-2", "Omar Diaz", d(3, 15), (OrderLine("TEA-010", 2, 950),), "open"),
        Order("C-3", "Omar Diaz", d(3, 5), (OrderLine("MUG-001", 1, 1250),), "paid"),
        Order("C-4", "Omar Diaz", d(3, 5), (OrderLine("MUG-001", 1, 1250),), "shipped"),
        Order("C-5", "Omar Diaz", d(3, 5), (OrderLine("MUG-001", 1, 1250),), "cancelled"),
        Order("C-6", "Ada Moss", d(3, 6), (OrderLine("MUG-001", 5, 1250),)),
    ]
    assert reprice(iter(orders), rules, iter(ITEMS)) == [
        Repricing("C-1", 1, "MUG-001", 2, 1250, 1000),
        Repricing("C-1", 3, "TEA-010", 1, 900, 950),
        Repricing("C-2", 1, "TEA-010", 2, 950, 895),
        Repricing("C-6", 1, "MUG-001", 5, 1250, 1100),
    ]
    assert reprice(orders[2:5], rules, ITEMS) == []


def _model(rules: list[PriceRule], sku: str, customer: str, day: date) -> PriceRule | None:
    who = " ".join(customer.split()).casefold()
    best, best_key = None, None
    for index, candidate in enumerate(rules):
        general = candidate.customer.strip() == ""
        if candidate.sku != sku:
            continue
        if not general and " ".join(candidate.customer.split()).casefold() != who:
            continue
        if candidate.start > day or (candidate.end is not None and day >= candidate.end):
            continue
        key = (
            1 if general else 0,
            -candidate.start.toordinal(),
            1 if candidate.end is None else 0,
            candidate.end.toordinal() if candidate.end is not None else 0,
            candidate.unit_price_cents,
            index,
        )
        if best_key is None or key < best_key:
            best, best_key = candidate, key
    return best


def test_random_queries_match_the_model() -> None:
    rng = random.Random(3141)
    names = ["Hana Kim", "hana  KIM", "Omar Diaz", "", "  "]
    for _ in range(400):
        rules = []
        for _ in range(rng.randrange(0, 9)):
            start = d(3, 1) + timedelta(days=rng.randrange(0, 12))
            end = None if rng.random() < 0.4 else start + timedelta(days=rng.randrange(1, 10))
            rules.append(
                rule(
                    rng.choice(["MUG-001", "TEA-010"]),
                    rng.choice(names),
                    start,
                    end,
                    rng.choice([100, 200, 300]),
                )
            )
        for _ in range(12):
            sku = rng.choice(["MUG-001", "TEA-010"])
            customer = rng.choice(["Hana Kim", " HANA kim", "Omar  Diaz", "Ada Moss", ""])
            day = d(3, 1) + timedelta(days=rng.randrange(-1, 22))
            expected = _model(rules, sku, customer, day)
            assert best_rule(rules, sku, customer, day) is expected
            fallback = 1250 if sku == "MUG-001" else 895
            price = expected.unit_price_cents if expected is not None else fallback
            assert price_for(rules, ITEMS, sku, customer, day) == price


def test_cli_reprice_report(tmp_path: Path) -> None:
    (tmp_path / "items.csv").write_text(ITEMS_CSV)
    (tmp_path / "orders.csv").write_text(ORDERS_CSV)
    rules = tmp_path / "rules.csv"
    rules.write_text(
        "sku,customer,start,end,price\n"
        "MUG-001,,2024-03-01,,11.00\n"
        "MUG-001,hana kim,2024-02-01,2024-04-01,10.00\n"
        "TEA-010,,2024-03-01,2024-03-15,9.50\n"
        "TEA-010,,2024-03-10,2024-03-20,9.25\n"
        "PEN-100,Omar Diaz,2024-03-01,,30.00\n"
    )
    args = ("reprice", "--items", "items.csv", "--rules", "rules.csv", "--orders", "orders.csv")
    done = run(*args, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "C-1/1 MUG-001 x2: $12.50 -> $10.00 (-$5.00)",
        "C-1/2 TEA-010 x3: $8.95 -> $9.50 (+$1.65)",
        "C-2/2 PEN-100 x1: $34.00 -> $30.00 (-$4.00)",
        "C-2/3 MUG-001 x4: $10.00 -> $11.00 (+$4.00)",
        "4 lines changed in 2 orders, difference -$3.35",
    ]
    rules.write_text("sku,customer,start,end,price\nTEA-010,,2024-03-01,2024-03-06,9.00\n")
    done = run(*args, cwd=tmp_path, env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "})
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "C-1/2 TEA-010 x3: EUR 8.95 -> EUR 9.00 (+EUR 0.15)",
        "C-2/1 TEA-010 x1: EUR 9.25 -> EUR 8.95 (-EUR 0.30)",
        "C-2/3 MUG-001 x4: EUR 10.00 -> EUR 12.50 (+EUR 10.00)",
        "C-4/1 MUG-001 x1: EUR 11.00 -> EUR 12.50 (+EUR 1.50)",
        "4 lines changed in 3 orders, difference +EUR 11.35",
    ]
    (tmp_path / "one.csv").write_text(
        ORDERS_CSV.splitlines()[0] + "\n" + ORDERS_CSV.splitlines()[2] + "\n"
    )
    done = run(
        "reprice",
        "--items",
        "items.csv",
        "--rules",
        "rules.csv",
        "--orders",
        "one.csv",
        cwd=tmp_path,
    )
    assert done.stdout.splitlines() == [
        "C-1/1 TEA-010 x3: $8.95 -> $9.00 (+$0.15)",
        "1 line changed in 1 order, difference +$0.15",
    ]
    rules.write_text("sku,customer,start,end,price\n")
    (tmp_path / "same.csv").write_text(
        ORDERS_CSV.splitlines()[0] + "\n" + ORDERS_CSV.splitlines()[1] + "\n"
    )
    done = run(
        "reprice",
        "--items",
        "items.csv",
        "--rules",
        "rules.csv",
        "--orders",
        "same.csv",
        cwd=tmp_path,
    )
    assert done.stdout.splitlines() == ["0 lines changed in 0 orders, difference $0.00"]


@pytest.mark.parametrize(
    ("rules", "orders"),
    [
        ("sku,customer,start,end,price\nMUG-001,,2024-03-01,2024-03-01,1.00\n", ORDERS_CSV),
        ("sku,customer,start,end,price\nMUG-001,,2024-03-01,,-1.00\n", ORDERS_CSV),
        (
            "sku,customer,start,end,price\n",
            ORDERS_CSV + "C-9,Ada Moss,2024-03-01,open,LMP-030,1,49.99\n",
        ),
    ],
)
def test_cli_errors(tmp_path: Path, rules: str, orders: str) -> None:
    (tmp_path / "items.csv").write_text(ITEMS_CSV)
    (tmp_path / "orders.csv").write_text(orders)
    (tmp_path / "rules.csv").write_text(rules)
    done = run(
        "reprice",
        "--items",
        "items.csv",
        "--rules",
        "rules.csv",
        "--orders",
        "orders.csv",
        cwd=tmp_path,
    )
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error: ")
