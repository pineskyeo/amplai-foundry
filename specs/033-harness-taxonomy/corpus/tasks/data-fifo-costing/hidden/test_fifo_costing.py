from __future__ import annotations

import dataclasses
import itertools
import os
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from stockroom.costing import (
    CostMovement,
    CostRow,
    average_cost,
    fifo_report,
    read_cost_movements,
)
from stockroom.errors import ParseError, StockError

ROOT = Path(__file__).resolve().parents[3]
HEADER = "day,sku,kind,quantity,unit_cost\n"


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=50,
        check=False,
    )


def IN(day: str, sku: str, qty: int, cost: int) -> CostMovement:
    return CostMovement(date.fromisoformat(day), sku, "in", qty, cost)


def OUT(day: str, sku: str, qty: int) -> CostMovement:
    return CostMovement(date.fromisoformat(day), sku, "out", qty, None)


def test_read_cost_movements() -> None:
    text = (
        HEADER
        + "2024-01-03,MUG-001,in,10,1.25\n"
        + " 2024-01-04 ,\tMUG-001 , out , 4 ,\n"
        + "\n"
        + "2024-01-05,TEA-010,in,007,12\r\n"
        + ",,,,\n"
        + "2024-02-01,TEA-010,in,1,0.05\n"
        + "2024-02-02,TEA-010,in,2,1000.00\n"
    )
    assert read_cost_movements(text) == [
        IN("2024-01-03", "MUG-001", 10, 125),
        OUT("2024-01-04", "MUG-001", 4),
        IN("2024-01-05", "TEA-010", 7, 1200),
        IN("2024-02-01", "TEA-010", 1, 5),
        IN("2024-02-02", "TEA-010", 2, 100000),
    ]
    assert read_cost_movements(HEADER) == []
    assert read_cost_movements(" day , sku,kind,quantity,unit_cost \n") == []
    assert OUT("2024-01-04", "MUG-001", 4).unit_cost_cents is None
    bad_rows = [
        "20240103,MUG-001,in,1,1.00",
        "2024-1-3,MUG-001,in,1,1.00",
        "2024-02-30,MUG-001,in,1,1.00",
        "2024-W01-3,MUG-001,in,1,1.00",
        "2024-01-03,mug-001,in,1,1.00",
        "2024-01-03,MUG-\u0661\u0662\u0663,in,1,1.00",
        "2024-01-03,MUG-001,IN,1,1.00",
        "2024-01-03,MUG-001,move,1,1.00",
        "2024-01-03,MUG-001,in,0,1.00",
        "2024-01-03,MUG-001,in,-1,1.00",
        "2024-01-03,MUG-001,in,+1,1.00",
        "2024-01-03,MUG-001,in,1.5,1.00",
        "2024-01-03,MUG-001,in,\u0663,1.00",
        "2024-01-03,MUG-001,in,\u00b2,1.00",
        "2024-01-03,MUG-001,in,1_0,1.00",
        "2024-01-03,MUG-001,in,\u00a07,1.00",
        '2024-01-03,MUG-001,in,"7\n",1.00',
        "2024-01-03,MUG-001,in,1,1_000.00",
        "2024-01-03,MUG-001,in,1,\u00a01.00",
        '2024-01-03,MUG-001,in,1,"1.00\n"',
        '"2024-01-03\n",MUG-001,in,1,1.00',
        "2024-01-03,MUG-001,in,1,12.5",
        "2024-01-03,MUG-001,in,1,12.505",
        "2024-01-03,MUG-001,in,1,$12.50",
        '2024-01-03,MUG-001,in,1,"1,250.00"',
        "2024-01-03,MUG-001,in,1,-1.00",
        "2024-01-03,MUG-001,in,1,.50",
        "2024-01-03,MUG-001,in,1,1.\u0665\u0660",
        "2024-01-03,MUG-001,in,1,1\xa0000.00",
        "2024-01-03,MUG-001,in,1,",
        "2024-01-03,MUG-001,out,1,1.00",
        "2024-01-03,MUG-001,in,1",
        "2024-01-03,MUG-001,in,1,1.00,x",
    ]
    for row in bad_rows:
        with pytest.raises(ParseError):
            read_cost_movements(HEADER + row + "\n")
    for header in ("", "\n", "day,sku,kind,quantity\n", "day,sku,kind,qty,unit_cost\n"):
        with pytest.raises(ParseError):
            read_cost_movements(header + "2024-01-03,MUG-001,in,1,1.00\n")


def row(month, sku, oq, ov, iq, iv, xq, xv, cq, cv) -> CostRow:
    return CostRow(month, sku, oq, ov, iq, iv, xq, xv, cq, cv)


def test_fifo_consumes_the_oldest_layers_first() -> None:
    movements = [
        IN("2024-01-02", "MUG-001", 10, 100),
        IN("2024-01-05", "MUG-001", 5, 130),
        OUT("2024-01-06", "MUG-001", 12),
        IN("2024-01-07", "MUG-001", 4, 90),
        OUT("2024-01-08", "MUG-001", 3),
        OUT("2024-01-09", "MUG-001", 2),
    ]
    # 12 out: 10 x 1.00 + 2 x 1.30 = 12.60; 3 out: 3 x 1.30 = 3.90; 2 out: 2 x 0.90 = 1.80
    assert fifo_report(movements) == [
        row("2024-01", "MUG-001", 0, 0, 19, 1000 + 650 + 360, 17, 1260 + 390 + 180, 2, 180)
    ]
    assert fifo_report(iter(reversed(movements))) == fifo_report(movements)
    assert fifo_report([]) == []
    # a layer used up exactly, then the next one
    exact = [
        IN("2024-03-01", "TEA-010", 2, 500),
        IN("2024-03-01", "TEA-010", 3, 700),
        OUT("2024-03-02", "TEA-010", 2),
        OUT("2024-03-03", "TEA-010", 1),
    ]
    assert fifo_report(exact) == [row("2024-03", "TEA-010", 0, 0, 5, 3100, 3, 1700, 2, 1400)]


def test_every_month_and_sku_carries_over() -> None:
    movements = [
        IN("2023-11-20", "MUG-001", 4, 250),
        OUT("2023-11-25", "MUG-001", 1),
        IN("2024-02-10", "TEA-010", 3, 333),
        OUT("2024-02-11", "MUG-001", 3),
        IN("2024-02-12", "AB-100", 1, 1),
        OUT("2024-03-31", "TEA-010", 1),
    ]
    assert fifo_report(movements) == [
        row("2023-11", "MUG-001", 0, 0, 4, 1000, 1, 250, 3, 750),
        row("2023-12", "MUG-001", 3, 750, 0, 0, 0, 0, 3, 750),
        row("2024-01", "MUG-001", 3, 750, 0, 0, 0, 0, 3, 750),
        row("2024-02", "AB-100", 0, 0, 1, 1, 0, 0, 1, 1),
        row("2024-02", "MUG-001", 3, 750, 0, 0, 3, 750, 0, 0),
        row("2024-02", "TEA-010", 0, 0, 3, 999, 0, 0, 3, 999),
        row("2024-03", "AB-100", 1, 1, 0, 0, 0, 0, 1, 1),
        row("2024-03", "MUG-001", 0, 0, 0, 0, 0, 0, 0, 0),
        row("2024-03", "TEA-010", 3, 999, 0, 0, 1, 333, 2, 666),
    ]


def test_same_day_order_and_shortage() -> None:
    ok = [IN("2024-01-05", "MUG-001", 2, 100), OUT("2024-01-05", "MUG-001", 2)]
    assert fifo_report(ok)[0].closing_qty == 0
    with pytest.raises(StockError) as caught:
        fifo_report(list(reversed(ok)))
    assert "2024-01-05 MUG-001: short by 2" in str(caught.value)
    later_in = [
        IN("2024-01-05", "MUG-001", 2, 100),
        OUT("2024-01-06", "MUG-001", 5),
        IN("2024-01-07", "MUG-001", 10, 100),
    ]
    with pytest.raises(StockError) as caught:
        fifo_report(later_in)
    assert "2024-01-06 MUG-001: short by 3" in str(caught.value)
    with pytest.raises(StockError) as caught:
        fifo_report([OUT("2024-02-01", "TEA-010", 1)])
    assert "2024-02-01 TEA-010: short by 1" in str(caught.value)
    # order by day first: an out written before an earlier-dated in is fine
    moved = [OUT("2024-01-06", "MUG-001", 1), IN("2024-01-05", "MUG-001", 1, 100)]
    assert fifo_report(moved)[0].out_value == 100


def test_average_cost_rounds_half_to_even() -> None:
    assert average_cost(0, 0) is None
    assert average_cost(2, 25) == 12
    assert average_cost(2, 75) == 38
    assert average_cost(2, 125) == 62
    assert average_cost(2, 135) == 68
    assert average_cost(4, 10) == 2
    assert average_cost(4, 14) == 4
    assert average_cost(3, 100) == 33
    assert average_cost(3, 200) == 67
    assert average_cost(8, 4) == 0
    assert average_cost(8, 12) == 2
    assert average_cost(1, 999) == 999
    assert average_cost(6, 999) == 166
    assert average_cost(10**12, 5 * 10**11 + 1) == 1
    assert average_cost(10**12, 5 * 10**11) == 0
    assert average_cost(10**12, 15 * 10**11) == 2


def model(movements: list[CostMovement]) -> list[tuple]:
    ordered = sorted(movements, key=lambda m: m.day)
    layers: dict[str, list[list[int]]] = {}
    events: dict[tuple[str, str], list[int]] = {}
    for m in ordered:
        key = (f"{m.day.year:04d}-{m.day.month:02d}", m.sku)
        acc = events.setdefault(key, [0, 0, 0, 0])
        stack = layers.setdefault(m.sku, [])
        if m.kind == "in":
            stack.append([m.quantity, m.unit_cost_cents])
            acc[0] += m.quantity
            acc[1] += m.quantity * m.unit_cost_cents
        else:
            left = m.quantity
            while left:
                take = min(left, stack[0][0])
                acc[3] += take * stack[0][1]
                acc[2] += take
                stack[0][0] -= take
                left -= take
                if not stack[0][0]:
                    stack.pop(0)
    first = min(m.day for m in ordered)
    last = max(m.day for m in ordered)
    months = []
    y, mo = first.year, first.month
    while (y, mo) <= (last.year, last.month):
        months.append(f"{y:04d}-{mo:02d}")
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    starts = {}
    for m in ordered:
        key = f"{m.day.year:04d}-{m.day.month:02d}"
        starts[m.sku] = min(starts.get(m.sku, key), key)
    out, have = [], {}
    for month in months:
        for sku in sorted(starts):
            if starts[sku] > month:
                continue
            oq, ov = have.get(sku, (0, 0))
            iq, iv, xq, xv = events.get((month, sku), [0, 0, 0, 0])
            have[sku] = (oq + iq - xq, ov + iv - xv)
            out.append((month, sku, oq, ov, iq, iv, xq, xv, *have[sku]))
    return out


def test_generated_ledgers_follow_fifo() -> None:
    rng = random.Random(31337)
    for _ in range(600):
        stock: dict[str, int] = {}
        movements = []
        start = date(2023, rng.randint(1, 12), rng.randint(1, 28))
        for _ in range(rng.randint(1, 25)):
            day = start + timedelta(days=rng.randint(0, 420))
            sku = rng.choice(["MUG-001", "TEA-010", "AB-100"])
            movements.append((day, sku))
        movements.sort(key=lambda pair: pair[0])
        built = []
        for day, sku in movements:
            have = stock.get(sku, 0)
            if have and rng.random() < 0.45:
                qty = rng.randint(1, have)
                stock[sku] = have - qty
                built.append(CostMovement(day, sku, "out", qty, None))
            else:
                qty = rng.randint(1, 50)
                stock[sku] = have + qty
                built.append(CostMovement(day, sku, "in", qty, rng.randint(0, 99999)))
        report = fifo_report(built)
        # days may come in any order; movements of one day keep their relative order
        days: dict[date, list[CostMovement]] = {}
        for m in built:
            days.setdefault(m.day, []).append(m)
        groups = list(days.values())
        rng.shuffle(groups)
        assert fifo_report([m for group in groups for m in group]) == report
        assert [dataclasses.astuple(r) for r in report] == model(built)
        for r in report:
            assert r.closing_qty == r.opening_qty + r.in_qty - r.out_qty >= 0
            assert r.closing_value == r.opening_value + r.in_value - r.out_value >= 0
        by_sku: dict[str, list[CostRow]] = {}
        for r in report:
            by_sku.setdefault(r.sku, []).append(r)
        for rows in by_sku.values():
            for before, after in itertools.pairwise(rows):
                assert (after.opening_qty, after.opening_value) == (
                    before.closing_qty,
                    before.closing_value,
                )
        text = HEADER + "".join(
            f"{m.day.isoformat()},{m.sku},{m.kind},{m.quantity},"
            + (
                ""
                if m.unit_cost_cents is None
                else f"{m.unit_cost_cents // 100}.{m.unit_cost_cents % 100:02d}"
            )
            + "\n"
            for m in built
        )
        assert read_cost_movements(text) == built


LEDGER = HEADER + (
    "2024-01-03,MUG-001,in,10,1.25\n"
    "2024-01-20,MUG-001,in,10,1.50\n"
    "2024-01-25,MUG-001,out,12,\n"
    "2024-03-02,TEA-010,in,2,0.75\n"
    "2024-03-02,TEA-010,in,1,0.76\n"
    "2024-03-05,MUG-001,out,8,\n"
)


def test_cogs_command(tmp_path: Path) -> None:
    (tmp_path / "moves.csv").write_text(LEDGER, encoding="utf-8")
    done = run("cogs", "--movements", "moves.csv", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "2024-01 MUG-001 open=0/0.00 in=20/27.50 out=12/15.50 close=8/12.00 avg=1.50",
        "2024-02 MUG-001 open=8/12.00 in=0/0.00 out=0/0.00 close=8/12.00 avg=1.50",
        "2024-03 MUG-001 open=8/12.00 in=0/0.00 out=8/12.00 close=0/0.00 avg=-",
        "2024-03 TEA-010 open=0/0.00 in=3/2.26 out=0/0.00 close=3/2.26 avg=0.75",
        "total cogs=27.50",
    ]
    done = run("cogs", "--movements", "moves.csv", "--month", "2024-03", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1] == "total cogs=12.00"
    assert len(done.stdout.splitlines()) == 3
    done = run("cogs", "--movements", "moves.csv", "--month", "2025-01", cwd=tmp_path)
    assert done.stdout.splitlines() == ["total cogs=0.00"]
    big = HEADER + "2024-05-01,LMP-030,in,1000,12.34\n2024-05-02,LMP-030,in,1,0.01\n"
    (tmp_path / "big.csv").write_text(big, encoding="utf-8")
    done = run("cogs", "--movements", "big.csv", cwd=tmp_path)
    assert done.stdout.splitlines()[0] == (
        "2024-05 LMP-030 open=0/0.00 in=1001/12,340.01 out=0/0.00 close=1001/12,340.01 avg=12.33"
    )
    (tmp_path / "empty.csv").write_text(HEADER, encoding="utf-8")
    done = run("cogs", "--movements", "empty.csv", cwd=tmp_path)
    assert done.returncode == 0 and done.stdout == "total cogs=0.00\n"
    (tmp_path / "short.csv").write_text(LEDGER + "2024-03-06,TEA-010,out,4,\n", encoding="utf-8")
    for args in (
        ("--movements", "short.csv"),
        ("--movements", "moves.csv", "--month", "2024-13"),
        ("--movements", "moves.csv", "--month", "2024-3"),
    ):
        done = run("cogs", *args, cwd=tmp_path)
        assert done.returncode == 1 and done.stdout == ""
        assert done.stderr.startswith("stockroom: error:")
    done = run("cogs", "--movements", "short.csv", cwd=tmp_path)
    assert "2024-03-06 TEA-010: short by 1" in done.stderr
