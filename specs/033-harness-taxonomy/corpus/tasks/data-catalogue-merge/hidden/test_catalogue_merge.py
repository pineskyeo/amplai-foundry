from __future__ import annotations

import os
import random
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest
from stockroom.csvio import read_items
from stockroom.errors import ParseError
from stockroom.merge import Conflict, MergeResult, merge_catalogues, name_key, read_renames
from stockroom.models import Item

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


def mk(sku: str, name: str, price: int, tags: tuple[str, ...] = (), reorder: int = 0) -> Item:
    return Item(sku, name, price, tags, reorder)


def test_last_catalogue_wins_field_by_field() -> None:
    a = [
        mk("MUG-001", "Blue Mug", 1250, ("kitchen", "gift"), 5),
        mk("TEA-010", "Green Tea", 895, ("tea",), 10),
        mk("PEN-100", "Pen", 0, (), 2),
    ]
    b = [
        mk("PEN-100", "Pen", 3400, ("Office",), 1),
        mk("NB-200", "Notebook", 625, ("office",), 20),
        mk("MUG-001", "Blue Mug XL", 0, ("Gift", "home", "KITCHEN"), 3),
    ]
    c = [
        mk("TEA-010", "Green Tea", 990, ("TEA", "food", "Food"), 12),
        mk("NB-200", "Notebook", 0, ("OFFICE",), 0),
    ]
    result = merge_catalogues([a, b, c])
    assert isinstance(result, MergeResult)
    assert result.items == (
        mk("MUG-001", "Blue Mug XL", 1250, ("kitchen", "gift", "home"), 5),
        mk("TEA-010", "Green Tea", 990, ("tea", "food"), 12),
        mk("PEN-100", "Pen", 3400, ("Office",), 2),
        mk("NB-200", "Notebook", 625, ("office",), 20),
    )
    assert result.conflicts == (Conflict("MUG-001", "Blue Mug", "Blue Mug XL"),)
    # duplicates inside one catalogue are later appearances too
    dup = merge_catalogues(
        [[mk("MUG-001", "A", 100, ("x", "X", "y")), mk("MUG-001", "B", 200, ("y", "z"), 4)]]
    )
    assert dup.items == (mk("MUG-001", "B", 200, ("x", "y", "z"), 4),)
    assert dup.conflicts == (Conflict("MUG-001", "A", "B"),)
    assert merge_catalogues([]) == MergeResult((), ())
    assert merge_catalogues([[], []]) == MergeResult((), ())
    assert merge_catalogues(iter([iter(a), iter(b), iter(c)])) == result
    # a zero price never replaces a price, but a later price replaces zero
    zero = merge_catalogues(
        [
            [mk("MUG-001", "M", 0)],
            [mk("MUG-001", "M", 0)],
            [mk("MUG-001", "M", 500)],
            [mk("MUG-001", "M", 0)],
        ]
    )
    assert zero.items[0].price_cents == 500


def test_prefer_first_keeps_the_first_name_and_price() -> None:
    a = [mk("MUG-001", "Blue Mug", 1250, ("kitchen",), 5), mk("PEN-100", "Pen", 0, (), 2)]
    b = [mk("PEN-100", "Ink Pen", 3400, ("office",), 1), mk("MUG-001", "Mug", 990, ("gift",), 9)]
    c = [mk("PEN-100", "pen", 3500), mk("MUG-001", "BLUE  MUG", 1, ("Kitchen", "home"))]
    result = merge_catalogues([a, b, c], prefer="first")
    assert result.items == (
        mk("MUG-001", "Blue Mug", 1250, ("kitchen", "gift", "home"), 9),
        mk("PEN-100", "Pen", 3400, ("office",), 2),
    )
    assert result.conflicts == (
        Conflict("PEN-100", "Pen", "Ink Pen"),
        Conflict("MUG-001", "Blue Mug", "Mug"),
    )
    assert merge_catalogues([a, b, c], prefer="last").items == (
        mk("MUG-001", "BLUE  MUG", 1, ("kitchen", "gift", "home"), 9),
        mk("PEN-100", "pen", 3500, ("office",), 2),
    )
    for bad in ("First", "both", ""):
        with pytest.raises(ValueError):
            merge_catalogues([a], prefer=bad)


def test_names_are_compared_after_normalising() -> None:
    assert name_key("Blue Mug") == "blue mug"
    assert name_key("  BLUE\xa0\xa0mug\t") == "blue mug"
    assert name_key("\uff22lue Mug") == "blue mug"
    assert name_key("Straße") == name_key("STRASSE")
    assert name_key("Cafe\u0301") == name_key("Café")
    assert name_key("\ufb01ne") == "fine"
    rng = random.Random(4)
    for _ in range(1000):
        name = "".join(
            rng.choice(["a", "B", " ", "\xa0", "ß", "e\u0301", "\uff21", "\t", "SS"])
            for _ in range(rng.randint(0, 7))
        )
        assert name_key(name) == " ".join(unicodedata.normalize("NFKC", name).split()).casefold()
    names = ["Blue Mug", "blue  mug", "Red Mug", "RED\xa0MUG", "\uff32ed Mug", "Blue Mug"]
    result = merge_catalogues([[mk("MUG-001", name, 100)] for name in names])
    # each appearance is compared with the name in effect before it
    assert result.conflicts == (
        Conflict("MUG-001", "blue  mug", "Red Mug"),
        Conflict("MUG-001", "\uff32ed Mug", "Blue Mug"),
    )
    assert result.items[0].name == "Blue Mug"
    first = merge_catalogues([[mk("MUG-001", name, 100)] for name in names], prefer="first")
    assert first.conflicts == (
        Conflict("MUG-001", "Blue Mug", "Red Mug"),
        Conflict("MUG-001", "Blue Mug", "RED\xa0MUG"),
        Conflict("MUG-001", "Blue Mug", "\uff32ed Mug"),
    )


def test_renamed_skus_merge_into_their_final_sku() -> None:
    renames = {"MUG-001": "MUG-0001", "MUG-0001": "MUG-1001", "TEA-010": "TEA-011"}
    a = [mk("MUG-001", "Blue Mug", 1250, ("kitchen",), 5), mk("TEA-011", "Tea", 900)]
    b = [mk("MUG-0001", "Blue Mug Large", 0, ("gift",), 7), mk("TEA-010", "TEA", 950, ("Food",))]
    result = merge_catalogues([a, b], renames=renames)
    assert result.items == (
        mk("MUG-1001", "Blue Mug Large", 1250, ("kitchen", "gift"), 7),
        mk("TEA-011", "TEA", 950, ("Food",), 0),
    )
    assert result.conflicts == (Conflict("MUG-1001", "Blue Mug", "Blue Mug Large"),)
    # the position is the first appearance of any of the SKU's names
    swapped = merge_catalogues([b, a], renames=renames, prefer="first")
    assert [item.sku for item in swapped.items] == ["MUG-1001", "TEA-011"]
    assert swapped.items[0] == mk("MUG-1001", "Blue Mug Large", 1250, ("gift", "kitchen"), 7)
    assert swapped.conflicts == (Conflict("MUG-1001", "Blue Mug Large", "Blue Mug"),)
    # no renames, an empty mapping and None are the same
    assert merge_catalogues([a, b], renames={}) == merge_catalogues([a, b])
    assert merge_catalogues([a, b], renames=None) == merge_catalogues([a, b])
    # a renamed SKU that never appears changes nothing; a target may also be renamed later
    assert merge_catalogues([[mk("PEN-100", "Pen", 1)]], renames=renames).items == (
        mk("PEN-100", "Pen", 1),
    )
    # every cycle is refused, also one that no item reaches, and a SKU renamed to itself
    for bad in (
        {"MUG-001": "MUG-002", "MUG-002": "MUG-001"},
        {"MUG-001": "MUG-001"},
        {"PEN-100": "PEN-200", "AB-100": "AB-200", "AB-200": "AB-300", "AB-300": "AB-100"},
    ):
        with pytest.raises(ValueError):
            merge_catalogues([[mk("PEN-100", "Pen", 1)]], renames=bad)
        with pytest.raises(ValueError):
            merge_catalogues([], renames=bad)
    text = "old_sku,new_sku\r\nMUG-001,MUG-0001\r\n\r\n MUG-0001 , MUG-1001 \r\nTEA-010,TEA-011\r\n"
    assert read_renames(text) == renames
    assert read_renames(" old_sku , new_sku \n") == {}
    bad_files = [
        "",
        "old,new\nMUG-001,MUG-002\n",
        "old_sku,new_sku\nmug-001,MUG-002\n",
        "old_sku,new_sku\nMUG-001,MUG-02\n",
        "old_sku,new_sku\nMUG-001,MUG-002,x\n",
        "old_sku,new_sku\nMUG-001\n",
        "old_sku,new_sku\nMUG-001,MUG-002\nMUG-001,MUG-002\n",
        "old_sku,new_sku\nMUG-001,MUG-002\nMUG-001,MUG-003\n",
    ]
    for bad_text in bad_files:
        with pytest.raises(ParseError):
            read_renames(bad_text)
    # a file may rename a SKU to itself; the cycle is refused when merging
    assert read_renames("old_sku,new_sku\nMUG-001,MUG-001\n") == {"MUG-001": "MUG-001"}


SKUS = ["MUG-001", "MUG-002", "TEA-010", "PEN-100", "NB-200"]
TARGETS = [*SKUS, "MUG-0001", "XYZ-999"]
NAMES = ["Blue Mug", "blue mug", "BLUE\xa0MUG", "Red Mug", "Tea", "TEA", "Straße", "STRASSE"]
TAGS = ["gift", "Gift", "GIFT", "home", "kitchen", "Kitchen", "ß", "ss", "SS", "x"]


def random_catalogue(rng: random.Random) -> list[Item]:
    return [
        mk(
            rng.choice(SKUS),
            rng.choice(NAMES),
            rng.choice([0, 0, 100, 250, 99999]),
            tuple(rng.choice(TAGS) for _ in range(rng.randint(0, 3))),
            rng.randint(0, 9),
        )
        for _ in range(rng.randint(0, 5))
    ]


def resolve(sku: str, renames: dict[str, str]) -> str:
    while sku in renames:
        sku = renames[sku]
    return sku


def model(
    catalogues: list[list[Item]], prefer: str, renames: dict[str, str]
) -> tuple[tuple[Item, ...], tuple[Conflict, ...]]:
    flat = [
        mk(resolve(item.sku, renames), item.name, item.price_cents, item.tags, item.reorder_level)
        for catalogue in catalogues
        for item in catalogue
    ]
    order: list[str] = []
    for item in flat:
        if item.sku not in order:
            order.append(item.sku)
    items, conflicts = [], []
    for sku in order:
        seen = [item for item in flat if item.sku == sku]
        tags: list[str] = []
        for item in seen:
            for tag in item.tags:
                if tag.casefold() not in [t.casefold() for t in tags]:
                    tags.append(tag)
        prices = [item.price_cents for item in seen if item.price_cents]
        if prefer == "last":
            name, price = seen[-1].name, (prices[-1] if prices else 0)
        else:
            name, price = seen[0].name, (prices[0] if prices else 0)
        items.append(mk(sku, name, price, tuple(tags), max(item.reorder_level for item in seen)))
    current: dict[str, str] = {}
    for item in flat:
        if item.sku in current:
            if name_key(current[item.sku]) != name_key(item.name):
                conflicts.append(Conflict(item.sku, current[item.sku], item.name))
            if prefer == "last":
                current[item.sku] = item.name
        else:
            current[item.sku] = item.name
    return tuple(items), tuple(conflicts)


def test_generated_merges_follow_the_rules_and_associate() -> None:
    rng = random.Random(9090)
    for _ in range(1500):
        prefer = rng.choice(["first", "last"])
        renames: dict[str, str] = {}
        if rng.random() < 0.5:
            # renames only point to SKUs later in TARGETS, so there is no cycle
            for i, sku in enumerate(SKUS):
                if rng.random() < 0.35:
                    renames[sku] = rng.choice(TARGETS[i + 1 :])
        kw = {"prefer": prefer, "renames": renames}
        a, b, c = (random_catalogue(rng) for _ in range(3))
        whole = merge_catalogues([a, b, c], **kw)
        assert (whole.items, whole.conflicts) == model([a, b, c], prefer, renames)
        left = merge_catalogues([list(merge_catalogues([a, b], **kw).items), c], **kw)
        right = merge_catalogues([a, list(merge_catalogues([b, c], **kw).items)], **kw)
        assert left.items == whole.items == right.items
        once = merge_catalogues([whole.items], **kw)
        assert once.items == whole.items and once.conflicts == ()
        assert merge_catalogues([whole.items, whole.items], **kw).items == whole.items
        assert len({item.sku for item in whole.items}) == len(whole.items)
        assert not any(item.sku in renames for item in whole.items)


def write(path: Path, rows: list[str]) -> None:
    path.write_text(
        "sku,name,price,tags,reorder_level\n" + "".join(r + "\n" for r in rows), encoding="utf-8"
    )


def test_merge_command(tmp_path: Path) -> None:
    write(tmp_path / "a.csv", ["MUG-001,Blue Mug,12.50,kitchen;gift,5", "PEN-100,Pen,0.00,,2"])
    write(tmp_path / "b.csv", ["PEN-100,Ink Pen,34.00,office,1", "TEA-010,Green Tea,8.95,tea,10"])
    write(tmp_path / "c.csv", ["MUG-001,blue  MUG,0.00,Home;gift,3"])
    done = run("merge", "--out", "all.csv", "a.csv", "b.csv", "c.csv", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "merged 3 items from 3 files\n"
    assert done.stderr == 'warning: PEN-100: name "Pen" differs from "Ink Pen"\n'
    assert (tmp_path / "all.csv").read_text(encoding="utf-8") == (
        "sku,name,price,tags,reorder_level\n"
        "MUG-001,blue  MUG,12.50,kitchen;gift;home,5\n"
        "PEN-100,Ink Pen,34.00,office,2\n"
        "TEA-010,Green Tea,8.95,tea,10\n"
    )
    assert [
        item.sku for item in read_items((tmp_path / "all.csv").read_text(encoding="utf-8"))
    ] == [
        "MUG-001",
        "PEN-100",
        "TEA-010",
    ]
    done = run("merge", "--prefer", "first", "--out", "first.csv", "b.csv", "a.csv", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "merged 3 items from 2 files\n"
    assert done.stderr == 'warning: PEN-100: name "Ink Pen" differs from "Pen"\n'
    assert (tmp_path / "first.csv").read_text(encoding="utf-8").splitlines()[
        1
    ] == "PEN-100,Ink Pen,34.00,office,2"
    write(tmp_path / "one.csv", ["MUG-001,Blue Mug,12.50,kitchen,5"])
    done = run("merge", "--out", "o.csv", "one.csv", cwd=tmp_path)
    assert done.stdout == "merged 1 item from 1 file\n" and done.stderr == ""
    (tmp_path / "bad.csv").write_text("sku,title\nMUG-001,x\n", encoding="utf-8")
    done = run("merge", "--out", "never.csv", "a.csv", "bad.csv", cwd=tmp_path)
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error:")
    assert not (tmp_path / "never.csv").exists()
    done = run("merge", "--out", "x.csv", cwd=tmp_path)
    assert done.returncode == 2
    (tmp_path / "renames.csv").write_text(
        "old_sku,new_sku\nPEN-100,PEN-0100\nMUG-001,MUG-0001\n", encoding="utf-8"
    )
    done = run(
        "merge", "--renames", "renames.csv", "--out", "r.csv", "a.csv", "b.csv", cwd=tmp_path
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "merged 3 items from 2 files\n"
    assert done.stderr == 'warning: PEN-0100: name "Pen" differs from "Ink Pen"\n'
    assert (tmp_path / "r.csv").read_text(encoding="utf-8").splitlines()[1:] == [
        "MUG-0001,Blue Mug,12.50,kitchen;gift,5",
        "PEN-0100,Ink Pen,34.00,office,2",
        "TEA-010,Green Tea,8.95,tea,10",
    ]
    (tmp_path / "cycle.csv").write_text(
        "old_sku,new_sku\nPEN-100,PEN-0100\nPEN-0100,PEN-100\n", encoding="utf-8"
    )
    (tmp_path / "twice.csv").write_text(
        "old_sku,new_sku\nPEN-100,PEN-0100\nPEN-100,PEN-0200\n", encoding="utf-8"
    )
    for renames in ("cycle.csv", "twice.csv"):
        done = run("merge", "--renames", renames, "--out", "none.csv", "a.csv", cwd=tmp_path)
        assert done.returncode == 1 and done.stdout == "", renames
        assert done.stderr.startswith("stockroom: error:")
        assert not (tmp_path / "none.csv").exists()
    done = run("merge", "--prefer", "both", "--out", "x.csv", "a.csv", cwd=tmp_path)
    assert done.returncode == 2
