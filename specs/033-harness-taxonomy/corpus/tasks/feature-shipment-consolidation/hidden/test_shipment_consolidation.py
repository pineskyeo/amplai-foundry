from __future__ import annotations

import dataclasses
import math
import os
import random
import subprocess
import sys
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path

import pytest
from stockroom.config import DEFAULTS, load_config
from stockroom.errors import ConfigError, StockroomError
from stockroom.models import Order, OrderLine
from stockroom.money import allocate
from stockroom.pricing import order_subtotal
from stockroom.shipments import Shipment, consolidate

ROOT = Path(__file__).resolve().parents[3]
ORDERS = (
    "order_id,customer,placed,status,sku,quantity,unit_price\n"
    "H-1,Hana Kim,2024-03-01,paid,MUG-001,2,12.50\n"
    "H-2,Hana Kim,2024-03-03,paid,TEA-010,3,8.95\n"
    "H-2,Hana Kim,2024-03-03,paid,NB-200,1,6.25\n"
    "H-3,Hana Kim,2024-03-05,paid,PEN-100,1,34.00\n"
    "O-1,Omar Diaz,2024-03-01,paid,MUG-001,1,12.50\n"
    "O-2,Omar Diaz,2024-03-02,open,MUG-001,1,12.50\n"
    "L-1,Lena Berg,2024-03-02,paid,LMP-030,1,49.99\n"
    "L-2,Lena Berg,2024-03-02,paid,CND-007,1,0.01\n"
)


def order(
    order_id: str, customer: str, day: int, units: int, cents: int = 100, status: str = "paid"
) -> Order:
    return Order(
        order_id, customer, date(2024, 3, day), (OrderLine("MUG-001", units, cents),), status
    )


def run(
    *args: str, cwd: Path = ROOT, env: dict[str, str] | None = None
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


def test_allocate_by_largest_remainder() -> None:
    assert allocate(100, [1, 1, 1]) == [34, 33, 33]
    assert allocate(600, [3395, 5440]) == [231, 369]
    assert allocate(2, [1, 3]) == [0, 2]
    assert allocate(2, [3, 1]) == [2, 0]
    assert allocate(3, [2, 2, 1, 1]) == [1, 1, 1, 0]
    assert allocate(700, [2500, 3310, 3400]) == [190, 252, 258]
    assert allocate(700, [4999, 1]) == [700, 0]
    assert allocate(10, [0, 1]) == [0, 10]
    assert allocate(0, [5, 5]) == [0, 0]
    assert allocate(5, [0, 0]) == [3, 2] and allocate(0, [0]) == [0]
    assert allocate(10**18, [1, 2]) == [333333333333333333, 666666666666666667]
    weights = (5, 7)
    assert allocate(3, weights) == [1, 2] and weights == (5, 7)
    for total, bad in ((-1, [1]), (5, []), (5, [1, -1])):
        with pytest.raises(ValueError):
            allocate(total, bad)


def test_groups_follow_the_window_and_unit_limit() -> None:
    assert [f.name for f in dataclasses.fields(Shipment)] == [
        "shipment_id",
        "customer",
        "placed",
        "order_ids",
        "units",
        "subtotal_cents",
        "fee_cents",
        "fee_shares",
    ]
    orders = [
        order("H-4", "Hana Kim", 5, 4),
        order("H-2", "Hana Kim", 3, 3),
        order("H-1", "Hana Kim", 1, 2),
        order("H-3", "Hana Kim", 4, 1),
        order("h-9", "hana kim", 2, 1),
        order("H-8", "Hana Kim", 2, 1, status="open"),
        order("H-7", "Hana Kim", 2, 1, status="shipped"),
        order("O-1", "Omar Diaz", 10, 15),
        order("O-2", "Omar Diaz", 10, 5),
        order("O-3", "Omar Diaz", 11, 1),
        order("O-4", "Omar Diaz", 20, 25),
        order("O-5", "Omar Diaz", 20, 1),
    ]
    shipments = consolidate(iter(orders))
    assert [(s.customer, s.order_ids, s.units) for s in shipments] == [
        ("Hana Kim", ("H-1", "H-2", "H-3"), 6),
        ("hana kim", ("h-9",), 1),
        ("Hana Kim", ("H-4",), 4),
        ("Omar Diaz", ("O-1", "O-2"), 20),
        ("Omar Diaz", ("O-3",), 1),
        ("Omar Diaz", ("O-4",), 25),
        ("Omar Diaz", ("O-5",), 1),
    ]
    assert [s.placed for s in shipments] == [date(2024, 3, d) for d in (1, 2, 5, 10, 11, 20, 20)]
    assert [s.shipment_id for s in shipments] == [f"SHP-{n}" for n in range(1, 8)]
    wide = consolidate(orders, window_days=4, max_units=21)
    assert [s.order_ids for s in wide][:4] == [
        ("H-1", "H-2", "H-3", "H-4"),
        ("h-9",),
        ("O-1", "O-2", "O-3"),
        ("O-4",),
    ]
    tight = consolidate(orders, window_days=0)
    assert [s.order_ids for s in tight][:3] == [("H-1",), ("h-9",), ("H-2",)]
    assert consolidate([]) == [] and consolidate(orders[5:7]) == []


def test_shipments_are_sorted_and_numbered() -> None:
    orders = [
        order("Z-1", "Zoe Park", 1, 1),
        order("A-2", "Ada Moss", 1, 1),
        order("B-5", "Ben Ito", 2, 15),
        order("B-6", "Ben Ito", 2, 6),
        order("B-1", "Ben Ito", 2, 1),
    ]
    shipments = consolidate(orders)
    assert [(s.shipment_id, s.order_ids) for s in shipments] == [
        ("SHP-1", ("A-2",)),
        ("SHP-2", ("Z-1",)),
        ("SHP-3", ("B-1", "B-5")),
        ("SHP-4", ("B-6",)),
    ]


def test_fees_and_shares() -> None:
    orders = [
        order("A-1", "Ada Moss", 1, 1, 2500),
        order("A-2", "Ada Moss", 2, 1, 2499),
        order("B-1", "Ben Ito", 1, 1, 3395),
        order("B-2", "Ben Ito", 1, 1, 5440),
    ]
    a, b = consolidate(orders)
    assert (a.subtotal_cents, a.fee_cents, a.fee_shares) == (4999, 600, (300, 300))
    assert (b.subtotal_cents, b.fee_cents, b.fee_shares) == (8835, 0, (0, 0))
    a, b = consolidate(orders, free_from_cents=8836, fee_cents=600)
    assert b.fee_cents == 600 and b.fee_shares == (231, 369)
    a, b = consolidate(orders, free_from_cents=4999, fee_cents=999)
    assert a.fee_cents == 0 and b.fee_cents == 0
    a, _ = consolidate(orders, free_from_cents=5000, fee_cents=1)
    assert a.fee_shares == (1, 0)
    (only,) = consolidate(orders[:2], free_from_cents=0, fee_cents=0)
    assert only.fee_cents == 0 and only.fee_shares == (0, 0)
    for bad in (
        {"window_days": -1},
        {"max_units": 0},
        {"fee_cents": -1},
        {"free_from_cents": -1},
    ):
        with pytest.raises(ValueError):
            consolidate(orders, **bad)  # type: ignore[arg-type]
    with pytest.raises(StockroomError):
        consolidate([*orders, order("A-1", "Ada Moss", 9, 1, status="cancelled")])


def test_settings() -> None:
    assert DEFAULTS["shipping_fee_cents"] == 600 and DEFAULTS["free_shipping_cents"] == 5000
    config = load_config(
        env={"STOCKROOM_SHIPPING_FEE_CENTS": "750", "STOCKROOM_FREE_SHIPPING_CENTS": "0"}
    )
    assert config["shipping_fee_cents"] == 750 and config["free_shipping_cents"] == 0
    with pytest.raises(ConfigError):
        load_config(env={"STOCKROOM_SHIPPING_FEE_CENTS": "-1"})
    with pytest.raises(ConfigError):
        load_config(env={"STOCKROOM_FREE_SHIPPING_CENTS": "lots"})


def _model_allocate(total: int, weights: list[int]) -> list[int]:
    whole = sum(weights)
    if whole == 0:
        base, extra = divmod(total, len(weights))
        return [base + 1 if i < extra else base for i in range(len(weights))]
    quotas = [Fraction(total * w, whole) for w in weights]
    shares = [math.floor(q) for q in quotas]
    order_ = sorted(range(len(weights)), key=lambda i: (-(quotas[i] - shares[i]), -weights[i], i))
    for i in order_[: total - sum(shares)]:
        shares[i] += 1
    return shares


def _model_consolidate(
    orders: list[Order], window: int, most: int, fee: int, free: int
) -> list[tuple[str, date, tuple[str, ...], int, int, int, tuple[int, ...]]]:
    groups: list[list[Order]] = []
    by_customer: dict[str, list[Order]] = {}
    for o in sorted(orders, key=lambda o: (o.placed, o.order_id)):
        if o.status == "paid":
            by_customer.setdefault(o.customer, []).append(o)
    for mine in by_customer.values():
        current: list[Order] = []
        for o in mine:
            if (
                current
                and (o.placed - current[0].placed).days <= window
                and sum(x.quantity for x in current) + o.quantity <= most
            ):
                current.append(o)
            else:
                if current:
                    groups.append(current)
                current = [o]
        groups.append(current)
    groups.sort(key=lambda g: (g[0].placed, g[0].customer, g[0].order_id))
    out = []
    for g in groups:
        subtotals = [order_subtotal(o) for o in g]
        cost = 0 if sum(subtotals) >= free else fee
        out.append(
            (
                g[0].customer,
                g[0].placed,
                tuple(o.order_id for o in g),
                sum(o.quantity for o in g),
                sum(subtotals),
                cost,
                tuple(_model_allocate(cost, subtotals)),
            )
        )
    return out


def test_random_data_matches_the_model() -> None:
    rng = random.Random(6006)
    for _ in range(400):
        weights = [rng.choice([0, 0, 1, 2, 3, 7, 10, 999]) for _ in range(rng.randrange(1, 6))]
        total = rng.choice([0, 1, 2, 5, 99, 600, 1001, 10**12 + 7])
        assert allocate(total, weights) == _model_allocate(total, weights)
    customers = ["Ada Moss", "Ben Ito", "ada moss"]
    for _ in range(150):
        orders = [
            Order(
                f"R-{n}",
                rng.choice(customers),
                date(2024, 5, 1) + timedelta(days=rng.randrange(0, 12)),
                tuple(
                    OrderLine("MUG-001", rng.randrange(1, 6), rng.choice([0, 1, 250, 999, 2500]))
                    for _ in range(rng.randrange(1, 3))
                ),
                rng.choice(["paid", "paid", "paid", "open", "cancelled"]),
            )
            for n in range(rng.randrange(0, 12))
        ]
        rng.shuffle(orders)
        window, most = rng.randrange(0, 5), rng.randrange(1, 15)
        fee, free = rng.choice([0, 1, 600, 1001]), rng.choice([0, 2000, 5000, 10**9])
        got = consolidate(
            orders, window_days=window, max_units=most, fee_cents=fee, free_from_cents=free
        )
        expected = _model_consolidate(orders, window, most, fee, free)
        assert [s.shipment_id for s in got] == [f"SHP-{n}" for n in range(1, len(expected) + 1)]
        assert [
            (
                s.customer,
                s.placed,
                s.order_ids,
                s.units,
                s.subtotal_cents,
                s.fee_cents,
                s.fee_shares,
            )
            for s in got
        ] == expected


def test_cli_sample_data() -> None:
    done = run("shipments", "--orders", "data/orders.csv")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "SHP-1  Omar Diaz  2024-01-19  1 order  1 unit  $34.00  fee $6.00",
        "  A-1002  $34.00  $6.00",
        "SHP-2  Hana Kim  2024-02-14  1 order  7 units  $54.40  fee $0.00",
        "  A-1004  $54.40  $0.00",
        "2 shipments, fees $6.00",
    ]


def test_cli_options_and_settings(tmp_path: Path) -> None:
    path = tmp_path / "orders.csv"
    path.write_text(ORDERS)
    done = run("shipments", "--orders", str(path), cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "SHP-1  Hana Kim  2024-03-01  2 orders  6 units  $58.10  fee $0.00",
        "  H-1  $25.00  $0.00",
        "  H-2  $33.10  $0.00",
        "SHP-2  Omar Diaz  2024-03-01  1 order  1 unit  $12.50  fee $6.00",
        "  O-1  $12.50  $6.00",
        "SHP-3  Lena Berg  2024-03-02  2 orders  2 units  $50.00  fee $0.00",
        "  L-1  $49.99  $0.00",
        "  L-2  $0.01  $0.00",
        "SHP-4  Hana Kim  2024-03-05  1 order  1 unit  $34.00  fee $6.00",
        "  H-3  $34.00  $6.00",
        "4 shipments, fees $12.00",
    ]
    env = {"STOCKROOM_SHIPPING_FEE_CENTS": "700", "STOCKROOM_FREE_SHIPPING_CENTS": "99999"}
    done = run(
        "shipments",
        "--orders",
        str(path),
        "--window",
        "4",
        "--max-units",
        "7",
        cwd=tmp_path,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "SHP-1  Hana Kim  2024-03-01  3 orders  7 units  $92.10  fee $7.00",
        "  H-1  $25.00  $1.90",
        "  H-2  $33.10  $2.52",
        "  H-3  $34.00  $2.58",
        "SHP-2  Omar Diaz  2024-03-01  1 order  1 unit  $12.50  fee $7.00",
        "  O-1  $12.50  $7.00",
        "SHP-3  Lena Berg  2024-03-02  2 orders  2 units  $50.00  fee $7.00",
        "  L-1  $49.99  $7.00",
        "  L-2  $0.01  $0.00",
        "3 shipments, fees $21.00",
    ]
    config = tmp_path / "s.ini"
    config.write_text("[stockroom]\nshipping_fee_cents = 50\n")
    done = run(
        "--config", str(config), "shipments", "--orders", str(path), "--window", "0",
        cwd=tmp_path, env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "},
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1] == "5 shipments, fees EUR 2.00"
    for option, value in (
        ("--window", "-1"),
        ("--window", "x"),
        ("--max-units", "0"),
        ("--max-units", "2.5"),
    ):
        done = run("shipments", "--orders", str(path), option, value, cwd=tmp_path)
        assert done.returncode == 2 and done.stdout == "", (option, value)
    done = run(
        "shipments", "--orders", str(path), cwd=tmp_path, env={"STOCKROOM_SHIPPING_FEE_CENTS": "-5"}
    )
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error: ")
    empty = tmp_path / "none.csv"
    empty.write_text(ORDERS.splitlines()[0] + "\n" + ORDERS.splitlines()[6] + "\n")
    done = run("shipments", "--orders", str(empty), cwd=tmp_path)
    assert done.stdout.splitlines() == ["0 shipments, fees $0.00"]
