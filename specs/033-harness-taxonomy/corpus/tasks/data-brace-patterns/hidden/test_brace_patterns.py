from __future__ import annotations

import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
from stockroom.braces import count_braces, escape_braces, expand_braces
from stockroom.errors import ParseError

ROOT = Path(__file__).resolve().parents[3]
BS = "\\"


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


def test_alternatives_and_nesting() -> None:
    assert expand_braces("") == [""]
    assert expand_braces("MUG-001") == ["MUG-001"]
    assert expand_braces("{a,b}") == ["a", "b"]
    assert expand_braces("x{a,b,c}y") == ["xay", "xby", "xcy"]
    assert expand_braces("{a,b}{1,2}") == ["a1", "a2", "b1", "b2"]
    assert expand_braces("{a,b{c,d}}e") == ["ae", "bce", "bde"]
    assert expand_braces("{,s}") == ["", "s"]
    assert expand_braces("mug{,s,}") == ["mug", "mugs", "mug"]
    assert expand_braces("{a,a}") == ["a", "a"]
    assert expand_braces("{{a,b},{c,d}}") == ["a", "b", "c", "d"]
    assert expand_braces("{x{1..2},y}") == ["x1", "x2", "y"]
    assert expand_braces("{1..3,x}") == ["1..3", "x"]
    assert expand_braces("{a..c,}") == ["a..c", ""]
    assert expand_braces("a,b") == ["a,b"]
    assert expand_braces("{TEA,PEN}-{1..9..4}") == [
        "TEA-1",
        "TEA-5",
        "TEA-9",
        "PEN-1",
        "PEN-5",
        "PEN-9",
    ]
    assert expand_braces("{ a , b }") == [" a ", " b "]
    assert expand_braces("café-{日,本}") == ["café-日", "café-本"]
    assert expand_braces("..{a,b}..") == ["..a..", "..b.."]


def test_ranges_steps_and_padding() -> None:
    assert expand_braces("{1..5}") == ["1", "2", "3", "4", "5"]
    assert expand_braces("{5..1}") == ["5", "4", "3", "2", "1"]
    assert expand_braces("{3..3}") == ["3"]
    assert expand_braces("{1..10..3}") == ["1", "4", "7", "10"]
    assert expand_braces("{1..10..4}") == ["1", "5", "9"]
    assert expand_braces("{10..1..4}") == ["10", "6", "2"]
    assert expand_braces("{0..2}") == ["0", "1", "2"]
    assert expand_braces("{00..2}") == ["00", "01", "02"]
    assert expand_braces("{001..3}") == ["001", "002", "003"]
    assert expand_braces("{1..010..3}") == ["001", "004", "007", "010"]
    assert expand_braces("{98..0100}") == ["0098", "0099", "0100"]
    assert expand_braces("{8..11}") == ["8", "9", "10", "11"]
    assert expand_braces("{09..11}") == ["09", "10", "11"]
    assert expand_braces("{1..3..007}") == ["1"]
    assert expand_braces("{1..2..100}") == ["1"]
    assert expand_braces("MUG-{001..003}") == ["MUG-001", "MUG-002", "MUG-003"]
    assert expand_braces("{a..e}") == ["a", "b", "c", "d", "e"]
    assert expand_braces("{a..e..2}") == ["a", "c", "e"]
    assert expand_braces("{e..a..2}") == ["e", "c", "a"]
    assert expand_braces("{X..Z}") == ["X", "Y", "Z"]
    assert expand_braces("{z..z}") == ["z"]
    assert expand_braces("{9..11}{a..b}") == ["9a", "9b", "10a", "10b", "11a", "11b"]
    assert expand_braces("{123456789012345678901..123456789012345678902}") == [
        "123456789012345678901",
        "123456789012345678902",
    ]


def test_escapes_and_literals() -> None:
    assert expand_braces(BS + "{a,b" + BS + "}") == ["{a,b}"]
    assert expand_braces("{a" + BS + ",b,c}") == ["a,b", "c"]
    assert expand_braces(BS + BS + "{a,b}") == [BS + "a", BS + "b"]
    assert expand_braces("{" + BS + "{,x}") == ["{", "x"]
    assert escape_braces("") == ""
    assert (
        escape_braces("a{b}c,d" + BS + "e")
        == "a" + BS + "{b" + BS + "}c" + BS + ",d" + BS + BS + "e"
    )
    assert escape_braces("1..3 . x") == "1..3 . x"
    rng = random.Random(12)
    for _ in range(2000):
        text = "".join(
            rng.choice(["a", "{", "}", ",", BS, ".", " ", "1", "é", "-"])
            for _ in range(rng.randint(0, 10))
        )
        assert expand_braces(escape_braces(text)) == [text]
        assert count_braces(escape_braces(text)) == 1


def column(pattern: str) -> int:
    with pytest.raises(ParseError) as caught:
        expand_braces(pattern)
    message = str(caught.value)
    assert message.startswith("column ")
    with pytest.raises(ParseError) as again:
        count_braces(pattern)
    assert str(again.value) == message
    return int(message.split(":")[0].split()[1])


def test_errors_report_their_column() -> None:
    cases = {
        "{a,b": 1,
        "x{a,b{c,d}": 2,
        "a}": 2,
        "}{": 1,
        "{a}}": 4,
        "{{a,b}": 1,
        "{a,b}}{": 6,
        "{}": 1,
        "x{a}": 2,
        "{a}{b,c}": 1,
        "{b,c}{a}": 6,
        "{x{a}}": 1,
        "{x,{a}}": 4,
        "{x,{y,{z}}}": 7,
        "{1..}": 1,
        "{..3}": 1,
        "{1...3}": 1,
        "{1..3..0}": 1,
        "{1..3..}": 1,
        "{1..3..2..1}": 1,
        "{-1..3}": 1,
        "{1..-3}": 1,
        "{1..a}": 1,
        "{a..Z}": 1,
        "{a..zz}": 1,
        "{aa..z}": 1,
        "{\u0661..3}": 1,
        "{1..\u00b2}": 1,
        "{1..1_0}": 1,
        "{1..3..\u00b2}": 1,
        "{1 ..3}": 1,
        "{é..z}": 1,
        "{1..3..-1}": 1,
        "a" + BS + "b": 2,
        "a" + BS: 2,
        BS + ".": 1,
        "{a,b" + BS + "}": 1,
        "{a" + BS + "x,b}": 3,
        "{a}" + BS + "q": 4,
        "}" + BS + "q": 2,
        "{1" + BS + "..3}": 3,
        "{1.." + BS + ",}": 1,
        "{a" + BS + ",b}": 1,
    }
    for pattern, expected in cases.items():
        assert column(pattern) == expected, pattern


def test_count_without_expanding() -> None:
    assert count_braces("") == 1
    assert count_braces("{a,b{c,d}}e") == 3
    assert count_braces("{1..10..3}") == 4
    assert count_braces("{a..z}{1..1000}{x,y,{1..5}}") == 26 * 1000 * 7
    huge = "{1..1000000}{1..1000000}{1..1000000}"
    assert count_braces(huge) == 10**18
    assert count_braces("{1..1000000000000000000000}") == 10**21
    with pytest.raises(ValueError):
        expand_braces(huge)
    assert len(expand_braces("{1..100000}")) == 100000
    with pytest.raises(ValueError):
        expand_braces("{1..100001}")
    with pytest.raises(ValueError):
        expand_braces("{1..1000}{a,b,{1..1000}}")
    assert len(expand_braces("{1..50}{1..2000}")) == 100000


def node(rng: random.Random, depth: int) -> tuple[str, list[str]]:
    """A random pattern and its expansion, built from the stated rules."""
    kind = rng.choice(
        ["literal", "literal", "alt", "range", "seq"] if depth > 0 else ["literal", "range"]
    )
    if kind == "literal":
        text = "".join(
            rng.choice(["a", "B", "-", ".", "{", "}", ",", BS, "7", " "])
            for _ in range(rng.randint(0, 3))
        )
        return escape_braces(text), [text]
    if kind == "range":
        if rng.random() < 0.6:
            a, b = rng.randint(0, 120), rng.randint(0, 120)
            first, last = str(a), str(b)
            width = 0
            if rng.random() < 0.3:
                first = first.zfill(rng.randint(len(first), 4))
                width = max(len(first), len(last)) if (len(first) > 1 and first[0] == "0") else 0
            values = list(range(a, b + 1)) if a <= b else list(range(a, b - 1, -1))
            step = rng.choice([None, 1, 2, 3, 7, 50])
            if step:
                values = values[::step]
            texts = [str(v).zfill(width) for v in values]
        else:
            letters = rng.choice(["abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"])
            a, b = rng.randrange(26), rng.randrange(26)
            first, last = letters[a], letters[b]
            values = list(range(a, b + 1)) if a <= b else list(range(a, b - 1, -1))
            step = rng.choice([None, 1, 2, 5])
            if step:
                values = values[::step]
            texts = [letters[v] for v in values]
        return "{" + first + ".." + last + (f"..{step}" if step else "") + "}", texts
    if kind == "alt":
        parts = [node(rng, depth - 1) for _ in range(rng.randint(2, 3))]
        if sum(len(ts) for _, ts in parts) > 5000:
            return parts[0]
        return "{" + ",".join(p for p, _ in parts) + "}", [t for _, ts in parts for t in ts]
    left, right = node(rng, depth - 1), node(rng, depth - 1)
    if len(left[1]) * len(right[1]) > 5000:
        return left
    return left[0] + right[0], [x + y for x in left[1] for y in right[1]]


def test_generated_patterns_follow_the_rules() -> None:
    rng = random.Random(777)
    for _ in range(1500):
        pattern, expected = node(rng, rng.randint(0, 3))
        if len(expected) > 5000:
            continue
        assert expand_braces(pattern) == expected, pattern
        assert count_braces(pattern) == len(expected)
        other, other_expected = node(rng, 1)
        if len(expected) * len(other_expected) > 5000:
            continue
        assert expand_braces(pattern + other) == [x + y for x in expected for y in other_expected]


CATALOGUE = (
    "sku,name,price,tags,reorder_level\n"
    "MUG-001,Blue Mug,12.50,kitchen,5\n"
    "MUG-002,Red Mug,12.50,kitchen,5\n"
    "MUG-010,Big Mug,14.00,kitchen,5\n"
    "TEA-010,Green Tea,8.95,tea,10\n"
    "PEN-100,Pen,34.00,office,2\n"
)


def test_expand_command_and_item_match(tmp_path: Path) -> None:
    done = run("expand", "MUG-{001..003},{a,b}", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "MUG-001,a\nMUG-001,b\nMUG-002,a\nMUG-002,b\nMUG-003,a\nMUG-003,b\n"
    done = run("expand", "{1..1000000}{a..z}", "--count", cwd=tmp_path)
    assert done.returncode == 0 and done.stdout == "26000000\n"
    done = run("expand", "{,x}", cwd=tmp_path)
    assert done.stdout == "\nx\n"
    for pattern, message in (("{a", "column 1:"), ("x{a}", "column 2:"), ("{1..200000}", "")):
        done = run("expand", pattern, cwd=tmp_path)
        assert done.returncode == 1 and done.stdout == "", pattern
        assert done.stderr.startswith("stockroom: error: " + message), done.stderr
    done = run("expand", "{a", "--count", cwd=tmp_path)
    assert done.returncode == 1 and done.stderr.startswith("stockroom: error: column 1:")
    (tmp_path / "items.csv").write_text(CATALOGUE, encoding="utf-8")

    def listed(*args: str) -> list[str]:
        done = run("items", "--file", "items.csv", *args, cwd=tmp_path)
        assert done.returncode == 0, done.stderr
        return [line.split()[0] for line in done.stdout.splitlines()[2:]]

    assert listed("--match", "MUG-{001..010}") == ["MUG-001", "MUG-002", "MUG-010"]
    assert listed("--match", "MUG-{001..010..9}") == ["MUG-001", "MUG-010"]
    assert listed("--match", "MUG-{1..10}") == []
    assert listed("--match", "{PEN-100,TEA-0{09..11},XYZ-999}") == ["TEA-010", "PEN-100"]
    assert listed("--match", "MUG-0{01,10}", "--tag", "kitchen") == ["MUG-001", "MUG-010"]
    done = run("items", "--file", "items.csv", "--match", "MUG-{1..3", cwd=tmp_path)
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error: column 5:")
