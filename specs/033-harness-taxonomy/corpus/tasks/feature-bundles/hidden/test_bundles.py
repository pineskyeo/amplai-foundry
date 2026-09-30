from __future__ import annotations

import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest

from stockroom.bundles import flatten, max_buildable, parse_bundles
from stockroom.errors import ParseError
from stockroom.inventory import Inventory

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


def test_parse_basic_and_spacing() -> None:
    text = (
        "KIT-100 = MUG-001 x2 + TEA-010\n"
        "KIT-101=MUG-001   x3+TEA-010+PEN-100 x1\n"
        "\t KIT-102 =   NB-200 \t x12  \n"
        "KIT-103 = LMP-030\n"
    )
    assert parse_bundles(text) == {
        "KIT-100": {"MUG-001": 2, "TEA-010": 1},
        "KIT-101": {"MUG-001": 3, "TEA-010": 1, "PEN-100": 1},
        "KIT-102": {"NB-200": 12},
        "KIT-103": {"LMP-030": 1},
    }
    assert parse_bundles("KIT-100 = KIT-200 x2 + MUG-001") == {"KIT-100": {"KIT-200": 2, "MUG-001": 1}}
    assert parse_bundles("KIT-100 = MUG-001 x007")["KIT-100"] == {"MUG-001": 7}


def test_parse_comments_and_blank_lines() -> None:
    text = "# gift sets\n\n   # indented comment\nKIT-100 = MUG-001\n\n# done\n"
    assert parse_bundles(text) == {"KIT-100": {"MUG-001": 1}}
    assert parse_bundles("") == {}
    assert parse_bundles("# only a comment\n\n") == {}


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("KIT-100 MUG-001 x2", 1),
        ("KIT-100 = MUG-001 = TEA-010", 1),
        ("= MUG-001", 1),
        ("kit-100 = MUG-001", 1),
        ("KIT100 = MUG-001", 1),
        ("KIT-100 = mug-001", 1),
        ("KIT-100 = MUG-001 + tea", 1),
        ("KIT-100 =", 1),
        ("KIT-100 = MUG-001 +", 1),
        ("KIT-100 = + MUG-001", 1),
        ("KIT-100 = MUG-001 + + TEA-010", 1),
        ("KIT-100 = MUG-001 x2 extra", 1),
        ("KIT-100 = MUG-001 x2 x3", 1),
        ("# c\n\nKIT-100 = MUG-001\nKIT-200 = MUG-001 x", 4),
        ("KIT-100 = MUG-001\n\n\nKIT-200 MUG-001", 4),
    ],
)
def test_parse_errors_with_line_numbers(text: str, line: int) -> None:
    with pytest.raises(ParseError) as caught:
        parse_bundles(text)
    assert str(caught.value).startswith(f"line {line}:")


@pytest.mark.parametrize("word", ["x0", "x00", "X2", "x-1", "x1.5", "2", "x", "xx2", "x2x", "x١", "x+1"])
def test_parse_quantity_words(word: str) -> None:
    with pytest.raises(ParseError, match=r"^line 2:"):
        parse_bundles(f"KIT-100 = TEA-010\nKIT-200 = MUG-001 {word}")
    assert parse_bundles("KIT-200 = MUG-001 x1")["KIT-200"] == {"MUG-001": 1}


def test_parse_duplicates() -> None:
    with pytest.raises(ParseError, match=r"^line 1:"):
        parse_bundles("KIT-100 = MUG-001 + TEA-010 + MUG-001 x2")
    with pytest.raises(ParseError, match=r"^line 3:"):
        parse_bundles("KIT-100 = MUG-001\n# x\nKIT-100 = TEA-010")
    with pytest.raises(ParseError, match=r"^line 2:"):
        parse_bundles("KIT-100 = MUG-001\nKIT-100 = MUG-001")
    assert parse_bundles("KIT-100 = MUG-001\nKIT-200 = MUG-001")["KIT-200"] == {"MUG-001": 1}


def test_flatten_nested_and_diamond() -> None:
    bundles = parse_bundles(
        "KIT-100 = MUG-001 x2 + TEA-010\n"
        "KIT-200 = KIT-100 + NB-200 x3\n"
        "KIT-300 = KIT-100 x2 + PEN-100\n"
        "KIT-400 = KIT-100 + KIT-200 x2\n"
        "KIT-500 = KIT-400 x3 + MUG-001\n"
    )
    assert flatten(bundles) == {
        "KIT-100": {"MUG-001": 2, "TEA-010": 1},
        "KIT-200": {"MUG-001": 2, "TEA-010": 1, "NB-200": 3},
        "KIT-300": {"MUG-001": 4, "TEA-010": 2, "PEN-100": 1},
        "KIT-400": {"MUG-001": 6, "TEA-010": 3, "NB-200": 6},
        "KIT-500": {"MUG-001": 19, "TEA-010": 9, "NB-200": 18},
    }
    assert flatten({}) == {}


def test_flatten_forward_reference_and_no_mutation() -> None:
    bundles = {"KIT-200": {"KIT-100": 3, "PEN-100": 1}, "KIT-100": {"MUG-001": 2}}
    snapshot = copy.deepcopy(bundles)
    result = flatten(bundles)
    assert result == {"KIT-200": {"MUG-001": 6, "PEN-100": 1}, "KIT-100": {"MUG-001": 2}}
    assert bundles == snapshot
    result["KIT-100"]["MUG-001"] = 99
    assert bundles == snapshot
    assert flatten(bundles)["KIT-100"] == {"MUG-001": 2}


def test_flatten_cycles() -> None:
    cases = {
        ("KIT-100",): {"KIT-100": {"KIT-100": 1, "MUG-001": 1}},
        ("KIT-100", "KIT-200"): {"KIT-100": {"KIT-200": 1}, "KIT-200": {"KIT-100": 2, "MUG-001": 1}},
        ("KIT-100", "KIT-200", "KIT-300"): {
            "KIT-100": {"KIT-200": 1},
            "KIT-200": {"KIT-300": 1},
            "KIT-300": {"KIT-100": 1},
        },
    }
    for members, bundles in cases.items():
        with pytest.raises(ParseError) as caught:
            flatten(bundles)
        message = str(caught.value)
        assert "cycle" in message
        assert all(sku in message for sku in members)
    parsed = parse_bundles("KIT-100 = KIT-200\nKIT-200 = KIT-100 + MUG-001")
    with pytest.raises(ParseError, match="cycle"):
        flatten(parsed)
    with pytest.raises(ParseError, match="cycle"):
        max_buildable(parsed, "KIT-100", Inventory())


def test_flatten_unreachable_cycle() -> None:
    bundles = {
        "KIT-100": {"MUG-001": 1},
        "KIT-200": {"KIT-300": 1},
        "KIT-300": {"KIT-200": 1, "TEA-010": 1},
    }
    with pytest.raises(ParseError) as caught:
        flatten(bundles)
    assert "cycle" in str(caught.value)
    assert "KIT-200" in str(caught.value) and "KIT-300" in str(caught.value)
    # a cycle is not the same as sharing: the diamond is fine
    diamond = {
        "KIT-100": {"KIT-200": 1, "KIT-300": 1},
        "KIT-200": {"KIT-400": 1},
        "KIT-300": {"KIT-400": 1},
        "KIT-400": {"MUG-001": 1},
    }
    assert flatten(diamond)["KIT-100"] == {"MUG-001": 2}


def test_max_buildable() -> None:
    bundles = parse_bundles(
        "KIT-100 = MUG-001 x2 + TEA-010\n"
        "KIT-200 = KIT-100 + PEN-100\n"
        "KIT-300 = NB-200 x5 + CND-007 x3 + LMP-030\n"
        "KIT-400 = KIT-100 x3 + ZZZ-999\n"
    )
    inv = Inventory.from_levels(
        [
            ("MUG-001", 14, 2),
            ("TEA-010", 30, 5),
            ("PEN-100", 3, 2),
            ("NB-200", 40, 4),
            ("CND-007", 12, 1),
            ("LMP-030", 2, 0),
        ]
    )
    assert max_buildable(bundles, "KIT-100", inv) == 6  # min(12 // 2, 25 // 1)
    assert max_buildable(bundles, "KIT-200", inv) == 1  # PEN-100: 1 available
    assert max_buildable(bundles, "KIT-300", inv) == 2  # min(36 // 5, 11 // 3, 2)
    assert max_buildable(bundles, "KIT-400", inv) == 0  # ZZZ-999 is unknown
    assert max_buildable({"KIT-900": {"MUG-001": 5}}, "KIT-900", Inventory.from_levels([("MUG-001", 4, 0)])) == 0
    assert max_buildable({"KIT-900": {"MUG-001": 5}}, "KIT-900", Inventory.from_levels([("MUG-001", 5, 0)])) == 1
    assert max_buildable({"KIT-900": {"MUG-001": 5}}, "KIT-900", Inventory.from_levels([("MUG-001", 14, 0)])) == 2


def test_max_buildable_errors_and_no_change() -> None:
    bundles = {"KIT-100": {"MUG-001": 2}}
    inv = Inventory.from_levels([("MUG-001", 10, 1)])
    with pytest.raises(ParseError):
        max_buildable(bundles, "KIT-999", inv)
    with pytest.raises(ParseError):
        max_buildable(bundles, "MUG-001", inv)
    assert max_buildable(bundles, "KIT-100", inv) == 4
    assert inv.snapshot() == {"MUG-001": (10, 1)}
    assert bundles == {"KIT-100": {"MUG-001": 2}}


DEFS = """\
# gift sets
KIT-100 = MUG-001 x2 + TEA-010
KIT-200 = KIT-100 + PEN-100 x1
KIT-300 = NB-200 x5 + CND-007 x3 + LMP-030
"""


def test_cli_counts(tmp_path: Path) -> None:
    defs = tmp_path / "defs.txt"
    defs.write_text(DEFS)
    done = run("bundles", "--defs", str(defs), "--stock", "data/stock.csv")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["KIT-100  6", "KIT-200  1", "KIT-300  2"]
    defs.write_text("KIT-900 = MUG-001\nKIT-100 = MUG-002 x3\nKIT-500 = KIT-900 x2\n")
    done = run("bundles", "--defs", str(defs), "--stock", "data/stock.csv")
    assert done.stdout.splitlines() == ["KIT-100  1", "KIT-500  6", "KIT-900  12"]


def test_cli_empty_and_errors(tmp_path: Path) -> None:
    defs = tmp_path / "defs.txt"
    defs.write_text("# nothing here\n\n")
    done = run("bundles", "--defs", str(defs), "--stock", "data/stock.csv")
    assert done.returncode == 0 and done.stdout.splitlines() == ["no bundles"]
    defs.write_text("KIT-100 = MUG-001\nKIT-200 = MUG-001 x0\n")
    done = run("bundles", "--defs", str(defs), "--stock", "data/stock.csv")
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error: ") and "line 2:" in done.stderr
    defs.write_text("KIT-100 = KIT-200\nKIT-200 = KIT-100\n")
    done = run("bundles", "--defs", str(defs), "--stock", "data/stock.csv")
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error: ") and "cycle" in done.stderr
