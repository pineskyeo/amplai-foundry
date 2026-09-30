import os
import random
from pathlib import Path

import pytest

from stockroom.cli import main
from stockroom.csvio import read_items, write_items
from stockroom.errors import ParseError
from stockroom.models import Item


@pytest.fixture(autouse=True)
def _clean_stockroom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("STOCKROOM_"):
            monkeypatch.delenv(name)


HEADER = "sku,name,price,tags,reorder_level"


def test_names_with_commas_and_quotes_round_trip() -> None:
    items = [
        Item("MUG-001", "Mug, large", 1250, ("kitchen",), 5),
        Item("MUG-002", 'The "Big" Mug', 1300, (), 0),
        Item("TEA-010", 'Tea, "green", 100g', 895, ("food", "tea"), 10),
        Item("NB-200", "Plain", 625, (), 0),
        Item("PEN-100", '"', 100, (), 1),
        Item("PEN-101", ",", 100, (), 1),
        Item("PEN-102", "a,b,c,d", 100, (), 1),
    ]
    assert read_items(write_items(items)) == items


def test_names_with_line_breaks_round_trip() -> None:
    items = [
        Item("MUG-001", "Two\nLines", 1250, (), 5),
        Item("MUG-002", "Para one\n\nPara two", 1300, ("x",), 0),
        Item("MUG-003", "Ends\nhere, too", 1300, (), 0),
        Item("MUG-004", 'Quote "and"\nbreak', 1300, (), 0),
        Item("NB-200", "After", 625, (), 0),
    ]
    assert read_items(write_items(items)) == items


def test_prices_of_one_thousand_or_more_round_trip() -> None:
    items = [
        Item("MUG-001", "Mug", 100000, (), 5),
        Item("MUG-002", "Mug 2", 123456789, ("a", "b"), 5),
        Item("MUG-003", "Mug 3", 99999, (), 5),
        Item("MUG-004", "Mug 4", 100001, (), 5),
        Item("MUG-005", "Mug, 5", 100050, (), 5),
    ]
    text = write_items(items)
    assert '"1,000.00"' in text
    assert read_items(text) == items


def test_generated_catalogues_round_trip() -> None:
    rng = random.Random(2024)
    alphabet = ["a", "B", "z", "é", "7", " ", ",", '"', "\n", ";", "-", "x y"]
    items = []
    for number in range(300):
        name = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12))).strip()
        if not name:
            name = "n"
        tags = tuple(f"t{rng.randint(0, 9)}" for _ in range(rng.randint(0, 3)))
        items.append(
            Item(
                sku=f"GEN-{100 + number:03d}",
                name=name,
                price_cents=rng.choice([0, 5, 99, 100, 99999, 100000, rng.randint(0, 10**9)]),
                tags=tags,
                reorder_level=rng.randint(0, 50),
            )
        )
    assert read_items(write_items(items)) == items


def test_hand_written_quoted_fields_and_crlf_line_ends() -> None:
    text = (
        f"{HEADER}\r\n"
        'MUG-001,"Blue, Mug","1,250.00",kitchen;gift,5\r\n'
        'NB-200,"He said ""hi""",6.25,,0\r\n'
        "TEA-010,Plain Tea,8.95,food,10\r\n"
    )
    items = read_items(text)
    assert items == [
        Item("MUG-001", "Blue, Mug", 125000, ("kitchen", "gift"), 5),
        Item("NB-200", 'He said "hi"', 625, (), 0),
        Item("TEA-010", "Plain Tea", 895, ("food",), 10),
    ]


def test_blank_lines_are_ignored_and_bad_rows_are_refused() -> None:
    text = f"\n{HEADER}\n\nMUG-001,Mug,1.00,,0\n\n\nNB-200,Book,2.00,,0\n\n"
    assert [item.sku for item in read_items(text)] == ["MUG-001", "NB-200"]
    spaced = " sku , name ,price, tags ,reorder_level \nMUG-001,Mug,1.00,,0\n"
    assert [item.sku for item in read_items(spaced)] == ["MUG-001"]
    for bad in (
        f"{HEADER}\nMUG-001,Blue, Mug,12.50,kitchen,5\n",
        f"{HEADER}\nMUG-001,Mug,12.50,kitchen\n",
        f"{HEADER}\nMUG-001,Mug,12.50,kitchen,5,6\n",
        f"{HEADER}\nMUG-001,Mug,abc,,0\n",
        f"{HEADER}\nMUG-001,Mug,1.00,,x\n",
        f"{HEADER}\nmug-001,Mug,1.00,,0\n",
        "sku,name,price,tags\nMUG-001,Mug,1.00,\n",
        "",
        "\n  \n",
    ):
        with pytest.raises(ParseError):
            read_items(bad)


def test_cli_lists_items_whose_names_contain_commas(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [
        Item("MUG-001", "Mug, large", 125000, ("kitchen",), 5),
        Item("MUG-002", 'The "Big" Mug', 1300, (), 0),
    ]
    path = tmp_path / "items.csv"
    path.write_text(write_items(items))
    assert main(["items", "--file", str(path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 4
    assert "MUG-001" in lines[2] and "Mug, large" in lines[2] and lines[2].endswith("$1,250.00")
    assert 'The "Big" Mug' in lines[3] and lines[3].endswith("$13.00")
    assert main(["items", "--file", str(path), "--tag", "kitchen"]) == 0
    tagged = capsys.readouterr().out.splitlines()
    assert len(tagged) == 3 and "Mug, large" in tagged[2]
