from __future__ import annotations

import dataclasses
import os
import random
import subprocess
import sys
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path

import pytest
from stockroom.config import DEFAULTS, load_config
from stockroom.errors import ConfigError, ParseError, StockError, StockroomError
from stockroom.inventory import Inventory
from stockroom.purchasing import (
    POLine,
    PurchaseOrder,
    Receiving,
    read_purchase_orders,
    read_receipts,
)

ROOT = Path(__file__).resolve().parents[3]
POS = (
    "po_id,supplier,expected,sku,ordered,unit_cost\n"
    "P-10,Acme,2024-03-01,MUG-001,10,10.00\n"
    "P-10,Acme,2024-03-01,TEA-010,25,3.33\n"
    "P-11,Bolt Ltd,2024-03-04,MUG-001,6,10.12\n"
    "P-12,Cove,2024-02-28,PEN-100,2,30.00\n"
)
RECEIPTS = "po_id,sku,quantity\nP-10,MUG-001,6\nP-11,MUG-001,6\nP-10,TEA-010,27\nP-10,MUG-001,1\n"


def d(month: int, day: int) -> date:
    return date(2024, month, day)


def po(
    po_id: str, expected: date, *lines: tuple[str, int, int], supplier: str = "Acme"
) -> PurchaseOrder:
    return PurchaseOrder(po_id, supplier, expected, tuple(POLine(*line) for line in lines))


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


def test_records_and_add_checks() -> None:
    assert [f.name for f in dataclasses.fields(POLine)] == ["sku", "ordered", "unit_cost_cents"]
    assert [f.name for f in dataclasses.fields(PurchaseOrder)] == [
        "po_id",
        "supplier",
        "expected",
        "lines",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        POLine("MUG-001", 1, 100).ordered = 2  # type: ignore[misc]
    desk = Receiving(Inventory())
    desk.add(po("P-1", d(3, 1), ("MUG-001", 10, 1000)))
    with pytest.raises(StockroomError):
        desk.add(po("P-1", d(3, 2), ("TEA-010", 1, 100)))
    for bad in (
        po("P-2", d(3, 1)),
        po("P-2", d(3, 1), ("mug-001", 1, 100)),
        po("P-2", d(3, 1), ("MUG-001", 0, 100)),
        po("P-2", d(3, 1), ("MUG-001", 1, -1)),
        po("P-2", d(3, 1), ("MUG-001", 1, 100), ("TEA-010", 1, 100), ("MUG-001", 2, 100)),
    ):
        with pytest.raises(ParseError):
            desk.add(bad)
    with pytest.raises(StockroomError):
        desk.status("P-2")
    desk.add(po("P-2", d(3, 1), ("MUG-001", 1, 0)))
    assert desk.status("P-2") == "open"
    with pytest.raises(ValueError):
        Receiving(Inventory(), tolerance_bp=-1)


def test_receipts_and_tolerance() -> None:
    inv = Inventory.from_levels([("MUG-001", 3, 1)])
    desk = Receiving(inv)
    desk.add(po("P-1", d(3, 1), ("MUG-001", 10, 1000), ("TEA-010", 25, 100), ("NB-200", 9, 100)))
    assert desk.receive("P-1", "MUG-001", 4) == 6
    assert desk.receive("P-1", "MUG-001", 7) == 0
    assert desk.received("P-1", "MUG-001") == 11
    assert inv.snapshot() == {"MUG-001": (14, 1)}
    with pytest.raises(StockError):
        desk.receive("P-1", "MUG-001", 1)
    assert desk.receive("P-1", "TEA-010", 27) == 0
    with pytest.raises(StockError):
        desk.receive("P-1", "TEA-010", 1)
    with pytest.raises(StockError):
        desk.receive("P-1", "NB-200", 10)
    assert desk.receive("P-1", "NB-200", 9) == 0
    with pytest.raises(StockError):
        desk.receive("P-1", "NB-200", 1)
    with pytest.raises(StockError):
        desk.receive("P-1", "MUG-001", 0)
    for po_id, sku in (("P-9", "MUG-001"), ("P-1", "PEN-100")):
        with pytest.raises(StockroomError):
            desk.receive(po_id, sku, 1)
        with pytest.raises(StockroomError):
            desk.received(po_id, sku)
    assert inv.snapshot() == {"MUG-001": (14, 1), "NB-200": (9, 0), "TEA-010": (27, 0)}
    strict = Receiving(Inventory(), tolerance_bp=0)
    strict.add(po("P-1", d(3, 1), ("MUG-001", 10, 1000)))
    with pytest.raises(StockError):
        strict.receive("P-1", "MUG-001", 11)
    loose = Receiving(Inventory(), tolerance_bp=2500)
    loose.add(po("P-1", d(3, 1), ("MUG-001", 10, 1000)))
    assert loose.receive("P-1", "MUG-001", 12) == 0
    with pytest.raises(StockError):
        loose.receive("P-1", "MUG-001", 1)


def test_status_close_and_outstanding() -> None:
    desk = Receiving(Inventory())
    desk.add(po("P-1", d(3, 1), ("MUG-001", 10, 1000), ("TEA-010", 4, 100)))
    desk.add(po("P-2", d(3, 1), ("MUG-001", 2, 1000)))
    desk.add(po("P-3", d(3, 1), ("MUG-001", 2, 1000)))
    assert desk.status("P-1") == "open"
    assert desk.outstanding("P-1") == {"MUG-001": 10, "TEA-010": 4}
    assert list(desk.outstanding("P-1")) == ["MUG-001", "TEA-010"]
    desk.receive("P-1", "TEA-010", 4)
    assert desk.status("P-1") == "partial" and desk.outstanding("P-1") == {"MUG-001": 10}
    desk.receive("P-1", "MUG-001", 11)
    assert desk.status("P-1") == "received" and desk.outstanding("P-1") == {}
    with pytest.raises(StockroomError):
        desk.close("P-1")
    assert desk.status("P-1") == "received"
    desk.receive("P-2", "MUG-001", 1)
    desk.close("P-2")
    assert desk.status("P-2") == "closed" and desk.outstanding("P-2") == {}
    assert desk.received("P-2", "MUG-001") == 1
    desk.close("P-2")
    assert desk.status("P-2") == "closed"
    with pytest.raises(StockroomError):
        desk.receive("P-2", "MUG-001", 1)
    desk.close("P-3")
    assert desk.status("P-3") == "closed"
    with pytest.raises(StockroomError):
        desk.close("P-9")
    with pytest.raises(StockroomError):
        desk.outstanding("P-9")


def test_moving_average_cost() -> None:
    inv = Inventory.from_levels([("MUG-001", 5, 0)])
    desk = Receiving(inv)
    desk.add(po("P-1", d(3, 1), ("MUG-001", 10, 1000), ("TEA-010", 4, 333)))
    desk.add(po("P-2", d(3, 2), ("MUG-001", 10, 1300)))
    assert desk.average_cost("MUG-001") is None and desk.average_cost("NB-200") is None
    desk.receive("P-1", "MUG-001", 2)
    assert desk.average_cost("MUG-001") == 1000
    desk.receive("P-2", "MUG-001", 3)
    assert desk.average_cost("MUG-001") == 1090
    inv.reserve("MUG-001", 8)
    inv.ship("MUG-001", 8)
    desk.receive("P-1", "MUG-001", 1)
    assert desk.average_cost("MUG-001") == 1060
    desk.receive("P-1", "TEA-010", 3)
    assert desk.average_cost("TEA-010") == 333
    other = Receiving(Inventory())
    other.add(po("P-3", d(3, 1), ("MUG-001", 10, 1000)))
    other.add(po("P-4", d(3, 1), ("MUG-001", 10, 1001)))
    other.add(po("P-5", d(3, 1), ("MUG-001", 10, 1003)))
    other.receive("P-3", "MUG-001", 1)
    other.receive("P-4", "MUG-001", 1)
    assert other.average_cost("MUG-001") == 1000
    other.receive("P-5", "MUG-001", 2)
    assert other.average_cost("MUG-001") == 1002
    with pytest.raises(StockError):
        other.receive("P-5", "MUG-001", 10)
    assert other.average_cost("MUG-001") == 1002


def test_overdue_purchase_orders() -> None:
    desk = Receiving(Inventory())
    desk.add(po("P-1", d(3, 1), ("MUG-001", 1, 100)))
    desk.add(po("P-2", d(3, 2), ("MUG-001", 5, 100)))
    desk.add(po("P-3", d(3, 2), ("MUG-001", 1, 100)))
    desk.add(po("P-4", d(3, 1), ("MUG-001", 1, 100)))
    desk.add(po("P-0", d(3, 5), ("MUG-001", 1, 100)))
    desk.add(po("P-9", d(2, 28), ("MUG-001", 1, 100)))
    desk.add(po("P-11", d(3, 1), ("MUG-001", 1, 100)))
    desk.receive("P-2", "MUG-001", 1)
    desk.receive("P-3", "MUG-001", 1)
    desk.close("P-4")
    assert desk.overdue(d(3, 2)) == ["P-9", "P-1", "P-11"]
    assert desk.overdue(d(3, 3)) == ["P-9", "P-1", "P-11", "P-2"]
    assert desk.overdue(d(2, 28)) == []
    assert desk.overdue(d(4, 1)) == ["P-9", "P-1", "P-11", "P-2", "P-0"]


def test_readers() -> None:
    text = (
        "po_id, supplier ,expected,sku,ordered,unit_cost\n"
        "P-1,Acme,2024-03-01,MUG-001,10,10.00\n"
        " P-2 , Bolt Ltd ,2024-03-02,TEA-010, 4 ,3.33\n"
        ",,,,,\n"
        "P-1,Acme, 2024-03-01 ,TEA-010,5,$2.50\n"
    )
    assert read_purchase_orders(text) == [
        po("P-1", d(3, 1), ("MUG-001", 10, 1000), ("TEA-010", 5, 250)),
        po("P-2", d(3, 2), ("TEA-010", 4, 333), supplier="Bolt Ltd"),
    ]
    header = "po_id,supplier,expected,sku,ordered,unit_cost\n"
    first = "P-1,Acme,2024-03-01,MUG-001,10,10.00\n"
    for bad in (
        "",
        "po,supplier,expected,sku,ordered,unit_cost\n" + first,
        header + "P-1,Acme,2024-03-01,MUG-001,10\n",
        header + first + "P-1,Acme Inc,2024-03-01,TEA-010,1,1.00\n",
        header + first + "P-1,Acme,2024-03-02,TEA-010,1,1.00\n",
        header + ",Acme,2024-03-01,MUG-001,10,10.00\n",
        header + "P-1,Acme,2024-03-01,MUG-001,ten,10.00\n",
        header + "P-1,Acme,2024-03-01,MUG-001,10,cheap\n",
        header + "P-1,Acme,2024-02-30,MUG-001,10,10.00\n",
    ):
        with pytest.raises(ParseError):
            read_purchase_orders(bad)
    assert read_receipts("po_id,sku,quantity\nP-1, MUG-001 ,3\n,,\nP-2,TEA-010,1\n") == [
        ("P-1", "MUG-001", 3),
        ("P-2", "TEA-010", 1),
    ]
    for bad in (
        "",
        "po,sku,quantity\nP-1,MUG-001,3\n",
        "po_id,sku,quantity\nP-1,MUG-001\n",
        "po_id,sku,quantity\nP-1,MUG-001,three\n",
    ):
        with pytest.raises(ParseError):
            read_receipts(bad)


def test_setting() -> None:
    assert DEFAULTS["over_receipt_bp"] == 1000
    assert load_config(env={"STOCKROOM_OVER_RECEIPT_BP": "250"})["over_receipt_bp"] == 250
    with pytest.raises(ConfigError):
        load_config(env={"STOCKROOM_OVER_RECEIPT_BP": "-1"})


def _half_even(value: Fraction) -> int:
    return round(value)


def _status(order: PurchaseOrder, got: dict[tuple[str, str], int], closed: set[str]) -> str:
    if order.po_id in closed:
        return "closed"
    done = [got.get((order.po_id, line.sku), 0) for line in order.lines]
    if all(g >= line.ordered for g, line in zip(done, order.lines, strict=True)):
        return "received"
    return "partial" if any(done) else "open"


def test_random_receipts_follow_the_model() -> None:
    rng = random.Random(5150)
    skus = ["MUG-001", "TEA-010"]
    for _ in range(60):
        tolerance = rng.choice([0, 500, 1000, 2500])
        on_hand = {sku: rng.randrange(0, 5) for sku in skus}
        inv = Inventory.from_levels([(sku, units, 0) for sku, units in on_hand.items()])
        desk = Receiving(inv, tolerance_bp=tolerance)
        orders = []
        for k in range(3):
            chosen = rng.sample(skus, rng.randrange(1, 3))
            order = po(
                f"P-{k}",
                d(3, 1) + timedelta(days=rng.randrange(0, 6)),
                *[
                    (sku, rng.randrange(1, 12), rng.choice([0, 999, 1000, 1001, 1003]))
                    for sku in chosen
                ],
            )
            desk.add(order)
            orders.append(order)
        got: dict[tuple[str, str], int] = {}
        closed: set[str] = set()
        average: dict[str, int] = {}

        for _ in range(25):
            op = rng.choice(["receive", "receive", "receive", "close", "ship"])
            order = rng.choice(orders)
            if op == "receive":
                sku = rng.choice(skus)
                units = rng.randrange(0, 6)
                line = next((x for x in order.lines if x.sku == sku), None)
                if order.po_id in closed or line is None:
                    with pytest.raises(StockroomError):
                        desk.receive(order.po_id, sku, units)
                    continue
                done = got.get((order.po_id, sku), 0)
                limit = line.ordered + line.ordered * tolerance // 10000
                if units < 1 or done + units > limit:
                    with pytest.raises(StockError):
                        desk.receive(order.po_id, sku, units)
                    continue
                if sku not in average:
                    average[sku] = line.unit_cost_cents
                else:
                    before = on_hand[sku]
                    value = Fraction(
                        average[sku] * before + line.unit_cost_cents * units, before + units
                    )
                    average[sku] = _half_even(value)
                on_hand[sku] += units
                got[(order.po_id, sku)] = done + units
                assert desk.receive(order.po_id, sku, units) == max(0, line.ordered - done - units)
            elif op == "close":
                if _status(order, got, closed) == "received":
                    with pytest.raises(StockroomError):
                        desk.close(order.po_id)
                else:
                    desk.close(order.po_id)
                    closed.add(order.po_id)
            else:
                sku = rng.choice(skus)
                if on_hand[sku] > 0:
                    units = rng.randrange(1, on_hand[sku] + 1)
                    inv.reserve(sku, units)
                    inv.ship(sku, units)
                    on_hand[sku] -= units
            assert inv.snapshot() == {sku: (on_hand[sku], 0) for sku in skus}
            for sku in skus:
                assert desk.average_cost(sku) == average.get(sku)
            for order in orders:
                assert desk.status(order.po_id) == _status(order, got, closed)
                expected_open = {
                    line.sku: line.ordered - got.get((order.po_id, line.sku), 0)
                    for line in order.lines
                    if line.ordered > got.get((order.po_id, line.sku), 0)
                }
                assert desk.outstanding(order.po_id) == (
                    {} if order.po_id in closed else expected_open
                )
                for line in order.lines:
                    assert desk.received(order.po_id, line.sku) == got.get(
                        (order.po_id, line.sku), 0
                    )
            as_of = d(3, 1) + timedelta(days=rng.randrange(0, 8))
            late = [
                o
                for o in orders
                if o.expected < as_of and _status(o, got, closed) in ("open", "partial")
            ]
            assert desk.overdue(as_of) == [
                o.po_id for o in sorted(late, key=lambda o: (o.expected, o.po_id))
            ]


def test_cli_receiving_report(tmp_path: Path) -> None:
    (tmp_path / "stock.csv").write_text("sku,on_hand,reserved\nMUG-001,4,1\nTEA-010,0,0\n")
    (tmp_path / "pos.csv").write_text(POS)
    (tmp_path / "receipts.csv").write_text(RECEIPTS)
    args = (
        "receive", "--stock", "stock.csv", "--pos", "pos.csv", "--receipts", "receipts.csv",
        "--as-of", "2024-03-02",
    )  # fmt: skip
    done = run(*args, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "P-10  Acme  2024-03-01  partial",
        "  MUG-001  received 7 of 10",
        "  TEA-010  received 27 of 25",
        "P-11  Bolt Ltd  2024-03-04  received",
        "  MUG-001  received 6 of 6",
        "P-12  Cove  2024-02-28  open",
        "  PEN-100  received 0 of 2",
        "MUG-001  on hand 17  average $10.04",
        "TEA-010  on hand 27  average $3.33",
        "overdue: P-12, P-10",
    ]
    (tmp_path / "receipts.csv").write_text("po_id,sku,quantity\n")
    done = run(*args[:-1], "2024-02-28", cwd=tmp_path, env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "})
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-2:] == ["  PEN-100  received 0 of 2", "overdue: none"]


def test_cli_errors_and_tolerance_setting(tmp_path: Path) -> None:
    (tmp_path / "stock.csv").write_text("sku,on_hand,reserved\nMUG-001,4,1\n")
    (tmp_path / "pos.csv").write_text(POS)
    receipts = tmp_path / "receipts.csv"
    args = (
        "receive", "--stock", "stock.csv", "--pos", "pos.csv", "--receipts", "receipts.csv",
        "--as-of", "2024-03-02",
    )  # fmt: skip
    receipts.write_text("po_id,sku,quantity\nP-10,MUG-001,2\nP-10,TEA-010,28\n")
    done = run(*args, cwd=tmp_path)
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error: ")
    done = run(*args, cwd=tmp_path, env={"STOCKROOM_OVER_RECEIPT_BP": "1200"})
    assert done.returncode == 0, done.stderr
    assert "  TEA-010  received 28 of 25" in done.stdout.splitlines()
    done = run(*args, cwd=tmp_path, env={"STOCKROOM_OVER_RECEIPT_BP": "-1"})
    assert done.returncode == 1 and done.stdout == ""
    for text in (
        "po_id,sku,quantity\nP-99,MUG-001,1\n",
        "po_id,sku,quantity\nP-12,MUG-001,1\n",
        "po_id,sku,quantity\nP-12,PEN-100,0\n",
    ):
        receipts.write_text(text)
        done = run(*args, cwd=tmp_path)
        assert done.returncode == 1 and done.stdout == "", text
        assert done.stderr.startswith("stockroom: error: ")
    receipts.write_text("po_id,sku,quantity\n")
    (tmp_path / "pos.csv").write_text(POS + "P-12,Cove Ltd,2024-02-28,NB-200,1,1.00\n")
    done = run(*args, cwd=tmp_path)
    assert done.returncode == 1 and done.stdout == ""
