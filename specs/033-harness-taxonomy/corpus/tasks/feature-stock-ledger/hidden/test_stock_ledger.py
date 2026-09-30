from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from stockroom.errors import ParseError, StockError
from stockroom.inventory import Inventory
from stockroom.ledger import Movement, apply_movements, month_end_snapshots, parse_ledger

ROOT = Path(__file__).resolve().parents[3]


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_parse_fields_note_and_comments() -> None:
    text = (
        "# stock ledger\n"
        "\n"
        "2024-03-01 receive MUG-001 10\n"
        "  2024-03-02\treserve   MUG-001  3   web order   77  \n"
        "   # indented comment\n"
        "2024-03-02 ship MUG-001 1 see #12 and # 13\n"
    )
    assert parse_ledger(text) == [
        Movement(date(2024, 3, 1), "receive", "MUG-001", 10, ""),
        Movement(date(2024, 3, 2), "reserve", "MUG-001", 3, "web order   77"),
        Movement(date(2024, 3, 2), "ship", "MUG-001", 1, "see #12 and # 13"),
    ]


def test_parse_keeps_file_order_and_types() -> None:
    moves = parse_ledger("2024-01-05 count NB-200 40\n2024-01-05 receive NB-200 2")
    assert [m.kind for m in moves] == ["count", "receive"]
    assert isinstance(moves[0].day, date) and type(moves[0].quantity) is int
    assert Movement(date(2024, 1, 1), "ship", "MUG-001", 1).note == ""
    assert parse_ledger("") == [] and parse_ledger("# nothing\n\n") == []


@pytest.mark.parametrize(
    "line",
    [
        "2024-03-01 receive MUG-001",
        "2024-03-01 receive",
        "2024-13-01 receive MUG-001 1",
        "March-1 receive MUG-001 1",
        "2024-03-01 Receive MUG-001 1",
        "2024-03-01 RECEIVE MUG-001 1",
        "2024-03-01 restock MUG-001 1",
        "2024-03-01 receive mug-001 1",
        "2024-03-01 receive MUG1 1",
        "2024-03-01 receive MUG-001 ten",
    ],
)
def test_parse_rejects_bad_fields(line: str) -> None:
    with pytest.raises(ParseError, match=r"^line 1:"):
        parse_ledger(line)


@pytest.mark.parametrize(
    "quantity", ["-1", "+1", "1.0", "1,000", "1_000", "0x10", "٣", "1e3", "0", "00"]
)
def test_parse_quantity_rules(quantity: str) -> None:
    with pytest.raises(ParseError, match=r"^line 1:"):
        parse_ledger(f"2024-03-01 receive MUG-001 {quantity}")
    if quantity in ("0", "00"):
        assert parse_ledger(f"2024-03-01 count MUG-001 {quantity}")[0].quantity == 0
    else:
        with pytest.raises(ParseError):
            parse_ledger(f"2024-03-01 count MUG-001 {quantity}")
    assert parse_ledger("2024-03-01 ship MUG-001 007")[0].quantity == 7
    assert parse_ledger("2024-03-01 count MUG-001 0")[0].quantity == 0


def test_parse_error_line_numbers() -> None:
    text = "# header\n\n2024-03-01 receive MUG-001 5\n   \n# more\n2024-03-02 ship MUG-001 x\n"
    with pytest.raises(ParseError) as caught:
        parse_ledger(text)
    assert str(caught.value).startswith("line 6:")
    with pytest.raises(ParseError) as caught:
        parse_ledger("\n\n2024-03-01 receive MUG-001\n")
    assert str(caught.value).startswith("line 3:")


def test_parse_rejects_going_back_in_time() -> None:
    text = "2024-03-05 receive MUG-001 1\n# c\n2024-03-04 receive MUG-001 1\n"
    with pytest.raises(ParseError) as caught:
        parse_ledger(text)
    assert str(caught.value).startswith("line 3:")
    assert len(parse_ledger("2024-03-05 receive MUG-001 1\n2024-03-05 ship MUG-001 1")) == 2
    assert len(parse_ledger("2024-03-31 receive MUG-001 1\n2024-04-01 receive MUG-001 1")) == 2
    with pytest.raises(ParseError):
        parse_ledger("2025-01-01 receive MUG-001 1\n2024-12-31 receive MUG-001 1")


def test_inventory_copy_is_independent() -> None:
    original = Inventory.from_levels([("MUG-001", 5, 2)])
    clone = original.copy()
    assert clone is not original and clone.snapshot() == {"MUG-001": (5, 2)}
    clone.receive("MUG-001", 4)
    clone.receive("TEA-010", 1)
    clone.release("MUG-001", 1)
    assert original.snapshot() == {"MUG-001": (5, 2)}
    original.ship("MUG-001", 2)
    assert clone.snapshot() == {"MUG-001": (9, 1), "TEA-010": (1, 0)}


def test_inventory_count() -> None:
    inv = Inventory.from_levels([("MUG-001", 5, 2)])
    inv.count("MUG-001", 9)
    assert inv.snapshot() == {"MUG-001": (9, 2)}
    inv.count("MUG-001", 2)
    assert inv.snapshot() == {"MUG-001": (2, 2)}
    inv.count("TEA-010", 0)
    assert inv.snapshot() == {"MUG-001": (2, 2), "TEA-010": (0, 0)}
    for bad in (1, 0, -1):
        with pytest.raises(StockError):
            inv.count("MUG-001", bad)
    with pytest.raises(StockError):
        inv.count("TEA-010", -3)
    assert inv.snapshot() == {"MUG-001": (2, 2), "TEA-010": (0, 0)}


def movements(text: str) -> list[Movement]:
    return parse_ledger(text)


def test_apply_each_kind() -> None:
    inv = Inventory.from_levels([("MUG-001", 10, 0)])
    apply_movements(
        inv,
        movements(
            "2024-03-01 receive MUG-001 5\n"
            "2024-03-01 receive TEA-010 20\n"
            "2024-03-02 reserve MUG-001 6\n"
            "2024-03-03 release MUG-001 2\n"
            "2024-03-03 ship MUG-001 3\n"
            "2024-03-04 count TEA-010 18\n"
        ),
    )
    assert inv.snapshot() == {"MUG-001": (12, 1), "TEA-010": (18, 0)}
    apply_movements(inv, [])
    assert inv.snapshot() == {"MUG-001": (12, 1), "TEA-010": (18, 0)}


def test_apply_accepts_iterator() -> None:
    inv = Inventory()
    moves = movements("2024-03-01 receive MUG-001 5\n2024-03-02 ship MUG-001 1\n")
    # ship needs reserved units, so the second movement fails; an iterator must still work
    with pytest.raises(StockError):
        apply_movements(inv, iter(moves))
    assert inv.snapshot() == {}
    apply_movements(inv, iter(moves[:1]))
    assert inv.snapshot() == {"MUG-001": (5, 0)}
    apply_movements(inv, (m for m in moves[:1]))
    assert inv.on_hand("MUG-001") == 10


def test_apply_is_all_or_nothing() -> None:
    inv = Inventory.from_levels([("MUG-001", 4, 1), ("TEA-010", 3, 0)])
    before = inv.snapshot()
    moves = movements(
        "2024-03-01 receive MUG-001 6\n"
        "2024-03-01 reserve TEA-010 2\n"
        "2024-03-02 count MUG-001 11\n"
        "2024-03-03 ship TEA-010 3\n"
    )
    with pytest.raises(StockError):
        apply_movements(inv, moves)
    assert inv.snapshot() == before
    assert (inv.on_hand("MUG-001"), inv.reserved("TEA-010")) == (4, 0)
    apply_movements(inv, moves[:3])
    assert inv.snapshot() == {"MUG-001": (11, 1), "TEA-010": (3, 2)}


def test_apply_error_names_day_and_sku() -> None:
    inv = Inventory.from_levels([("MUG-001", 4, 3)])
    moves = movements("2024-03-01 receive MUG-001 1\n2024-07-19 count MUG-001 2\n")
    with pytest.raises(StockError) as caught:
        apply_movements(inv, moves)
    message = str(caught.value)
    assert "2024-07-19" in message and "MUG-001" in message
    with pytest.raises(StockError) as caught:
        apply_movements(Inventory(), movements("2024-05-06 ship TEA-011 1"))
    assert "2024-05-06" in str(caught.value) and "TEA-011" in str(caught.value)


LEDGER = (
    "2024-01-10 receive MUG-001 10\n"
    "2024-01-20 reserve MUG-001 4\n"
    "2024-03-02 ship MUG-001 3\n"
    "2024-03-05 count MUG-001 20\n"
    "2024-03-07 receive TEA-010 5\n"
    "2024-06-30 release MUG-001 1\n"
)


def test_month_end_snapshots() -> None:
    inv = Inventory.from_levels([("NB-200", 7, 0)])
    result = month_end_snapshots(inv, parse_ledger(LEDGER))
    assert list(result) == ["2024-01", "2024-03", "2024-06"]
    assert result["2024-01"] == {"MUG-001": (10, 4), "NB-200": (7, 0)}
    assert result["2024-03"] == {"MUG-001": (20, 1), "NB-200": (7, 0), "TEA-010": (5, 0)}
    assert result["2024-06"] == {"MUG-001": (20, 0), "NB-200": (7, 0), "TEA-010": (5, 0)}
    assert month_end_snapshots(inv, []) == {}


def test_month_end_does_not_touch_input_and_fails_like_apply() -> None:
    inv = Inventory.from_levels([("NB-200", 7, 0)])
    month_end_snapshots(inv, parse_ledger(LEDGER))
    assert inv.snapshot() == {"NB-200": (7, 0)}
    unsorted = [
        Movement(date(2024, 3, 1), "receive", "NB-200", 1),
        Movement(date(2024, 2, 1), "receive", "NB-200", 2),
        Movement(date(2024, 3, 9), "receive", "NB-200", 4),
    ]
    result = month_end_snapshots(inv, unsorted)
    assert list(result) == ["2024-02", "2024-03"]
    assert result["2024-02"] == {"NB-200": (10, 0)}
    assert result["2024-03"] == {"NB-200": (14, 0)}
    bad = [Movement(date(2024, 4, 2), "ship", "NB-200", 1)]
    with pytest.raises(StockError) as caught:
        month_end_snapshots(inv, bad)
    assert "2024-04-02" in str(caught.value) and "NB-200" in str(caught.value)
    assert inv.snapshot() == {"NB-200": (7, 0)}


def test_cli_final_levels(tmp_path: Path) -> None:
    stock = tmp_path / "stock.csv"
    stock.write_text("sku,on_hand,reserved\nTEA-010,30,5\nMUG-001,14,2\n")
    ledger = tmp_path / "ledger.txt"
    ledger.write_text("# movements\n2024-03-01 ship MUG-001 2\n2024-03-02 receive NB-200 8 boxes\n")
    done = run("ledger", "--stock", str(stock), "--ledger", str(ledger))
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["MUG-001 12 0", "NB-200 8 0", "TEA-010 30 5"]
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    done = run("ledger", "--stock", str(stock), "--ledger", str(empty))
    assert done.stdout.splitlines() == ["MUG-001 14 2", "TEA-010 30 5"]


def test_cli_month_ends(tmp_path: Path) -> None:
    stock = tmp_path / "stock.csv"
    stock.write_text("sku,on_hand,reserved\nNB-200,7,0\n")
    ledger = tmp_path / "ledger.txt"
    ledger.write_text(LEDGER)
    done = run("ledger", "--stock", str(stock), "--ledger", str(ledger), "--month-ends")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "== 2024-01",
        "MUG-001 10 4",
        "NB-200 7 0",
        "== 2024-03",
        "MUG-001 20 1",
        "NB-200 7 0",
        "TEA-010 5 0",
        "== 2024-06",
        "MUG-001 20 0",
        "NB-200 7 0",
        "TEA-010 5 0",
    ]


def test_cli_errors(tmp_path: Path) -> None:
    stock = tmp_path / "stock.csv"
    stock.write_text("sku,on_hand,reserved\nMUG-001,4,0\n")
    bad_syntax = tmp_path / "a.txt"
    bad_syntax.write_text("2024-03-01 receive MUG-001 1\n2024-03-02 zap MUG-001 1\n")
    done = run("ledger", "--stock", str(stock), "--ledger", str(bad_syntax))
    assert done.returncode == 1
    assert done.stderr.startswith("stockroom: error: ") and "line 2:" in done.stderr
    bad_stock = tmp_path / "b.txt"
    bad_stock.write_text("2024-03-01 receive MUG-001 1\n2024-03-02 ship MUG-001 1\n")
    for extra in ((), ("--month-ends",)):
        done = run("ledger", "--stock", str(stock), "--ledger", str(bad_stock), *extra)
        assert done.returncode == 1
        assert done.stderr.startswith("stockroom: error: ")
        assert "2024-03-02" in done.stderr and "MUG-001" in done.stderr
