from __future__ import annotations

import os
import random
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from stockroom.errors import ParseError, StockError
from stockroom.inventory import Inventory
from stockroom.movements import Movement, apply_movements, format_movement, parse_movements

ROOT = Path(__file__).resolve().parents[3]
GOOD = "2024-03-09T14:05:33Z RECEIVE MUG-001 12"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=50,
        check=False,
    )


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_parse_basic_lines_and_time_zones() -> None:
    assert parse_movements(GOOD) == [Movement(utc(2024, 3, 9, 14, 5, 33), "RECEIVE", "MUG-001", 12)]
    assert parse_movements(GOOD)[0].note == ""
    assert parse_movements(GOOD + "\n") == parse_movements(GOOD)
    assert parse_movements("") == []
    assert parse_movements("\n  \n\t\n") == []
    lines = {
        "2024-03-09T16:05:33+02:00 SHIP TEA-010 3": utc(2024, 3, 9, 14, 5, 33),
        "2024-03-09T14:05:33-05:30 SHIP TEA-010 3": utc(2024, 3, 9, 19, 35, 33),
        "2024-03-09T14:05:33+00:00 SHIP TEA-010 3": utc(2024, 3, 9, 14, 5, 33),
        "2024-03-09T14:05:33-00:00 SHIP TEA-010 3": utc(2024, 3, 9, 14, 5, 33),
        "2024-03-09T01:00:00+05:00 SHIP TEA-010 3": utc(2024, 3, 8, 20, 0, 0),
        "2024-01-01T00:30:00+01:00 SHIP TEA-010 3": utc(2023, 12, 31, 23, 30, 0),
        "2024-12-31T23:30:00-02:00 SHIP TEA-010 3": utc(2025, 1, 1, 1, 30, 0),
        "2024-03-09T00:00:00+23:59 SHIP TEA-010 3": utc(2024, 3, 8, 0, 1, 0),
        "2024-03-09T23:59:59Z SHIP TEA-010 3": utc(2024, 3, 9, 23, 59, 59),
    }
    for line, moment in lines.items():
        (movement,) = parse_movements(line)
        assert movement.at == moment and movement.at.utcoffset() == timedelta(0), line
        assert movement.kind == "SHIP" and movement.sku == "TEA-010" and movement.quantity == 3
    kinds = ("RECEIVE", "RESERVE", "RELEASE", "SHIP")
    text = "\n".join(f"2024-03-09T14:05:33Z {kind} MUG-001 1" for kind in kinds)
    assert [m.kind for m in parse_movements(text)] == list(kinds)


def test_parse_whitespace_comments_quantities_and_notes() -> None:
    text = (
        "# morning delivery\n"
        "\n"
        "   # indented comment\n"
        "  2024-03-09T14:05:33Z \t RECEIVE   MUG-001\t+12   \n"
        "2024-03-09T14:06:00Z RESERVE MUG-001 007\r\n"
        "\t\n"
        '2024-03-09T14:07:00Z RELEASE MUG-001 1 "delivery, pallet 7 # not a comment"\n'
        '2024-03-09T14:08:00Z SHIP MUG-001 2\t  "  spaced  "  \n'
    )
    movements = parse_movements(text)
    assert [(m.kind, m.quantity, m.note) for m in movements] == [
        ("RECEIVE", 12, ""),
        ("RESERVE", 7, ""),
        ("RELEASE", 1, "delivery, pallet 7 # not a comment"),
        ("SHIP", 2, "  spaced  "),
    ]
    notes = {
        r'"a\\tb"': "a\\tb",
        r'"a\tb"': "a\tb",
        r'"\\\\"': "\\\\",
        r'"\\"': "\\",
        r'"say \"hi\""': 'say "hi"',
        r'"line1\nline2"': "line1\nline2",
        '""': "",
        '"日本 é ü"': "日本 é ü",
        '"raw\ttab"': "raw\ttab",
        '"x\u2028y\rz"': "x\u2028y\rz",
        '"a\\\\"': "a\\",
    }
    for source, expected in notes.items():
        (movement,) = parse_movements(f"{GOOD} {source}")
        assert movement.note == expected, source
    crlf = parse_movements(f'{GOOD} "a"\r\n{GOOD}\r\n')
    assert [m.note for m in crlf] == ["a", ""]
    (only,) = parse_movements(f'{GOOD} "x\u2028y"\n')
    assert only.note == "x\u2028y"


BAD_LINES = [
    "2024-03-09 14:05:33Z RECEIVE MUG-001 1",
    "2024-03-09T14:05:33 RECEIVE MUG-001 1",
    "2024-03-09T14:05:33+0200 RECEIVE MUG-001 1",
    "2024-03-09T14:05:33.5Z RECEIVE MUG-001 1",
    "2024-03-09T24:00:00Z RECEIVE MUG-001 1",
    "2024-03-09T14:60:00Z RECEIVE MUG-001 1",
    "2024-03-09T14:05:60Z RECEIVE MUG-001 1",
    "2023-02-29T10:00:00Z RECEIVE MUG-001 1",
    "2024-13-09T10:00:00Z RECEIVE MUG-001 1",
    "2024-03-09T14:05:33+24:00 RECEIVE MUG-001 1",
    "2024-03-09T14:05:33+05:60 RECEIVE MUG-001 1",
    "2024-03-09T14:05:33z RECEIVE MUG-001 1",
    "2024-3-9T14:05:33Z RECEIVE MUG-001 1",
    "２０２４-03-09T14:05:33Z RECEIVE MUG-001 1",
    "2024-03-09T14:05:33Z receive MUG-001 1",
    "2024-03-09T14:05:33Z MOVE MUG-001 1",
    "2024-03-09T14:05:33Z RECEIVE mug-001 1",
    "2024-03-09T14:05:33Z RECEIVE MUG-01 1",
    "2024-03-09T14:05:33Z RECEIVE MUG-001 0",
    "2024-03-09T14:05:33Z RECEIVE MUG-001 -1",
    "2024-03-09T14:05:33Z RECEIVE MUG-001 1.5",
    "2024-03-09T14:05:33Z RECEIVE MUG-001 abc",
    "2024-03-09T14:05:33Z RECEIVE MUG-001 +-1",
    "2024-03-09T14:05:33Z RECEIVE MUG-001 +0",
    "2024-03-09T14:05:33Z RECEIVE MUG-001 ١",
    "2024-03-09T14:05:33Z RECEIVE MUG-001",
    "2024-03-09T14:05:33Z RECEIVE",
    "RECEIVE MUG-001 1",
    "2024-03-09T14:05:33Z RECEIVE MUG-001 1 note",
    '2024-03-09T14:05:33Z RECEIVE MUG-001 1 "unterminated',
    '2024-03-09T14:05:33Z RECEIVE MUG-001 1 "bad \\x escape"',
    '2024-03-09T14:05:33Z RECEIVE MUG-001 1 "a" trailing',
    '2024-03-09T14:05:33Z RECEIVE MUG-001 1 "a" "b"',
    '2024-03-09T14:05:33Z RECEIVE MUG-001 1 "a\\"',
    '2024-03-09T14:05:33Z RECEIVE MUG-001 1 "trailing backslash\\',
    '2024-03-09T14:05:33Z RECEIVE MUG-001 1 "a"b',
    "2024-03-09T14:05:33Z RECEIVE MUG-001 1 # trailing comment",
    '2024-03-09T14:05:33Z RECEIVE MUG-001 1"a"',
]


@pytest.mark.parametrize("bad", BAD_LINES)
def test_bad_lines_are_refused_with_their_line_number(bad: str) -> None:
    with pytest.raises(ParseError) as caught:
        parse_movements(f"{GOOD}\n# comment\n{bad}\n")
    assert str(caught.value).startswith("line 3:"), str(caught.value)
    with pytest.raises(ParseError) as caught:
        parse_movements(bad)
    assert str(caught.value).startswith("line 1:")
    with pytest.raises(ParseError) as caught:
        parse_movements(f"\n\n  \n{bad}")
    assert str(caught.value).startswith("line 4:")


def test_format_movement() -> None:
    plus_two = timezone(timedelta(hours=2))
    assert (
        format_movement(Movement(datetime(2024, 3, 9, 16, 5, 33, tzinfo=plus_two), "RECEIVE", "MUG-001", 12, "x"))
        == '2024-03-09T14:05:33Z RECEIVE MUG-001 12 "x"'
    )
    assert format_movement(Movement(utc(2024, 1, 1, 0, 0, 0), "SHIP", "TEA-010", 3)) == "2024-01-01T00:00:00Z SHIP TEA-010 3"
    assert format_movement(Movement(utc(2024, 1, 1, 0, 0, 0), "SHIP", "TEA-010", 3, "")) == "2024-01-01T00:00:00Z SHIP TEA-010 3"
    west = timezone(timedelta(hours=-5, minutes=-30))
    assert format_movement(Movement(datetime(2024, 1, 1, 0, 0, 0, tzinfo=west), "SHIP", "TEA-010", 3)).startswith("2024-01-01T05:30:00Z ")
    note = 'a"b\\c\nd\te'
    text = format_movement(Movement(utc(2024, 1, 1, 0, 0, 0), "RESERVE", "TEA-010", 3, note))
    assert text == '2024-01-01T00:00:00Z RESERVE TEA-010 3 "a\\"b\\\\c\\nd\\te"'
    assert "\n" not in text and "\t" not in text
    with pytest.raises(ValueError):
        format_movement(Movement(datetime(2024, 1, 1, 0, 0, 0), "SHIP", "TEA-010", 3))
    with pytest.raises(ValueError):
        format_movement(Movement(datetime(2024, 1, 1, 0, 0, 0, 5, tzinfo=UTC), "SHIP", "TEA-010", 3))


def _movement(rng: random.Random) -> Movement:
    offset = timezone(timedelta(minutes=rng.choice([0, 60, -60, 120, 330, -330, -720, 840, 1439, -1439])))
    at = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(seconds=rng.randrange(0, 366 * 86400))
    alphabet = list('abc XYZ019 "\\#\n\t\r,=é日\u2028-')
    note = "".join(rng.choice(alphabet) for _ in range(rng.choice([0, 0, 1, 3, 12])))
    return Movement(
        at.astimezone(offset),
        rng.choice(["RECEIVE", "RESERVE", "RELEASE", "SHIP"]),
        rng.choice(["MUG-001", "TEA-010", "AB-0001", "ZZZZ-99999"]),
        rng.choice([1, 2, 9, 10, 99, rng.randrange(1, 10**9)]),
        note,
    )


def test_generated_logs_round_trip() -> None:
    rng = random.Random(20261001)
    for _ in range(600):
        movements = [_movement(rng) for _ in range(rng.randrange(0, 8))]
        lines = [format_movement(m) for m in movements]
        assert all(line.split(" ")[0].endswith("Z") for line in lines)
        text = "\n".join(lines) + ("\n" if lines else "")
        parsed = parse_movements(text)
        assert parsed == movements, (movements, text)
        assert [m.note for m in parsed] == [m.note for m in movements]
        assert all(m.at.utcoffset() == timedelta(0) for m in parsed)
        assert [format_movement(m) for m in parsed] == lines
        assert parse_movements(text.replace("\n", "\r\n")) == movements
        noisy = "# header\n\n" + "\n  # between\n\t\n".join(lines) + "\n"
        assert parse_movements(noisy) == movements


def _stock() -> Inventory:
    return Inventory.from_levels([("MUG-001", 14, 2), ("TEA-010", 30, 5)])


def test_movements_apply_oldest_first() -> None:
    inventory = _stock()
    log = parse_movements(
        "2024-03-09T12:00:00+02:00 SHIP MUG-001 5\n"  # 10:00Z
        "2024-03-09T08:00:00Z RESERVE MUG-001 4\n"
    )
    apply_movements(inventory, log)
    assert inventory.snapshot() == {"MUG-001": (9, 1), "TEA-010": (30, 5)}
    inventory = _stock()
    log = parse_movements(
        "2024-03-09T10:00:00+02:00 RESERVE MUG-001 3\n"  # 08:00Z
        "2024-03-09T07:30:00Z RECEIVE NB-200 10 \"delivery, pallet 7\"\n"
        "2024-03-09T09:00:00-01:00 SHIP MUG-001 4\n"  # 10:00Z
        "2024-03-09T07:00:00Z RELEASE TEA-010 2\n"
    )
    apply_movements(inventory, log)
    assert inventory.snapshot() == {"MUG-001": (10, 1), "NB-200": (10, 0), "TEA-010": (30, 3)}
    same_instant = parse_movements(
        "2024-03-09T08:00:00Z RESERVE MUG-001 4\n2024-03-09T10:00:00+02:00 SHIP MUG-001 5\n"
    )
    inventory = _stock()
    apply_movements(inventory, same_instant)
    assert inventory.snapshot()["MUG-001"] == (9, 1)
    inventory = _stock()
    with pytest.raises(StockError):
        apply_movements(inventory, list(reversed(same_instant)))
    inventory = _stock()
    apply_movements(inventory, iter(same_instant))
    assert inventory.snapshot()["MUG-001"] == (9, 1)
    apply_movements(inventory, [])
    assert inventory.snapshot()["MUG-001"] == (9, 1)
    for line in (
        "2024-03-09T08:00:00Z SHIP MUG-001 3",
        "2024-03-09T08:00:00Z RELEASE MUG-001 3",
        "2024-03-09T08:00:00Z RESERVE MUG-001 15",
        "2024-03-09T08:00:00Z SHIP NB-999 1",
    ):
        with pytest.raises(StockError):
            apply_movements(_stock(), parse_movements(line))


def test_replay_command(tmp_path: Path) -> None:
    stock = tmp_path / "stock.csv"
    stock.write_text("sku,on_hand,reserved\nMUG-001,14,2\nTEA-010,30,5\n")
    log = tmp_path / "moves.log"
    log.write_text(
        "# shift 1\n"
        "2024-03-09T12:00:00+02:00 SHIP MUG-001 5\n"
        "\n"
        '2024-03-09T08:00:00Z RESERVE MUG-001 4 "for order A-1002"\n'
        "2024-03-09T09:00:00Z RECEIVE NB-200 40\n"
    )
    done = run("replay", "--stock", str(stock), "--log", str(log))
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "MUG-001 on_hand=9 reserved=1",
        "NB-200 on_hand=40 reserved=0",
        "TEA-010 on_hand=30 reserved=5",
    ]
    empty = tmp_path / "empty.log"
    empty.write_text("# nothing happened\n")
    done = run("replay", "--stock", str(stock), "--log", str(empty))
    assert done.stdout.splitlines() == ["MUG-001 on_hand=14 reserved=2", "TEA-010 on_hand=30 reserved=5"]
    bad = tmp_path / "bad.log"
    bad.write_text("# x\n\n2024-03-09T08:00:00Z RESERVE MUG-001 four\n")
    done = run("replay", "--stock", str(stock), "--log", str(bad))
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error:") and "line 3" in done.stderr
    short = tmp_path / "short.log"
    short.write_text("2024-03-09T08:00:00Z SHIP MUG-001 3\n")
    done = run("replay", "--stock", str(stock), "--log", str(short))
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error:")
