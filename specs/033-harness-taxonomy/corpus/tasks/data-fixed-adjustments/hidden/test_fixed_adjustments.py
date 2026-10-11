from __future__ import annotations

import os
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from stockroom.errors import ParseError, StockError
from stockroom.fixedwidth import (
    Adjustment,
    apply_adjustments,
    format_adjustment,
    format_adjustments,
    parse_adjustments,
)

ROOT = Path(__file__).resolve().parents[3]


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


def rec(sku: str, qty: str, day: str, reason: str = "") -> str:
    return sku.ljust(10) + qty + day + reason.ljust(20)


def error_at(text: str) -> str:
    with pytest.raises(ParseError) as caught:
        parse_adjustments(text)
    return str(caught.value)


def test_zoned_quantities_with_overpunched_signs() -> None:
    cases = {
        "0000012{": 120,
        "0000012A": 121,
        "0000012B": 122,
        "0000012E": 125,
        "0000012I": 129,
        "0000012}": -120,
        "0000012J": -121,
        "0000012N": -125,
        "0000012R": -129,
        "00000123": 123,
        "00000010": 10,
        "0000000A": 1,
        "0000000J": -1,
        "9999999I": 99_999_999,
        "9999999R": -99_999_999,
        "99999999": 99_999_999,
        "1234567}": -12_345_670,
    }
    for field, value in cases.items():
        (adjustment,) = parse_adjustments(rec("MUG-001", field, "20240309", "count"))
        assert adjustment == Adjustment("MUG-001", value, date(2024, 3, 9), "count"), field
    (adjustment,) = parse_adjustments(rec("ABCD-12345", "0000000A", "00010101"))
    assert adjustment == Adjustment("ABCD-12345", 1, date(1, 1, 1), "")
    assert adjustment.reason == ""


def test_line_ends_bom_padding_and_blank_lines() -> None:
    a = rec("MUG-001", "0000001{", "20240309", "damaged")
    b = rec("TEA-010", "0000000E", "20240310", "  found in back")
    c = rec("NB-200", "0000004}", "20240311", "Café \u2028 ünï")
    expected = [
        Adjustment("MUG-001", 10, date(2024, 3, 9), "damaged"),
        Adjustment("TEA-010", 5, date(2024, 3, 10), "  found in back"),
        Adjustment("NB-200", -40, date(2024, 3, 11), "Café \u2028 ünï"),
    ]
    assert parse_adjustments(a + "\n" + b + "\n" + c + "\n") == expected
    assert parse_adjustments(a + "\r\n" + b + "\r" + c) == expected
    assert parse_adjustments(a + "\r" + b + "\r\n" + c + "\r") == expected
    assert parse_adjustments("\ufeff" + a + "\n" + b + "\n" + c) == expected
    # trailing spaces may be missing: shorter lines are padded with spaces
    short = "\n".join(line.rstrip(" ") for line in (a, b, c))
    assert parse_adjustments(short) == expected
    assert parse_adjustments(rec("MUG-001", "0000001{", "20240309").rstrip()) == [
        Adjustment("MUG-001", 10, date(2024, 3, 9), "")
    ]
    # empty and space-only lines (of any length) are skipped
    spaced = "\n" + a + "\n\n" + " " * 60 + "\r\n   \r" + b + "\n\n" + c + "\n\n"
    assert parse_adjustments(spaced) == expected
    assert parse_adjustments("") == []
    assert parse_adjustments("\ufeff") == []
    assert parse_adjustments("\n\r\n   \r") == []
    # characters other than CR and LF never end a line
    assert parse_adjustments(c) == [expected[2]]
    assert parse_adjustments(rec("NB-200", "0000004}", "20240311", "a\u2029b\u2028c")) == [
        Adjustment("NB-200", -40, date(2024, 3, 11), "a\u2029b\u2028c")
    ]
    # a U+FEFF after the start is data
    assert parse_adjustments(rec("NB-200", "0000004}", "20240311", "x\ufeff")) == [
        Adjustment("NB-200", -40, date(2024, 3, 11), "x\ufeff")
    ]


def test_errors_name_line_and_column() -> None:
    good = rec("MUG-001", "0000001{", "20240309", "ok")
    cases = [
        (good + "x", 1, 47),
        (good + " ", 1, 47),
        (rec(" MUG-001", "0000001{", "20240309"), 1, 1),
        (rec("mug-001", "0000001{", "20240309"), 1, 1),
        (rec("MUG-01", "0000001{", "20240309"), 1, 1),
        (rec("MUG-\u0661\u0662\u0663", "0000001{", "20240309"), 1, 1),
        ("MUG-001", 1, 11),
        (rec("MUG-001", "000 001{", "20240309"), 1, 14),
        (rec("MUG-001", "00000\u00a01{", "20240309"), 1, 16),
        (rec("MUG-001", "0000\u06611{", "20240309"), 1, 15),
        (rec("MUG-001", "00000\u00b21{", "20240309"), 1, 16),
        (rec("MUG-001", "0_00001{", "20240309"), 1, 12),
        (rec("MUG-001", "0000001\u00b2", "20240309"), 1, 18),
        (rec("MUG-001", "0000001{", "2024030\u00b2"), 1, 26),
        (rec("MUG-001", "+000001{", "20240309"), 1, 11),
        (rec("MUG-001", "0000001a", "20240309"), 1, 18),
        (rec("MUG-001", "0000001S", "20240309"), 1, 18),
        (rec("MUG-001", "0000001 ", "20240309"), 1, 18),
        (rec("MUG-001", "0000000{", "20240309"), 1, 11),
        (rec("MUG-001", "0000000}", "20240309"), 1, 11),
        (rec("MUG-001", "00000000", "20240309"), 1, 11),
        (rec("MUG-001", "0000001{", "2024-3-9"), 1, 23),
        (rec("MUG-001", "0000001{", "2024030"), 1, 26),
        (rec("MUG-001", "0000001{", "20230229"), 1, 19),
        (rec("MUG-001", "0000001{", "20241301"), 1, 19),
        (rec("MUG-001", "0000001{", "00000101"), 1, 19),
        (rec("MUG-001", "0000001{", "2024\uff10309"), 1, 23),
        (rec("MUG-001", "0000001{", "20240309", "tab\there"), 1, 30),
        (rec("MUG-001", "0000001{", "20240309", "nel\x85x"), 1, 30),
        (rec("MUG-001", "0000001{", "20240309", "ff\x0cx"), 1, 29),
        (rec("MUG-001", "0000001{", "20240309", "\x0b"), 1, 27),
        (good + "\n\n" + rec("MUG-001", "0000001X", "20240309"), 3, 18),
        (good + "\r\n\r\n" + rec("MUG-001", "0000001{", "2024031"), 3, 26),
        (good + "\r\r" + good + "\r" + "MUG-001   0000001{20240309" + "z" * 21, 4, 47),
        ("\ufeff" + rec("MUG-001", "0000001{", "20240309", "a\x07"), 1, 28),
        ("\ufeff\ufeff" + good.rstrip(), 1, 1),
        (good + "\n" + rec("MUG-001", "0000001{", "20240309", "a\u2028b\x0cc") + "\n", 2, 30),
        (rec("MUG-001", "0000001{", "20240309", "a\x85") + "\n" + good, 1, 28),
    ]
    for text, line, column in cases:
        message = error_at(text)
        assert message.startswith(f"line {line}, column {column}:"), (text, message)


def test_format_adjustment() -> None:
    a = Adjustment("MUG-001", 120, date(2024, 3, 9), "count")
    assert format_adjustment(a) == "MUG-001   0000012{20240309count               "
    assert len(format_adjustment(a)) == 46
    assert format_adjustment(Adjustment("TEA-010", -129, date(2024, 3, 9))).startswith(
        "TEA-010   0000012R20240309"
    )
    assert format_adjustment(Adjustment("TEA-010", 5, date(2024, 3, 9))) == (
        "TEA-010   0000000E20240309" + " " * 20
    )
    assert format_adjustment(Adjustment("TEA-010", 3, date(2024, 3, 9))) == (
        "TEA-010   0000000C20240309" + " " * 20
    )
    assert format_adjustment(Adjustment("ABCD-12345", -99_999_999, date(1, 2, 3), "x" * 20)) == (
        "ABCD-12345" + "9999999R" + "00010203" + "x" * 20
    )
    assert format_adjustment(Adjustment("NB-200", 10, date(2024, 1, 1), " lead")) == (
        "NB-200    0000001{20240101 lead               "
    )
    rows = [
        Adjustment("MUG-001", 120, date(2024, 3, 9), "count"),
        Adjustment("TEA-010", -1, date(2024, 3, 10)),
    ]
    assert format_adjustments(rows) == (
        format_adjustment(rows[0]) + "\r\n" + format_adjustment(rows[1]) + "\r\n"
    )
    assert format_adjustments([]) == ""
    assert format_adjustments(iter(rows)) == format_adjustments(rows)
    bad = [
        Adjustment("MUG-001", 0, date(2024, 3, 9)),
        Adjustment("MUG-001", 100_000_000, date(2024, 3, 9)),
        Adjustment("MUG-001", -100_000_000, date(2024, 3, 9)),
        Adjustment("mug-001", 1, date(2024, 3, 9)),
        Adjustment("MUG-\u0661\u0662\u0663", 1, date(2024, 3, 9)),
        Adjustment("MUG-001", 1, date(2024, 3, 9), "x" * 21),
        Adjustment("MUG-001", 1, date(2024, 3, 9), "trailing "),
        Adjustment("MUG-001", 1, date(2024, 3, 9), "tab\there"),
        Adjustment("MUG-001", 1, date(2024, 3, 9), "line\nbreak"),
    ]
    for adjustment in bad:
        with pytest.raises(ValueError):
            format_adjustment(adjustment)


REASON_CHARS = "abcXYZ019 -#,.'\"\\é日\u2028\u00a0\ufeff"


def random_adjustment(rng: random.Random) -> Adjustment:
    letters = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(rng.randint(2, 4)))
    digits = "".join(rng.choice("0123456789") for _ in range(rng.randint(3, 5)))
    size = rng.choice([1, 9, 10, 11, 99, 100, 12345, 99_999_999, rng.randint(1, 99_999_999)])
    delta = size if rng.random() < 0.5 else -size
    day = date(1, 1, 1) + timedelta(days=rng.randint(0, 3_652_058))
    reason = "".join(rng.choice(REASON_CHARS) for _ in range(rng.randint(0, 20))).rstrip(" ")
    return Adjustment(f"{letters}-{digits}", delta, day, reason)


def test_generated_files_round_trip() -> None:
    rng = random.Random(20261011)
    for _ in range(1500):
        rows = [random_adjustment(rng) for _ in range(rng.randint(0, 6))]
        text = format_adjustments(rows)
        assert parse_adjustments(text) == rows
        lines = [format_adjustment(row) for row in rows]
        assert all(len(line) == 46 for line in lines)
        mixed = ""
        for line in lines:
            if rng.random() < 0.5:
                line = line.rstrip(" ")
            mixed += line + rng.choice(["\n", "\r\n", "\r"])
            if rng.random() < 0.2:
                mixed += rng.choice(["\n", " " * rng.randint(1, 50) + "\r\n", "\r"])
        if rng.random() < 0.5:
            mixed = mixed.rstrip("\r\n")
        if rng.random() < 0.3:
            mixed = "\ufeff" + mixed
        parsed = parse_adjustments(mixed)
        assert parsed == rows
        assert format_adjustments(parsed) == text


def test_generated_damage_is_reported_at_its_column() -> None:
    rng = random.Random(4242)
    for _ in range(1500):
        rows = [random_adjustment(rng) for _ in range(rng.randint(1, 5))]
        lines = [format_adjustment(row) for row in rows]
        target = rng.randrange(len(lines))
        column = rng.randint(1, 46)
        line = lines[target]
        if column <= 10:
            bad = rng.choice(["a", "-", "\u0661", "*"])
            line = bad + line[1:] if column == 1 else line[: column - 1] + "!" + line[column:]
            expected = 1
        elif column <= 26:
            bad = rng.choice(["x", " ", "\u0663", "\u00a0", "+", "."])
            line = line[: column - 1] + bad + line[column:]
            expected = column
        else:
            line = line[: column - 1] + rng.choice(["\t", "\x00", "\x85", "\x1f"]) + line[column:]
            expected = column
        lines[target] = line
        blanks = rng.randint(0, 2)
        text = "\n" * blanks + "\r\n".join(lines)
        message = error_at(text)
        prefix = f"line {blanks + target + 1}, column {expected}:"
        assert message.startswith(prefix), (lines[target], message)


def test_apply_adjustments() -> None:
    levels = [("MUG-001", 14, 2), ("TEA-010", 30, 5), ("NB-200", 40, 4)]
    adjustments = [
        Adjustment("TEA-010", -25, date(2024, 3, 10)),
        Adjustment("MUG-001", -12, date(2024, 3, 9)),
        Adjustment("PEN-100", 3, date(2024, 3, 11)),
    ]
    assert apply_adjustments(levels, adjustments) == [
        ("MUG-001", 2, 2),
        ("NB-200", 40, 4),
        ("PEN-100", 3, 0),
        ("TEA-010", 5, 5),
    ]
    assert apply_adjustments(iter(levels), iter(adjustments)) == apply_adjustments(
        levels, adjustments
    )
    assert apply_adjustments(levels, []) == [
        ("MUG-001", 14, 2),
        ("NB-200", 40, 4),
        ("TEA-010", 30, 5),
    ]
    # applied by date, not by position: the receipt dated earlier comes first
    out_of_order = [
        Adjustment("MUG-001", -20, date(2024, 3, 10)),
        Adjustment("MUG-001", 10, date(2024, 3, 9)),
    ]
    assert apply_adjustments(levels, out_of_order) == [
        ("MUG-001", 4, 2),
        ("NB-200", 40, 4),
        ("TEA-010", 30, 5),
    ]
    # equal dates keep the given order
    same_day = [
        Adjustment("MUG-001", 10, date(2024, 3, 9)),
        Adjustment("MUG-001", -20, date(2024, 3, 9)),
    ]
    assert apply_adjustments(levels, same_day)[0] == ("MUG-001", 4, 2)
    with pytest.raises(StockError):
        apply_adjustments(levels, list(reversed(same_day)))
    with pytest.raises(StockError):
        apply_adjustments(levels, [Adjustment("MUG-001", -13, date(2024, 3, 9))])
    with pytest.raises(StockError):
        apply_adjustments(levels, [Adjustment("PEN-100", -1, date(2024, 3, 9))])
    assert apply_adjustments(levels, [Adjustment("MUG-001", -12, date(2024, 3, 9))])[0] == (
        "MUG-001",
        2,
        2,
    )


STOCK = "sku,on_hand,reserved\nMUG-001,14,2\nTEA-010,30,5\nNB-200,40,4\n"


def test_reconcile_command(tmp_path: Path) -> None:
    (tmp_path / "stock.csv").write_text(STOCK, encoding="utf-8")
    log = "".join(
        [
            "\ufeff",
            rec("TEA-010", "0000002E", "20240310", "spoiled") + "\r\n",
            rec("MUG-001", "0000001}", "20240309", "count").rstrip() + "\r",
            "\r\n",
            rec("PEN-100", "0000000C", "20240311", "found") + "\n",
            rec("MUG-001", "0000000B", "20240312", "returned \u2028 box") + "\n",
        ]
    )
    (tmp_path / "adj.dat").write_bytes(log.encode("utf-8"))
    done = run("reconcile", "--stock", "stock.csv", "--file", "adj.dat", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "MUG-001 on_hand=6 reserved=2 available=4",
        "NB-200 on_hand=40 reserved=4 available=36",
        "PEN-100 on_hand=3 reserved=0 available=3",
        "TEA-010 on_hand=55 reserved=5 available=50",
    ]
    done = run(
        "reconcile",
        "--stock",
        "stock.csv",
        "--file",
        "adj.dat",
        "--until",
        "2024-03-11",
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == "MUG-001 on_hand=4 reserved=2 available=2"
    done = run(
        "reconcile",
        "--stock",
        "stock.csv",
        "--file",
        "adj.dat",
        "--until",
        "2024-03-09",
        cwd=tmp_path,
    )
    assert done.stdout.splitlines() == [
        "MUG-001 on_hand=4 reserved=2 available=2",
        "NB-200 on_hand=40 reserved=4 available=36",
        "TEA-010 on_hand=30 reserved=5 available=25",
    ]
    (tmp_path / "empty.dat").write_text("", encoding="utf-8")
    done = run("reconcile", "--stock", "stock.csv", "--file", "empty.dat", cwd=tmp_path)
    assert done.returncode == 0 and done.stdout.splitlines()[0] == (
        "MUG-001 on_hand=14 reserved=2 available=12"
    )
    bad = rec("MUG-001", "0000001{", "20240309") + "\n\n" + rec("MUG-001", "0000001Z", "20240309")
    (tmp_path / "bad.dat").write_text(bad, encoding="utf-8")
    done = run("reconcile", "--stock", "stock.csv", "--file", "bad.dat", cwd=tmp_path)
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error: line 3, column 18:")
    short = rec("MUG-001", "0000001M", "20240309")
    (tmp_path / "short.dat").write_text(short, encoding="utf-8")
    done = run("reconcile", "--stock", "stock.csv", "--file", "short.dat", cwd=tmp_path)
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error:")
