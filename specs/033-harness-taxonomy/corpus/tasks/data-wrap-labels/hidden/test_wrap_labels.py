from __future__ import annotations

import os
import random
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from stockroom.textutil import display_width, wrap_text

ROOT = Path(__file__).resolve().parents[3]
ACUTE = "́"


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    base["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=50,
        check=False,
    )


def test_display_width() -> None:
    assert display_width("") == 0
    assert display_width("abc") == 3
    assert display_width("Blue Mug, Large") == 15
    assert display_width("日本語") == 6
    assert display_width("한글") == 4
    assert display_width("Ａ") == 2
    assert display_width("ｱ") == 1
    assert display_width("e" + ACUTE) == 1
    assert display_width("café") == 4
    assert display_width("cafe" + ACUTE) == 4
    assert display_width("a‍b") == 2
    assert display_width("\t") == 0
    assert display_width("😀") == 2
    assert display_width("日本 Tea") == 8
    assert display_width(ACUTE) == 0


def test_wrap_examples_with_plain_text() -> None:
    assert wrap_text("the quick brown fox", 10) == ["the quick", "brown fox"]
    assert wrap_text("the quick brown fox", 9) == ["the quick", "brown fox"]
    assert wrap_text("the quick brown fox", 8) == ["the", "quick", "brown", "fox"]
    assert wrap_text("the quick brown fox", 100) == ["the quick brown fox"]
    assert wrap_text("a  b\tc", 5) == ["a b c"]
    assert wrap_text("a b", 5) == ["a b"]
    assert wrap_text("  padded   words  ", 20) == ["padded words"]
    assert wrap_text("a\r\nb", 5) == ["a", "b"]
    assert wrap_text("", 5) == [""]
    assert wrap_text("   ", 5) == [""]
    assert wrap_text("a\n\nb", 5) == ["a", "", "b"]
    assert wrap_text("a\n", 5) == ["a", ""]
    assert wrap_text("\nb", 5) == ["", "b"]
    assert wrap_text("one two\nthree four five", 9) == ["one two", "three", "four five"]
    for bad in (0, -1):
        with pytest.raises(ValueError):
            wrap_text("x", bad)


def test_wrap_breaks_words_that_are_too_long() -> None:
    assert wrap_text("abcdefghij", 4) == ["abcd", "efgh", "ij"]
    assert wrap_text("xx abcdefghij yy", 4) == ["xx", "abcd", "efgh", "ij", "yy"]
    assert wrap_text("xx abcdefghij yy", 5) == ["xx", "abcde", "fghij", "yy"]
    assert wrap_text("xx abcdefghij yy", 7) == ["xx", "abcdefg", "hij yy"]
    assert wrap_text("abcdefghij", 10) == ["abcdefghij"]
    assert wrap_text("abcdefghij k", 10) == ["abcdefghij", "k"]
    assert wrap_text("abcdefghijk", 10) == ["abcdefghij", "k"]
    assert wrap_text("a abcdefgh", 4) == ["a", "abcd", "efgh"]
    assert wrap_text("MUG-001", 4) == ["MUG-", "001"]


def test_wrap_counts_display_columns() -> None:
    assert wrap_text("日本語のテキスト", 6) == ["日本語", "のテキ", "スト"]
    assert wrap_text("日本語のテキスト", 5) == ["日本", "語の", "テキ", "スト"]
    assert wrap_text("日本語", 1) == ["日", "本", "語"]
    assert wrap_text("Blue 日本 mug", 7) == ["Blue", "日本", "mug"]
    assert wrap_text("Blue 日本 mug", 8) == ["Blue", "日本 mug"]
    assert wrap_text("Blue 日本 mug", 9) == ["Blue 日本", "mug"]
    assert wrap_text("😀😀😀", 4) == ["😀😀", "😀"]
    assert wrap_text("ＡＢＣ d", 8) == ["ＡＢＣ d"]
    assert wrap_text("ＡＢＣ d", 7) == ["ＡＢＣ", "d"]
    cafe = "cafe" + ACUTE
    assert wrap_text(f"{cafe} au lait", 4) == [cafe, "au", "lait"]
    assert wrap_text(f"{cafe} au", 7) == [f"{cafe} au"]
    marked = ("e" + ACUTE) * 3
    assert wrap_text(marked, 2) == [("e" + ACUTE) * 2, "e" + ACUTE]
    assert wrap_text(marked, 3) == [marked]
    assert wrap_text("x" + "日" * 3, 3) == ["x日", "日", "日"]
    assert wrap_text("日a" + "日" * 2, 3) == ["日a", "日", "日"]
    assert wrap_text("日" + ACUTE, 1) == ["日" + ACUTE]
    assert wrap_text("本" + ACUTE + "本" + ACUTE, 1) == ["本" + ACUTE, "本" + ACUTE]


def _model(text: str, width: int) -> list[str]:
    def columns(word: str) -> int:
        return sum(
            0
            if unicodedata.category(c) in ("Mn", "Me", "Cf", "Cc")
            else 2
            if unicodedata.east_asian_width(c) in ("W", "F")
            else 1
            for c in word
        )

    out: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            if columns(word) > width:
                if line:
                    out.append(line)
                pieces, cur = [], ""
                for char in word:
                    if cur and columns(char) > 0 and columns(cur + char) > width:
                        pieces.append(cur)
                        cur = ""
                    cur += char
                pieces.append(cur)
                out.extend(pieces[:-1])
                line = pieces[-1]
            elif line and columns(line) + 1 + columns(word) <= width:
                line += " " + word
            else:
                if line:
                    out.append(line)
                line = word
        out.append(line)
    return out


def test_generated_texts_keep_every_character_and_fit() -> None:
    rng = random.Random(20261001)
    alphabet = ["a", "b", "c", "d", "日", "本", "Ａ", "한", "😀", ACUTE, "‍", " ", " ", "\n", "\t", "-"]
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 40)))
        width = rng.randrange(1, 12)
        lines = wrap_text(text, width)
        assert lines == _model(text, width), (text, width)
        assert "".join("".join(lines).split()) == "".join(text.split())
        assert len(lines) >= text.count("\n") + 1
        for line in lines:
            assert line == line.strip(), (text, width, lines)
            assert "\n" not in line
            if display_width(line) > width:
                visible = [c for c in line if display_width(c) > 0]
                assert len(visible) == 1 and display_width(visible[0]) > width, (text, width, line)
    # a mark stays with the character before it
    assert wrap_text("ab" + ACUTE, 1) == ["a", "b" + ACUTE]


def _items_file(tmp_path: Path) -> Path:
    path = tmp_path / "items.csv"
    path.write_text(
        "sku,name,price,tags,reorder_level\n"
        "MUG-001,Blue Mug,12.50,,0\n"
        "TEA-010,Extra Long Green Tea 100g,8.95,,0\n"
        "TEA-020,日本茶 Tea,5.00,,0\n",
        encoding="utf-8",
    )
    return path


def test_labels_command(tmp_path: Path) -> None:
    path = _items_file(tmp_path)
    done = run("labels", "--file", str(path), "--width", "10")
    assert done.returncode == 0, done.stderr
    assert done.stdout == (
        "+------------+\n"
        "| Blue Mug   |\n"
        "| MUG-001    |\n"
        "| $12.50     |\n"
        "+------------+\n"
        "\n"
        "+------------+\n"
        "| Extra Long |\n"
        "| Green Tea  |\n"
        "| 100g       |\n"
        "| TEA-010    |\n"
        "| $8.95      |\n"
        "+------------+\n"
        "\n"
        "+------------+\n"
        "| 日本茶 Tea |\n"
        "| TEA-020    |\n"
        "| $5.00      |\n"
        "+------------+\n"
    )
    done = run("labels", "--file", str(path), "--width", "4")
    assert done.returncode == 0, done.stderr
    first = done.stdout.split("\n\n")[0]
    assert first.splitlines() == [
        "+------+",
        "| Blue |",
        "| Mug  |",
        "| MUG- |",
        "| 001  |",
        "| $12. |",
        "| 50   |",
        "+------+",
    ]
    last = done.stdout.split("\n\n")[2].splitlines()
    assert last[:5] == ["+------+", "| 日本 |", "| 茶   |", "| Tea  |", "| TEA- |"]
    default = run("labels", "--file", str(path))
    assert default.returncode == 0
    assert default.stdout.splitlines()[0] == "+" + "-" * 22 + "+"
    assert default.stdout.splitlines()[1] == "| Blue Mug" + " " * 12 + " |"
    symbol = run("labels", "--file", str(path), "--width", "10", env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "})
    assert "| EUR 12.50  |" in symbol.stdout.splitlines()
    empty = tmp_path / "empty.csv"
    empty.write_text("sku,name,price,tags,reorder_level\n", encoding="utf-8")
    done = run("labels", "--file", str(empty))
    assert done.returncode == 0 and done.stdout == ""
    for bad in ("0", "-3"):
        done = run("labels", "--file", str(path), "--width", bad)
        assert done.returncode == 1 and done.stdout == ""
        assert done.stderr.startswith("stockroom: error:")
