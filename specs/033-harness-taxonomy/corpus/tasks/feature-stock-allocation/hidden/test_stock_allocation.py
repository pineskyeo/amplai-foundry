from __future__ import annotations

import dataclasses
import os
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from stockroom.allocation import Allocation, Allocator, priority_queue
from stockroom.errors import StockError, StockroomError
from stockroom.inventory import Inventory
from stockroom.models import STATUSES, Order, OrderLine

ROOT = Path(__file__).resolve().parents[3]


def make(order_id: str, status: str, day: int, *lines: tuple[str, int]) -> Order:
    return Order(
        order_id,
        "Hana Kim",
        date(2024, 3, day),
        tuple(OrderLine(sku, quantity, 100) for sku, quantity in lines),
        status,
    )


def run(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_queue_order_and_filter() -> None:
    assert [f.name for f in dataclasses.fields(Allocation)] == [
        "order_id",
        "line",
        "sku",
        "wanted",
        "held",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        Allocation("A-1", 1, "MUG-001", 1, 0).held = 1  # type: ignore[misc]
    orders = [
        make("B-2", "open", 1, ("MUG-001", 1)),
        make("A-9", "paid", 5, ("MUG-001", 1)),
        make("C-1", "shipped", 1, ("MUG-001", 1)),
        make("A-10", "paid", 5, ("MUG-001", 1)),
        make("A-3", "paid", 7, ("MUG-001", 1)),
        make("Z-1", "open", 1, ("MUG-001", 1)),
        make("D-4", "cancelled", 1, ("MUG-001", 1)),
        make("A-2", "open", 9, ("MUG-001", 1)),
    ]
    queue = priority_queue(iter(orders))
    assert [o.order_id for o in queue] == ["A-10", "A-9", "A-3", "B-2", "Z-1", "A-2"]
    assert queue[0] is orders[3]
    assert priority_queue(orders) is not orders and len(orders) == 8
    assert priority_queue([]) == []
    with pytest.raises(StockroomError):
        priority_queue(
            [make("A-1", "open", 1, ("MUG-001", 1)), make("A-1", "cancelled", 2, ("TEA-010", 1))]
        )


def test_run_shares_free_stock_in_queue_order() -> None:
    inv = Inventory.from_levels([("MUG-001", 10, 3), ("TEA-010", 4, 0)])
    orders = [
        make("O-1", "open", 1, ("MUG-001", 5), ("TEA-010", 5)),
        make("P-1", "paid", 2, ("MUG-001", 4), ("TEA-010", 1), ("MUG-001", 2)),
        make("P-2", "paid", 1, ("MUG-001", 2), ("NB-200", 1)),
        make("S-1", "shipped", 1, ("MUG-001", 9)),
    ]
    plan = Allocator(inv).run(orders)
    assert plan == [
        Allocation("P-2", 1, "MUG-001", 2, 2),
        Allocation("P-2", 2, "NB-200", 1, 0),
        Allocation("P-1", 1, "MUG-001", 4, 4),
        Allocation("P-1", 2, "TEA-010", 1, 1),
        Allocation("P-1", 3, "MUG-001", 2, 1),
        Allocation("O-1", 1, "MUG-001", 5, 0),
        Allocation("O-1", 2, "TEA-010", 5, 3),
    ]
    # the 3 units reserved before are never handed out; on-hand stock is untouched
    assert inv.snapshot() == {"MUG-001": (10, 10), "TEA-010": (4, 4)}
    assert inv.available("MUG-001") == 0 and inv.available("NB-200") == 0


def test_run_tops_up_and_repeats_without_change() -> None:
    inv = Inventory.from_levels([("MUG-001", 10, 3), ("TEA-010", 4, 0)])
    orders = [
        make("O-1", "open", 1, ("MUG-001", 5), ("TEA-010", 5)),
        make("P-1", "paid", 2, ("MUG-001", 4), ("TEA-010", 1), ("MUG-001", 2)),
        make("P-2", "paid", 1, ("MUG-001", 2), ("NB-200", 1)),
    ]
    allocator = Allocator(inv)
    first = allocator.run(orders)
    assert allocator.run(list(reversed(orders))) == first
    assert inv.snapshot() == {"MUG-001": (10, 10), "TEA-010": (4, 4)}
    inv.receive("MUG-001", 4)
    inv.receive("NB-200", 1)
    again = allocator.run(orders)
    assert [(a.order_id, a.line, a.held) for a in again] == [
        ("P-2", 1, 2),
        ("P-2", 2, 1),
        ("P-1", 1, 4),
        ("P-1", 2, 1),
        ("P-1", 3, 2),
        ("O-1", 1, 3),
        ("O-1", 2, 3),
    ]
    assert inv.snapshot() == {"MUG-001": (14, 14), "NB-200": (1, 1), "TEA-010": (4, 4)}
    assert allocator.held("P-1") == {1: 4, 2: 1, 3: 2}
    assert allocator.held("O-1") == {1: 3, 2: 3}
    # an order that no longer waits keeps what it holds; run does not give it back
    gone = make("O-1", "cancelled", 1, ("MUG-001", 5), ("TEA-010", 5))
    rest = [o for o in orders if o.order_id != "O-1"]
    later = allocator.run([*rest, gone])
    assert [a.order_id for a in later] == ["P-2", "P-2", "P-1", "P-1", "P-1"]
    assert allocator.held("O-1") == {1: 3, 2: 3}
    assert inv.snapshot() == {"MUG-001": (14, 14), "NB-200": (1, 1), "TEA-010": (4, 4)}


def test_run_checks_duplicates_before_reserving() -> None:
    inv = Inventory.from_levels([("MUG-001", 10, 0)])
    allocator = Allocator(inv)
    orders = [
        make("A-1", "paid", 1, ("MUG-001", 3)),
        make("B-1", "open", 2, ("MUG-001", 2)),
        make("A-1", "shipped", 3, ("MUG-001", 1)),
    ]
    with pytest.raises(StockroomError):
        allocator.run(orders)
    assert inv.snapshot() == {"MUG-001": (10, 0)}
    assert allocator.held("A-1") == {} and allocator.held("B-1") == {}
    assert allocator.run([]) == []


def test_complete_only_lets_later_orders_jump() -> None:
    orders = [
        make("P-1", "paid", 1, ("MUG-001", 4), ("TEA-010", 3)),
        make("P-2", "paid", 2, ("MUG-001", 3), ("MUG-001", 2)),
        make("O-1", "open", 1, ("TEA-010", 2)),
        make("O-2", "open", 2, ("MUG-001", 1), ("TEA-010", 1)),
        make("O-3", "open", 3, ("MUG-001", 1)),
    ]
    inv = Inventory.from_levels([("MUG-001", 6, 0), ("TEA-010", 2, 0)])
    plan = Allocator(inv).run(orders, complete_only=True)
    assert [(a.order_id, a.line, a.held) for a in plan] == [
        ("P-1", 1, 0),
        ("P-1", 2, 0),
        ("P-2", 1, 3),
        ("P-2", 2, 2),
        ("O-1", 1, 2),
        ("O-2", 1, 0),
        ("O-2", 2, 0),
        ("O-3", 1, 1),
    ]
    assert inv.snapshot() == {"MUG-001": (6, 6), "TEA-010": (2, 2)}
    # the same data without complete_only serves strictly in queue order
    inv = Inventory.from_levels([("MUG-001", 6, 0), ("TEA-010", 2, 0)])
    allocator = Allocator(inv)
    plan = allocator.run(orders)
    assert [a.held for a in plan] == [4, 2, 2, 0, 0, 0, 0, 0]
    # a complete_only run only needs what is still missing
    inv.receive("TEA-010", 1)
    plan = allocator.run(orders, complete_only=True)
    assert [a.held for a in plan] == [4, 3, 2, 0, 0, 0, 0, 0]
    assert inv.snapshot() == {"MUG-001": (6, 6), "TEA-010": (3, 3)}
    # lines of one SKU are counted together: 3 + 2 units do not fit into 4
    inv = Inventory.from_levels([("MUG-001", 4, 0)])
    plan = Allocator(inv).run([orders[1], orders[4]], complete_only=True)
    assert [a.held for a in plan] == [0, 0, 1]


def test_held_returns_fresh_dicts() -> None:
    inv = Inventory.from_levels([("MUG-001", 5, 0), ("TEA-010", 0, 0)])
    allocator = Allocator(inv)
    allocator.run([make("A-1", "open", 1, ("TEA-010", 2), ("MUG-001", 2), ("MUG-001", 9))])
    held = allocator.held("A-1")
    assert held == {2: 2, 3: 3} and list(held) == [2, 3]
    held[1] = 99
    assert allocator.held("A-1") == {2: 2, 3: 3}
    assert allocator.held("A-1") is not allocator.held("A-1")
    assert allocator.held("ZZ-9") == {}


def test_cancel_gives_everything_back() -> None:
    inv = Inventory.from_levels([("MUG-001", 10, 3), ("TEA-010", 4, 0)])
    orders = [
        make("P-1", "paid", 2, ("MUG-001", 4), ("TEA-010", 1), ("MUG-001", 2)),
        make("P-2", "paid", 1, ("MUG-001", 2), ("NB-200", 1)),
    ]
    allocator = Allocator(inv)
    allocator.run(orders)
    assert inv.snapshot() == {"MUG-001": (10, 10), "TEA-010": (4, 1)}
    assert allocator.cancel("P-1") == 6
    assert inv.snapshot() == {"MUG-001": (10, 5), "TEA-010": (4, 0)}
    assert allocator.held("P-1") == {}
    assert allocator.cancel("P-1") == 0
    assert allocator.cancel("NOPE-1") == 0
    assert inv.snapshot() == {"MUG-001": (10, 5), "TEA-010": (4, 0)}
    # a cancelled order can be served again from scratch by a later run
    plan = allocator.run(orders)
    assert [a.held for a in plan] == [2, 0, 4, 1, 1]
    assert inv.snapshot() == {"MUG-001": (10, 10), "TEA-010": (4, 1)}


def test_ship_needs_every_line_fully_held() -> None:
    inv = Inventory.from_levels([("MUG-001", 5, 0), ("TEA-010", 5, 1)])
    full = make("S-1", "paid", 1, ("MUG-001", 2), ("TEA-010", 3))
    part = make("S-2", "open", 2, ("MUG-001", 1), ("TEA-010", 4))
    allocator = Allocator(inv)
    allocator.run([full, part])
    assert allocator.held("S-2") == {1: 1, 2: 1}
    with pytest.raises(StockError):
        allocator.ship(part)
    assert allocator.held("S-2") == {1: 1, 2: 1}
    assert inv.snapshot() == {"MUG-001": (5, 3), "TEA-010": (5, 5)}
    allocator.ship(full)
    assert inv.snapshot() == {"MUG-001": (3, 1), "TEA-010": (2, 2)}
    assert allocator.held("S-1") == {}
    with pytest.raises(StockError):
        allocator.ship(full)
    with pytest.raises(StockError):
        allocator.ship(make("S-9", "paid", 1, ("MUG-001", 1)))
    assert inv.snapshot() == {"MUG-001": (3, 1), "TEA-010": (2, 2)}


def _model_run(
    levels: dict[str, list[int]],
    held: dict[tuple[str, int], tuple[str, int]],
    orders: list[Order],
    complete_only: bool,
) -> list[Allocation]:
    def free(sku: str) -> int:
        on_hand, reserved = levels.get(sku, [0, 0])
        return on_hand - reserved

    def take(order_id: str, number: int, sku: str, units: int) -> None:
        levels[sku][1] += units
        held[(order_id, number)] = (sku, held.get((order_id, number), (sku, 0))[1] + units)

    waiting = [o for o in orders if o.status in ("paid", "open")]
    waiting.sort(key=lambda o: (o.status != "paid", o.placed, o.order_id))
    out = []
    for order in waiting:
        lines = list(enumerate(order.lines, start=1))
        missing = {
            n: line.quantity - held.get((order.order_id, n), ("", 0))[1] for n, line in lines
        }
        if complete_only:
            need: dict[str, int] = {}
            for n, line in lines:
                need[line.sku] = need.get(line.sku, 0) + missing[n]
            if all(units <= free(sku) for sku, units in need.items()):
                for n, line in lines:
                    if missing[n] > 0:
                        take(order.order_id, n, line.sku, missing[n])
        else:
            for n, line in lines:
                units = min(missing[n], free(line.sku))
                if units > 0:
                    take(order.order_id, n, line.sku, units)
        out += [
            Allocation(
                order.order_id,
                n,
                line.sku,
                line.quantity,
                held.get((order.order_id, n), ("", 0))[1],
            )
            for n, line in lines
        ]
    return out


def test_random_sequences_follow_the_model() -> None:
    rng = random.Random(20261011)
    skus = ["MUG-001", "TEA-010", "NB-200"]
    for _ in range(80):
        levels: dict[str, list[int]] = {}
        for sku in skus:
            if rng.random() < 0.8:
                on_hand = rng.randrange(0, 12)
                levels[sku] = [on_hand, rng.randrange(0, on_hand + 1)]
        before = {sku: levels.get(sku, [0, 0])[1] for sku in skus}
        inv = Inventory.from_levels([(s, v[0], v[1]) for s, v in levels.items()])
        orders = [
            Order(
                f"R-{n}",
                "Ada Moss",
                date(2024, 5, rng.randrange(1, 5)),
                tuple(
                    OrderLine(rng.choice(skus), rng.randrange(1, 6), 100)
                    for _ in range(rng.randrange(1, 4))
                ),
                rng.choice(STATUSES),
            )
            for n in range(rng.randrange(3, 9))
        ]
        allocator = Allocator(inv)
        held: dict[tuple[str, int], tuple[str, int]] = {}
        for _ in range(14):
            op = rng.choice(["run", "run", "complete", "receive", "cancel", "ship"])
            if op in ("run", "complete"):
                subset = [o for o in orders if rng.random() < 0.85]
                rng.shuffle(subset)
                expected = _model_run(levels, held, subset, op == "complete")
                assert allocator.run(subset, complete_only=op == "complete") == expected
            elif op == "receive":
                sku = rng.choice(skus)
                units = rng.randrange(1, 6)
                inv.receive(sku, units)
                levels.setdefault(sku, [0, 0])[0] += units
            elif op == "cancel":
                order = rng.choice(orders)
                mine = {k: v for k, v in held.items() if k[0] == order.order_id}
                for key, (sku, units) in mine.items():
                    levels[sku][1] -= units
                    del held[key]
                assert allocator.cancel(order.order_id) == sum(u for _, u in mine.values())
            else:
                order = rng.choice(orders)
                full = all(
                    held.get((order.order_id, n), ("", 0))[1] == line.quantity
                    for n, line in enumerate(order.lines, start=1)
                )
                if full:
                    allocator.ship(order)
                    for line in order.lines:
                        levels[line.sku][0] -= line.quantity
                        levels[line.sku][1] -= line.quantity
                    for key in [k for k in held if k[0] == order.order_id]:
                        del held[key]
                    orders[orders.index(order)] = dataclasses.replace(order, status="shipped")
                else:
                    with pytest.raises(StockError):
                        allocator.ship(order)
            assert inv.snapshot() == {s: (v[0], v[1]) for s, v in sorted(levels.items())}
            for sku in skus:
                mine_units = sum(u for s, u in held.values() if s == sku)
                assert inv.reserved(sku) == before[sku] + mine_units
                assert inv.available(sku) >= 0
            for order in orders:
                expected_held = {
                    n: u for (oid, n), (_, u) in sorted(held.items()) if oid == order.order_id and u
                }
                assert allocator.held(order.order_id) == expected_held


def test_cli_sample_data() -> None:
    done = run("allocate", "--orders", "data/orders.csv", "--stock", "data/stock.csv")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "ORDER     LINE  SKU       WANTED  HELD SHORT",
        "A-1002       1  PEN-100        1     1     0",
        "A-1004       1  CND-007        3     3     0",
        "A-1004       2  NB-200         4     4     0",
        "A-1005       1  TEA-011        2     2     0",
        "A-1006       1  MUG-002        1     1     0",
        "TOTAL                         11    11     0",
        "",
        "SKU        ON_HAND RESERVED AVAILABLE",
        "CND-007         12        4         8",
        "LMP-030          2        0         2",
        "MUG-001         14        2        12",
        "MUG-002          4        1         3",
        "NB-200          40        8        32",
        "PEN-100          3        3         0",
        "TEA-010         30        5        25",
        "TEA-011          9        2         7",
    ]


def test_cli_complete_only_and_shortages(tmp_path: Path) -> None:
    orders = tmp_path / "orders.csv"
    orders.write_text(
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        "W-7,Omar Diaz,2024-04-02,open,MUG-001,1,12.50\n"
        "W-12,Lena Berg,2024-04-03,paid,MUG-001,4,12.50\n"
        "W-12,Lena Berg,2024-04-03,paid,TEA-010,3,8.95\n"
        "W-3,Hana Kim,2024-04-01,shipped,MUG-001,5,12.50\n"
        "W-100,Ada Moss,2024-04-03,paid,MUG-001,3,12.50\n"
        "W-100,Ada Moss,2024-04-03,paid,MUG-001,2,12.50\n"
        "W-8,Ravi Nair,2024-04-02,open,TEA-010,2,8.95\n"
    )
    stock = tmp_path / "stock.csv"
    stock.write_text("sku,on_hand,reserved\nTEA-010,2,0\nMUG-001,7,1\n")
    done = run("allocate", "--orders", str(orders), "--stock", str(stock), cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "ORDER     LINE  SKU       WANTED  HELD SHORT",
        "W-100        1  MUG-001        3     3     0",
        "W-100        2  MUG-001        2     2     0",
        "W-12         1  MUG-001        4     1     3",
        "W-12         2  TEA-010        3     2     1",
        "W-7          1  MUG-001        1     0     1",
        "W-8          1  TEA-010        2     0     2",
        "TOTAL                         15     8     7",
        "",
        "SKU        ON_HAND RESERVED AVAILABLE",
        "MUG-001          7        7         0",
        "TEA-010          2        2         0",
    ]
    done = run(
        "allocate", "--orders", str(orders), "--stock", str(stock), "--complete-only", cwd=tmp_path
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "ORDER     LINE  SKU       WANTED  HELD SHORT",
        "W-100        1  MUG-001        3     3     0",
        "W-100        2  MUG-001        2     2     0",
        "W-12         1  MUG-001        4     0     4",
        "W-12         2  TEA-010        3     0     3",
        "W-7          1  MUG-001        1     1     0",
        "W-8          1  TEA-010        2     2     0",
        "TOTAL                         15     8     7",
        "",
        "SKU        ON_HAND RESERVED AVAILABLE",
        "MUG-001          7        7         0",
        "TEA-010          2        2         0",
    ]
    bad = tmp_path / "bad.csv"
    bad.write_text("sku,on_hand,reserved\nMUG-001,1,2\n")
    done = run("allocate", "--orders", str(orders), "--stock", str(bad), cwd=tmp_path)
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error: ")
