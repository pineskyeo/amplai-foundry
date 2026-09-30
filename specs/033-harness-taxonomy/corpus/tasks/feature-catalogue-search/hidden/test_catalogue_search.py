from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from stockroom.catalogue import search
from stockroom.models import Item
from stockroom.textutil import words

ROOT = Path(__file__).resolve().parents[3]


def item(sku: str, name: str, *tags: str) -> Item:
    return Item(sku, name, 100, tuple(tags), 0)


def skus(found: list[Item]) -> list[str]:
    return [i.sku for i in found]


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


def test_words() -> None:
    assert words("Blue Mug, 2x") == ["blue", "mug", "2x"]
    assert words("NB-200") == ["nb", "200"]
    assert words("naïve") == ["na", "ve"]
    assert words("") == [] and words("  ,;- ") == []
    assert words("A5/B6_c7") == ["a5", "b6", "c7"]
    assert words("100G") == ["100g"]
    assert words("Ünder Über") == ["nder", "ber"]
    assert isinstance(words("x"), list)


CATALOGUE = [
    item("MUG-001", "Blue Mug", "kitchen", "gift"),
    item("MUG-002", "Red Mug", "kitchen"),
    item("TEA-010", "Green Tea 100g", "food", "tea"),
    item("TEA-011", "Black Tea 100g", "food", "tea"),
    item("PEN-100", "Fountain Pen", "office", "gift"),
    item("NB-200", "Notebook A5", "office"),
]


def test_prefix_matching_over_name_sku_and_tags() -> None:
    assert skus(search(CATALOGUE, "mug")) == ["MUG-001", "MUG-002"]
    assert skus(search(CATALOGUE, "MU")) == ["MUG-001", "MUG-002"]
    assert skus(search(CATALOGUE, "kitch")) == ["MUG-001", "MUG-002"]
    assert skus(search(CATALOGUE, "foun")) == ["PEN-100"]
    assert skus(search(CATALOGUE, "a5")) == ["NB-200"]
    assert skus(search(CATALOGUE, "a")) == ["NB-200"]
    assert skus(search(CATALOGUE, "20")) == ["NB-200"]  # a SKU word: "200"
    assert skus(search(CATALOGUE, "nb-200")) == ["NB-200"]
    assert skus(search(CATALOGUE, "ug")) == []  # prefixes only, never infixes
    assert skus(search(CATALOGUE, "ea")) == []
    assert skus(search(CATALOGUE, "100g")) == ["TEA-010", "TEA-011"]


def test_all_words_must_match() -> None:
    assert skus(search(CATALOGUE, "gift mug")) == ["MUG-001"]
    assert skus(search(CATALOGUE, "mug, GIFT!")) == ["MUG-001"]
    assert skus(search(CATALOGUE, "gift office")) == ["PEN-100"]
    assert skus(search(CATALOGUE, "tea mug")) == []
    assert skus(search(CATALOGUE, "red kitchen mug")) == ["MUG-002"]


def test_ranking_exact_then_name_then_sku() -> None:
    cups = [
        item("POT-001", "Tea Pot"),
        item("CUP-002", "Teacup"),
        item("CUP-003", "Mug", "tea"),
        item("CUP-001", "Teacup", "tea"),
    ]
    # exact: POT-001 1, CUP-001 1 (tag), CUP-003 1 (tag), CUP-002 0
    # name prefix: POT-001 1, CUP-001 1, CUP-003 0, CUP-002 1
    assert skus(search(cups, "tea")) == ["CUP-001", "POT-001", "CUP-003", "CUP-002"]
    assert skus(search(CATALOGUE, "100")) == ["PEN-100", "TEA-010", "TEA-011"]
    assert skus(search(CATALOGUE, "gift")) == ["MUG-001", "PEN-100"]
    by_sku = [item("ZZ-100", "Item"), item("AA-200", "Item"), item("MM-300", "Item")]
    assert skus(search(by_sku, "item")) == ["AA-200", "MM-300", "ZZ-100"]


def test_repeated_query_words_count_each_time() -> None:
    pair = [item("AAA-001", "Teapot Mug"), item("BBB-002", "Tea Mugs")]
    # query "tea tea mug": in "Tea Mugs" the word tea is an exact word (twice in the query) and
    # mug is only a prefix; in "Teapot Mug" mug is exact and tea is only a prefix.
    # exact counts: BBB-002 2, AAA-001 1; both have 3 name-prefix matches.
    assert skus(search(pair, "tea tea mug")) == ["BBB-002", "AAA-001"]
    # once in the query each has exact count 1 and 2 name-prefix matches: the SKU decides
    assert skus(search(pair, "tea mug")) == ["AAA-001", "BBB-002"]
    trio = [
        item("AAA-001", "Sea Mug", "tea"),
        item("BBB-001", "Tea Tea"),
        item("CCC-001", "Teapot", "tea"),
    ]
    assert skus(search(trio, "tea")) == ["BBB-001", "CCC-001", "AAA-001"]
    assert skus(search(trio, "tea tea")) == ["BBB-001", "CCC-001", "AAA-001"]


def test_full_ties_keep_input_order() -> None:
    twins = [item("TEA-010", "Tea One"), item("TEA-010", "Tea Two"), item("TEA-009", "Tea Zero")]
    found = search(twins, "tea")
    assert [i.name for i in found] == ["Tea Zero", "Tea One", "Tea Two"]
    assert found[1] is twins[0] and found[2] is twins[1]


def test_empty_query_and_no_match() -> None:
    for query in ("", "   ", ",;-", "!!"):
        found = search(CATALOGUE, query)
        assert found == CATALOGUE and found is not CATALOGUE
    assert search(CATALOGUE, "zebra") == []
    assert search([], "mug") == [] and search([], "") == []


def test_input_handling() -> None:
    snapshot = list(CATALOGUE)
    assert skus(search(iter(CATALOGUE), "mug")) == ["MUG-001", "MUG-002"]
    assert skus(search((i for i in CATALOGUE), "")) == skus(CATALOGUE)
    assert skus(search(tuple(CATALOGUE), "tea")) == ["TEA-010", "TEA-011"]
    search(CATALOGUE, "mug")
    assert CATALOGUE == snapshot
    out = search(CATALOGUE, "mug")
    out.clear()
    assert len(search(CATALOGUE, "mug")) == 2


def listed(done: subprocess.CompletedProcess[str]) -> list[str]:
    return [line.split()[0] for line in done.stdout.splitlines()[2:]]


def test_cli_search_order() -> None:
    done = run("items", "--file", "data/items.csv", "--search", "100")
    assert done.returncode == 0, done.stderr
    assert listed(done) == ["PEN-100", "TEA-010", "TEA-011"]
    assert done.stdout.splitlines()[0].split() == ["SKU", "Name", "Price"]
    done = run("items", "--file", "data/items.csv", "--search", "gift")
    assert listed(done) == ["CND-007", "MUG-001", "PEN-100"]
    done = run("items", "--file", "data/items.csv", "--search", "home")
    assert listed(done) == ["CND-007", "LMP-030"]
    done = run("items", "--file", "data/items.csv", "--search", "T")
    assert listed(done) == ["TEA-010", "TEA-011"]


def test_cli_search_with_tag_and_no_match() -> None:
    done = run("items", "--file", "data/items.csv", "--tag", "gift", "--search", "mug")
    assert done.returncode == 0 and listed(done) == ["MUG-001"]
    done = run("items", "--file", "data/items.csv", "--tag", "GIFT", "--search", "home")
    assert listed(done) == ["CND-007"]
    done = run("items", "--file", "data/items.csv", "--tag", "tea", "--search", "mug")
    assert done.returncode == 0 and listed(done) == []
    lines = done.stdout.splitlines()
    assert len(lines) == 2 and lines[0].split() == ["SKU", "Name", "Price"]
    assert set(lines[1].replace(" ", "")) == {"-"}
    done = run("items", "--file", "data/items.csv", "--search", "zebra")
    assert done.returncode == 0 and len(done.stdout.splitlines()) == 2


def test_cli_without_search_unchanged() -> None:
    plain = run("items", "--file", "data/items.csv")
    assert plain.returncode == 0, plain.stderr
    assert listed(plain) == [
        "MUG-001",
        "MUG-002",
        "TEA-010",
        "TEA-011",
        "PEN-100",
        "NB-200",
        "LMP-030",
        "CND-007",
    ]
    empty = run("items", "--file", "data/items.csv", "--search", "")
    assert empty.returncode == 0 and empty.stdout == plain.stdout
    tagged = run("items", "--file", "data/items.csv", "--tag", "TEA")
    assert listed(tagged) == ["TEA-010", "TEA-011"]
