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
from stockroom.csvio import read_returns
from stockroom.errors import ParseError, StockroomError
from stockroom.inventory import Inventory
from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook
from stockroom.returns import (
    REASONS,
    RESTOCKING_FEE_BP,
    Refund,
    ReturnDesk,
    ReturnRequest,
    restocking_fee,
)

ROOT = Path(__file__).resolve().parents[3]
PLACED = date(2024, 5, 10)


def order(order_id: str, status: str, *lines: tuple[str, int, int]) -> Order:
    return Order(
        order_id,
        "Hana Kim",
        PLACED,
        tuple(OrderLine(sku, qty, price) for sku, qty, price in lines),
        status,
    )


def req(
    return_id: str,
    order_id: str,
    sku: str,
    quantity: int,
    day: date = PLACED,
    reason: str = "damaged",
) -> ReturnRequest:
    return ReturnRequest(return_id, order_id, sku, quantity, day, reason)


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


def test_records_and_fee_rounding() -> None:
    assert REASONS == ("unwanted", "damaged", "wrong_item") and RESTOCKING_FEE_BP == 1500
    assert [f.name for f in dataclasses.fields(ReturnRequest)] == [
        "return_id",
        "order_id",
        "sku",
        "quantity",
        "day",
        "reason",
    ]
    assert [f.name for f in dataclasses.fields(Refund)] == [
        "return_id",
        "order_id",
        "sku",
        "quantity",
        "gross_cents",
        "fee_cents",
        "refund_cents",
        "restocked",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        req("R-1", "A-1", "MUG-001", 1).quantity = 2  # type: ignore[misc]
    cases = {0: 0, 1: 0, 3: 0, 4: 1, 10: 2, 30: 4, 50: 8, 70: 10, 110: 16, 1230: 184, 1250: 188}
    for gross, fee in cases.items():
        assert restocking_fee(gross) == fee, gross
    assert restocking_fee(10**18 + 30) == 150000000000000004
    assert restocking_fee(5, 1000) == 0 and restocking_fee(15, 1000) == 2
    assert restocking_fee(25, 1000) == 2 and restocking_fee(35, 1000) == 4
    assert restocking_fee(999, 0) == 0 and restocking_fee(999, 10000) == 999
    for bad in ((-1, 1500), (100, -1)):
        with pytest.raises(ValueError):
            restocking_fee(*bad)


def test_cheapest_lines_are_returned_first() -> None:
    book = OrderBook(
        [
            order(
                "A-1",
                "shipped",
                ("MUG-001", 2, 1250),
                ("TEA-010", 1, 895),
                ("MUG-001", 3, 1000),
                ("MUG-001", 1, 1000),
            ),
            order("A-2", "shipped", ("PEN-100", 2, 1250), ("PEN-100", 1, 1100)),
        ]
    )
    desk = ReturnDesk(book, Inventory())
    first = desk.process(req("R-1", "A-1", "MUG-001", 4))
    assert (first.gross_cents, first.fee_cents, first.refund_cents) == (4000, 0, 4000)
    assert desk.returned("A-1", "MUG-001") == 4
    second = desk.process(req("R-2", "A-1", "MUG-001", 2))
    assert second.gross_cents == 2500
    assert desk.returned("A-1", "MUG-001") == 6
    with pytest.raises(StockroomError):
        desk.process(req("R-3", "A-1", "MUG-001", 1))
    mixed = desk.process(req("R-4", "A-2", "PEN-100", 2))
    assert mixed.gross_cents == 1100 + 1250
    last = desk.process(req("R-5", "A-2", "PEN-100", 1))
    assert last.gross_cents == 1250
    assert desk.returned("A-2", "PEN-100") == 3 and desk.returned("A-1", "TEA-010") == 0


def test_fee_and_restock_depend_on_the_reason() -> None:
    book = OrderBook([order("A-1", "shipped", ("MUG-001", 5, 1230), ("TEA-010", 4, 895))])
    inv = Inventory.from_levels([("MUG-001", 2, 1)])
    desk = ReturnDesk(book, inv)
    unwanted = desk.process(req("R-1", "A-1", "MUG-001", 1, reason="unwanted"))
    assert unwanted == Refund("R-1", "A-1", "MUG-001", 1, 1230, 184, 1046, 1)
    assert inv.snapshot() == {"MUG-001": (3, 1)}
    damaged = desk.process(req("R-2", "A-1", "MUG-001", 2, reason="damaged"))
    assert damaged == Refund("R-2", "A-1", "MUG-001", 2, 2460, 0, 2460, 0)
    assert inv.snapshot() == {"MUG-001": (3, 1)}
    wrong = desk.process(req("R-3", "A-1", "TEA-010", 3, reason="wrong_item"))
    assert wrong == Refund("R-3", "A-1", "TEA-010", 3, 2685, 0, 2685, 3)
    assert inv.snapshot() == {"MUG-001": (3, 1), "TEA-010": (3, 0)}
    big = desk.process(req("R-4", "A-1", "MUG-001", 2, reason="unwanted"))
    assert (big.gross_cents, big.fee_cents, big.refund_cents) == (2460, 369, 2091)
    assert inv.snapshot() == {"MUG-001": (5, 1), "TEA-010": (3, 0)}


def test_window_status_and_validation() -> None:
    book = OrderBook(
        [
            order("A-1", "shipped", ("MUG-001", 9, 100)),
            order("A-2", "paid", ("MUG-001", 1, 100)),
            order("A-3", "open", ("MUG-001", 1, 100)),
            order("A-4", "cancelled", ("MUG-001", 1, 100)),
        ]
    )
    inv = Inventory()
    desk = ReturnDesk(book, inv)
    assert desk.process(req("R-1", "A-1", "MUG-001", 1, PLACED)).quantity == 1
    assert desk.process(req("R-2", "A-1", "MUG-001", 1, date(2024, 6, 9))).quantity == 1
    refused = [
        req("X-1", "A-1", "MUG-001", 1, date(2024, 6, 10)),
        req("X-2", "A-1", "MUG-001", 1, date(2024, 5, 9)),
        req("X-3", "A-2", "MUG-001", 1),
        req("X-4", "A-3", "MUG-001", 1),
        req("X-5", "A-4", "MUG-001", 1),
        req("X-6", "A-9", "MUG-001", 1),
        req("X-7", "A-1", "TEA-010", 1),
        req("X-8", "A-1", "MUG-001", 8),
    ]
    for bad in refused:
        with pytest.raises(StockroomError):
            desk.process(bad)
    for bad in (req("X-9", "A-1", "MUG-001", 1, reason="lost"), req("X-10", "A-1", "MUG-001", 0)):
        with pytest.raises(ParseError):
            desk.process(bad)
    assert desk.returned("A-1", "MUG-001") == 2
    assert [r.return_id for r in desk.refunds()] == ["R-1", "R-2"]
    assert inv.snapshot() == {}
    tight = ReturnDesk(book, Inventory(), window_days=0)
    assert tight.process(req("R-1", "A-1", "MUG-001", 1, PLACED)).gross_cents == 100
    with pytest.raises(StockroomError):
        tight.process(req("R-2", "A-1", "MUG-001", 1, PLACED + timedelta(days=1)))
    wide = ReturnDesk(book, Inventory(), window_days=45)
    assert wide.process(req("R-1", "A-1", "MUG-001", 1, date(2024, 6, 24))).quantity == 1
    with pytest.raises(ValueError):
        ReturnDesk(book, Inventory(), window_days=-1)


def test_return_ids_are_idempotent() -> None:
    book = OrderBook([order("A-1", "shipped", ("MUG-001", 3, 1250), ("TEA-010", 1, 895))])
    inv = Inventory()
    desk = ReturnDesk(book, inv)
    first = desk.process(req("R-1", "A-1", "MUG-001", 3, reason="unwanted"))
    assert inv.snapshot() == {"MUG-001": (3, 0)}
    # the same request again (all three units are back already) is answered, not refused
    again = desk.process(req("R-1", "A-1", "MUG-001", 3, reason="unwanted"))
    assert again == first
    assert inv.snapshot() == {"MUG-001": (3, 0)}
    assert desk.returned("A-1", "MUG-001") == 3 and len(desk.refunds()) == 1
    for other in (
        req("R-1", "A-1", "TEA-010", 1, reason="unwanted"),
        req("R-1", "A-1", "MUG-001", 3, reason="damaged"),
        req("R-1", "A-1", "MUG-001", 3, PLACED + timedelta(days=1), "unwanted"),
    ):
        with pytest.raises(StockroomError):
            desk.process(other)
    assert inv.snapshot() == {"MUG-001": (3, 0)} and len(desk.refunds()) == 1
    views = desk.refunds()
    views.clear()
    assert desk.refunds() == [first]


def test_process_all_is_all_or_nothing() -> None:
    book = OrderBook(
        [
            order("A-1", "shipped", ("MUG-001", 3, 1250), ("TEA-010", 2, 895)),
            order("A-2", "paid", ("MUG-001", 1, 1250)),
        ]
    )
    inv = Inventory.from_levels([("MUG-001", 4, 2)])
    desk = ReturnDesk(book, inv)
    kept = desk.process(req("R-0", "A-1", "TEA-010", 1, reason="wrong_item"))
    batch = [
        req("R-0", "A-1", "TEA-010", 1, reason="wrong_item"),
        req("R-1", "A-1", "MUG-001", 2, reason="unwanted"),
        req("R-2", "A-1", "TEA-010", 1, reason="wrong_item"),
        req("R-3", "A-2", "MUG-001", 1),
    ]
    with pytest.raises(StockroomError):
        desk.process_all(iter(batch))
    assert inv.snapshot() == {"MUG-001": (4, 2), "TEA-010": (1, 0)}
    assert desk.returned("A-1", "MUG-001") == 0 and desk.returned("A-1", "TEA-010") == 1
    assert desk.refunds() == [kept]
    done = desk.process_all(r for r in batch[:3] + batch[1:2])
    assert [r.return_id for r in done] == ["R-0", "R-1", "R-2", "R-1"]
    assert done[0] == kept and done[1] == done[3]
    assert [r.return_id for r in desk.refunds()] == ["R-0", "R-1", "R-2"]
    assert inv.snapshot() == {"MUG-001": (6, 2), "TEA-010": (2, 0)}
    assert desk.process_all([]) == []


def test_read_returns() -> None:
    text = (
        "return_id, order_id ,sku,quantity,day,reason\n"
        " R-1 ,A-1001, MUG-001 , 2 ,2024-01-20 , unwanted \n"
        ",,,,,\n"
        "R-2,A-1001,TEA-010,1,2024-02-07,lost\n"
    )
    assert read_returns(text) == [
        ReturnRequest("R-1", "A-1001", "MUG-001", 2, date(2024, 1, 20), "unwanted"),
        ReturnRequest("R-2", "A-1001", "TEA-010", 1, date(2024, 2, 7), "lost"),
    ]
    assert read_returns("return_id,order_id,sku,quantity,day,reason\n") == []
    header = "return_id,order_id,sku,quantity,day,reason\n"
    for bad in (
        "",
        "return_id,order_id,sku,quantity,day\nR-1,A-1,MUG-001,1,2024-01-20\n",
        header + "R-1,A-1,MUG-001,1,2024-01-20\n",
        header + "R-1,A-1,MUG-001,1,2024-01-20,unwanted,extra\n",
        header + "R-1,A-1,MUG-001,two,2024-01-20,unwanted\n",
        header + "R-1,A-1,MUG-001,1,2024-13-20,unwanted\n",
        header + "R-1,A-1,MUG-001,1,soon,unwanted\n",
    ):
        with pytest.raises(ParseError):
            read_returns(bad)


def _expected(
    book: dict[str, Order],
    left: dict[tuple[str, int], int],
    window: int,
    request: ReturnRequest,
) -> tuple[type[Exception], None] | tuple[None, tuple[Refund, list[tuple[int, int]]]]:
    if request.reason not in ("unwanted", "damaged", "wrong_item") or request.quantity < 1:
        return ParseError, None
    found = book.get(request.order_id)
    if found is None or found.status != "shipped":
        return StockroomError, None
    if not found.placed <= request.day <= found.placed + timedelta(days=window):
        return StockroomError, None
    lines = sorted(
        (line.unit_price_cents, n) for n, line in enumerate(found.lines) if line.sku == request.sku
    )
    if not lines or request.quantity > sum(left[(found.order_id, n)] for _, n in lines):
        return StockroomError, None
    wanted, gross, taken = request.quantity, 0, []
    for price, n in lines:
        units = min(wanted, left[(found.order_id, n)])
        wanted -= units
        gross += units * price
        taken.append((n, units))
    fee = round(Fraction(gross * 15, 100)) if request.reason == "unwanted" else 0
    restocked = 0 if request.reason == "damaged" else request.quantity
    refund = Refund(
        request.return_id,
        request.order_id,
        request.sku,
        request.quantity,
        gross,
        fee,
        gross - fee,
        restocked,
    )
    return None, (refund, taken)


def test_random_returns_follow_the_model() -> None:
    rng = random.Random(1511)
    skus = ["MUG-001", "TEA-010", "NB-200"]
    for _ in range(40):
        orders = [
            Order(
                f"M-{k}",
                "Lena Berg",
                date(2024, 7, 1) + timedelta(days=rng.randrange(0, 5)),
                tuple(
                    OrderLine(rng.choice(skus), rng.randrange(1, 4), rng.choice([0, 30, 70, 1230]))
                    for _ in range(rng.randrange(1, 5))
                ),
                rng.choice(["shipped", "shipped", "shipped", "shipped", "paid"]),
            )
            for k in range(4)
        ]
        window = rng.randrange(0, 6)
        inv = Inventory.from_levels([("MUG-001", 5, 2), ("NB-200", 0, 0), ("TEA-010", 1, 0)])
        desk = ReturnDesk(OrderBook(orders), inv, window_days=window)
        book = {o.order_id: o for o in orders}
        left = {(o.order_id, n): line.quantity for o in orders for n, line in enumerate(o.lines)}
        on_hand = {"MUG-001": 5, "NB-200": 0, "TEA-010": 1}
        accepted: dict[str, tuple[ReturnRequest, Refund]] = {}
        returned: dict[tuple[str, str], int] = {}
        history: list[Refund] = []
        for step in range(30):
            batch = []
            for _ in range(rng.choice([1, 1, 1, 2, 3])):
                if accepted and rng.random() < 0.25:
                    old = accepted[rng.choice(sorted(accepted))][0]
                    if rng.random() < 0.5:
                        old = dataclasses.replace(old, quantity=old.quantity + 1)
                    batch.append(old)
                    continue
                target = rng.choice(sorted(book)) if rng.random() < 0.9 else "M-9"
                found = book.get(target)
                if found is not None and rng.random() < 0.8:
                    sku = rng.choice(found.lines).sku
                else:
                    sku = rng.choice(skus)
                offset = rng.randrange(0, window + 1)
                if rng.random() < 0.15:
                    offset = rng.choice([-1, window + 1])
                batch.append(
                    ReturnRequest(
                        f"Q-{step}-{len(batch)}",
                        target,
                        sku,
                        rng.choice([0, 1, 1, 1, 2, 2, 3]),
                        (found.placed if found else date(2024, 7, 1)) + timedelta(days=offset),
                        rng.choice(["unwanted", "damaged", "wrong_item"] * 3 + ["lost"]),
                    )
                )
            # the model processes the batch on copies and commits only when all succeed
            t_left, t_on_hand = dict(left), dict(on_hand)
            t_accepted, t_returned = dict(accepted), dict(returned)
            t_history = list(history)
            results: list[Refund] = []
            error: type[Exception] | None = None
            for request in batch:
                if request.return_id in t_accepted:
                    if t_accepted[request.return_id][0] == request:
                        results.append(t_accepted[request.return_id][1])
                        continue
                    error = StockroomError
                    break
                error, outcome = _expected(book, t_left, window, request)
                if error is not None or outcome is None:
                    break
                refund, taken = outcome
                for n, units in taken:
                    t_left[(request.order_id, n)] -= units
                key = (request.order_id, request.sku)
                t_returned[key] = t_returned.get(key, 0) + request.quantity
                if refund.restocked:
                    t_on_hand[request.sku] = t_on_hand.get(request.sku, 0) + refund.restocked
                t_accepted[request.return_id] = (request, refund)
                t_history.append(refund)
                results.append(refund)
            if error is not None:
                with pytest.raises(error):
                    if len(batch) == 1 and rng.random() < 0.5:
                        desk.process(batch[0])
                    else:
                        desk.process_all(batch)
            else:
                if len(batch) == 1 and rng.random() < 0.5:
                    assert [desk.process(batch[0])] == results
                else:
                    assert desk.process_all(batch) == results
                left, on_hand, accepted, returned = t_left, t_on_hand, t_accepted, t_returned
                history = t_history
            assert desk.refunds() == history
            assert inv.snapshot() == {
                sku: (units, 2 if sku == "MUG-001" else 0) for sku, units in sorted(on_hand.items())
            }
            for o in orders:
                for sku in skus:
                    assert desk.returned(o.order_id, sku) == returned.get((o.order_id, sku), 0)


def test_cli_returns_report(tmp_path: Path) -> None:
    orders = tmp_path / "orders.csv"
    orders.write_text(
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        "B-1,Hana Kim,2024-05-10,shipped,MUG-001,2,12.30\n"
        "B-1,Hana Kim,2024-05-10,shipped,TEA-010,3,8.95\n"
        "B-1,Hana Kim,2024-05-10,shipped,MUG-001,1,10.00\n"
        "B-2,Omar Diaz,2024-05-20,paid,PEN-100,1,34.00\n"
    )
    stock = tmp_path / "stock.csv"
    stock.write_text("sku,on_hand,reserved\nMUG-001,4,1\nTEA-010,10,0\n")
    returns = tmp_path / "returns.csv"
    returns.write_text(
        "return_id,order_id,sku,quantity,day,reason\n"
        "R-1,B-1,MUG-001,2,2024-05-12,unwanted\n"
        "R-2,B-1,TEA-010,2,2024-06-09,damaged\n"
        "R-1,B-1,MUG-001,2,2024-05-12,unwanted\n"
        "R-3,B-1,TEA-010,1,2024-05-30,wrong_item\n"
    )
    args = ("returns", "--orders", str(orders), "--stock", str(stock), "--returns", str(returns))
    done = run(*args)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "R-1  B-1  MUG-001 x2  gross $22.30  fee $3.34  refund $18.96  restocked 2",
        "R-2  B-1  TEA-010 x2  gross $17.90  fee $0.00  refund $17.90  restocked 0",
        "R-3  B-1  TEA-010 x1  gross $8.95  fee $0.00  refund $8.95  restocked 1",
        "3 returns: gross $49.15  fee $3.34  refund $45.81",
        "MUG-001 on hand 6",
        "TEA-010 on hand 11",
    ]
    returns.write_text(
        "return_id,order_id,sku,quantity,day,reason\nR-7,B-1,TEA-010,1,2024-05-11,damaged\n"
    )
    done = run(*args, env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "})
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "R-7  B-1  TEA-010 x1  gross EUR 8.95  fee EUR 0.00  refund EUR 8.95  restocked 0",
        "1 return: gross EUR 8.95  fee EUR 0.00  refund EUR 8.95",
    ]
    returns.write_text("return_id,order_id,sku,quantity,day,reason\n")
    done = run(*args)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["0 returns: gross $0.00  fee $0.00  refund $0.00"]


@pytest.mark.parametrize(
    "row",
    [
        "R-2,B-2,PEN-100,1,2024-05-21,damaged",
        "R-2,B-1,TEA-010,1,2024-06-10,damaged",
        "R-2,B-1,MUG-001,2,2024-05-12,unwanted",
        "R-1,B-1,TEA-010,1,2024-05-12,damaged",
        "R-2,B-1,TEA-010,1,2024-05-12,broken",
    ],
)
def test_cli_refuses_a_bad_batch(tmp_path: Path, row: str) -> None:
    orders = tmp_path / "orders.csv"
    orders.write_text(
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        "B-1,Hana Kim,2024-05-10,shipped,MUG-001,2,12.30\n"
        "B-1,Hana Kim,2024-05-10,shipped,TEA-010,3,8.95\n"
        "B-2,Omar Diaz,2024-05-20,paid,PEN-100,1,34.00\n"
    )
    stock = tmp_path / "stock.csv"
    stock.write_text("sku,on_hand,reserved\nMUG-001,4,1\n")
    returns = tmp_path / "returns.csv"
    returns.write_text(
        "return_id,order_id,sku,quantity,day,reason\n"
        "R-1,B-1,MUG-001,1,2024-05-12,unwanted\n" + row + "\n"
    )
    done = run("returns", "--orders", str(orders), "--stock", str(stock), "--returns", str(returns))
    assert done.returncode == 1, done.stdout
    assert done.stdout == "" and done.stderr.startswith("stockroom: error: ")
