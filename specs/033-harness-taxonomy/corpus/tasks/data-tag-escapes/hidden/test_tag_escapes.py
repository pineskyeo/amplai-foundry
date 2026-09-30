from __future__ import annotations

import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from stockroom.csvio import format_tags, parse_tags, read_items, write_items
from stockroom.errors import ParseError
from stockroom.models import Item

ROOT = Path(__file__).resolve().parents[3]
B = "\\"


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


def test_existing_behaviour_of_parse_tags() -> None:
    assert parse_tags("Kitchen; gift") == ("kitchen", "gift")
    assert parse_tags("") == ()
    assert parse_tags("a;;b;") == ("a", "b")
    assert parse_tags(" Gift ; ;Tea ") == ("gift", "tea")
    assert parse_tags("a;a;A") == ("a", "a", "a")
    assert parse_tags("Blue Mug;\tTEA\t") == ("blue mug", "tea")
    assert parse_tags("  ") == ()


def test_escaped_separators_and_backslashes() -> None:
    assert parse_tags(f"gift{B};  wrap;Home") == ("gift;  wrap", "home")
    assert parse_tags(f"gift{B}; wrap;Home") == ("gift; wrap", "home")
    assert parse_tags(f"a{B}{B}b") == (f"a{B}b",)
    assert parse_tags(f"a{B}{B};b") == (f"a{B}", "b")
    assert parse_tags(f"{B};") == (";",)
    assert parse_tags(f"{B}{B}{B};") == (f"{B};",)
    assert parse_tags(f"a {B}; b") == ("a ; b",)
    assert parse_tags(f" {B}; ") == (";",)
    assert parse_tags(f"{B};X") == (";x",)
    assert parse_tags(f"{B}{B}{B}{B}") == (f"{B}{B}",)
    assert parse_tags(f"x{B}; ;y") == ("x;", "y")
    assert parse_tags(f"{B}; ;{B};") == (";", ";")
    assert parse_tags(f"{B}{B}{B}{B};a") == (f"{B}{B}", "a")
    assert parse_tags(f"Gift{B}{B}Shop;a") == (f"gift{B}shop", "a")


@pytest.mark.parametrize(
    "cell",
    [
        f"a{B}b",
        f"a{B}",
        B,
        f"{B}x",
        f"{B} ",
        f"a{B}{B}{B}",
        f"a;{B}q",
        f"{B}x;fine",
        f"fine;{B}",
        f"{B}{B}{B}",
        f"a{B} b",
        f"{B}\t",
        f"{B}\n",
    ],
)
def test_other_backslashes_are_refused(cell: str) -> None:
    with pytest.raises(ParseError):
        parse_tags(cell)


def test_format_tags() -> None:
    assert format_tags(()) == ""
    assert format_tags(("gift", "home")) == "gift;home"
    assert format_tags(("gift; wrap", "home")) == f"gift{B}; wrap;home"
    assert format_tags((f"a{B}b",)) == f"a{B}{B}b"
    assert format_tags((f"a{B}", "b")) == f"a{B}{B};b"
    assert format_tags((";",)) == f"{B};"
    assert format_tags((f"{B};",)) == f"{B}{B}{B};"
    assert format_tags(("Home", "GIFT")) == "Home;GIFT"
    assert format_tags(iter(["x", "y z"])) == "x;y z"
    assert format_tags(["a", "", "b"]) == "a;;b"


def _tag(rng: random.Random) -> str:
    pieces = list("abc xyz019-é;;\\\\_.")
    while True:
        tag = "".join(rng.choice(pieces) for _ in range(rng.randrange(1, 9)))
        if tag.strip() == tag and tag and tag.lower() == tag:
            return tag


def test_generated_tags_round_trip() -> None:
    rng = random.Random(20261001)
    for _ in range(2000):
        tags = tuple(_tag(rng) for _ in range(rng.randrange(0, 6)))
        cell = format_tags(tags)
        assert parse_tags(cell) == tags, (tags, cell)
        # every ';' that is not escaped is a separator: there are len(tags) - 1 of them
        unescaped = 0
        chars = iter(cell)
        for char in chars:
            if char == B:
                next(chars)
            elif char == ";":
                unescaped += 1
        assert unescaped == max(len(tags) - 1, 0)


def test_generated_cells_are_stable() -> None:
    rng = random.Random(99)
    alphabet = list("aB c;;\\\\\\\\ \t-é")
    checked = 0
    for _ in range(4000):
        cell = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 12)))
        try:
            tags = parse_tags(cell)
        except ParseError:
            continue
        checked += 1
        assert all(tag and tag == tag.strip() for tag in tags), (cell, tags)
        assert parse_tags(format_tags(tags)) == tags, (cell, tags)
    assert checked > 500


def test_catalogue_files_round_trip_tags() -> None:
    items = [
        Item("MUG-001", "Blue Mug", 1250, ("gift; wrap", f"a{B}b"), 5),
        Item("NB-200", "Notebook", 625, (";", f"{B}", f"x{B};", "plain"), 0),
        Item("TEA-010", "Green Tea", 895, (), 3),
    ]
    text = write_items(items)
    lines = text.splitlines()
    assert lines[1] == f"MUG-001,Blue Mug,12.50,gift{B}; wrap;a{B}{B}b,5"
    assert lines[2] == f"NB-200,Notebook,6.25,{B};;{B}{B};x{B}{B}{B};;plain,0"
    assert lines[3] == "TEA-010,Green Tea,8.95,,3"
    assert read_items(text) == items
    rng = random.Random(5)
    for _ in range(200):
        generated = [
            Item(
                f"GEN-{100 + n}",
                "Thing " + str(n),
                rng.randrange(0, 99999),
                tuple(_tag(rng) for _ in range(rng.randrange(0, 5))),
                rng.randrange(0, 9),
            )
            for n in range(rng.randrange(1, 6))
        ]
        assert read_items(write_items(generated)) == generated
    bad = "sku,name,price,tags,reorder_level\nMUG-001,Mug,1.00,a" + B + "b,0\n"
    with pytest.raises(ParseError):
        read_items(bad)


def test_command_line_with_escaped_tags(tmp_path: Path) -> None:
    items = [
        Item("MUG-001", "Blue Mug", 1250, ("gift; wrap", "home"), 5),
        Item("MUG-002", "Red Mug", 1250, (f"a{B}b", "home", "home"), 5),
        Item("TEA-010", "Green Tea", 895, ("gift; wrap", "tea"), 3),
        Item("TEA-011", "Black Tea", 740, (";", "tea", "Tea"), 3),
    ]
    path = tmp_path / "items.csv"
    path.write_text(write_items(items))
    done = run("items", "--file", str(path), "--tag", "gift; wrap")
    assert done.returncode == 0, done.stderr
    assert [line.split()[0] for line in done.stdout.splitlines()[2:]] == ["MUG-001", "TEA-010"]
    done = run("items", "--file", str(path), "--tag", f"A{B}B")
    assert [line.split()[0] for line in done.stdout.splitlines()[2:]] == ["MUG-002"]
    done = run("items", "--file", str(path), "--tag", ";")
    assert [line.split()[0] for line in done.stdout.splitlines()[2:]] == ["TEA-011"]
    done = run("tags", "--file", str(path))
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        f"2 gift{B}; wrap",
        "2 home",
        "2 tea",
        f"1 {B};",
        f"1 a{B}{B}b",
    ]
    empty = tmp_path / "none.csv"
    empty.write_text(write_items([Item("MUG-001", "Mug", 100)]))
    done = run("tags", "--file", str(empty))
    assert done.returncode == 0 and done.stdout == ""
    bad = tmp_path / "bad.csv"
    bad.write_text("sku,name,price,tags,reorder_level\nMUG-001,Mug,1.00,x" + B + "y,0\n")
    for command in (("tags", "--file", str(bad)), ("items", "--file", str(bad))):
        done = run(*command)
        assert done.returncode == 1 and done.stderr.startswith("stockroom: error:")
        assert done.stdout == ""
