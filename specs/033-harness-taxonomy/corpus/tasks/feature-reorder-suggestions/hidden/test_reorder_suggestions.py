from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from stockroom.config import DEFAULTS, load_config
from stockroom.errors import ConfigError
from stockroom.inventory import Inventory
from stockroom.models import Item
from stockroom.reorder import suggest_orders

ROOT = Path(__file__).resolve().parents[3]


def item(sku: str, level: int) -> Item:
    return Item(sku, f"Item {sku}", 100, (), level)


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


BASE = ("reorder", "--items", "data/items.csv", "--stock", "data/stock.csv")


def test_selection_and_order() -> None:
    items = [item("TEA-010", 10), item("MUG-001", 5), item("NB-200", 0), item("PEN-100", 2)]
    inv = Inventory.from_levels(
        [("TEA-010", 10, 1), ("MUG-001", 5, 0), ("NB-200", 0, 0), ("PEN-100", 4, 3)]
    )
    # TEA available 9 < 10; MUG available 5 is not below 5; NB has level 0; PEN available 1 < 2
    assert suggest_orders(items, inv) == [("TEA-010", 11), ("PEN-100", 3)]
    assert suggest_orders([], inv) == []
    assert suggest_orders(reversed(items), inv) == [("PEN-100", 3), ("TEA-010", 11)]
    assert suggest_orders(iter(items), inv) == [("TEA-010", 11), ("PEN-100", 3)]


def test_incoming_counts_towards_position() -> None:
    items = [item("TEA-010", 10), item("MUG-001", 5)]
    inv = Inventory.from_levels([("TEA-010", 4, 0), ("MUG-001", 1, 0)])
    assert suggest_orders(items, inv) == [("TEA-010", 16), ("MUG-001", 9)]
    # a position that reaches the level exactly (4 + 6 = 10, 1 + 4 = 5) means no suggestion
    assert suggest_orders(items, inv, incoming={"TEA-010": 6}) == [("MUG-001", 9)]
    assert suggest_orders(items, inv, incoming={"TEA-010": 6, "MUG-001": 4}) == []
    # one unit below the level still gets a suggestion
    assert suggest_orders(items, inv, incoming={"TEA-010": 5, "MUG-001": 4}) == [("TEA-010", 11)]
    assert suggest_orders(items, inv, incoming={"TEA-010": 2, "MUG-001": 3}) == [
        ("TEA-010", 14),
        ("MUG-001", 6),
    ]
    assert suggest_orders(items, inv, incoming={"ZZZ-999": 50}) == [("TEA-010", 16), ("MUG-001", 9)]


def test_quantity_to_target() -> None:
    items = [item("TEA-010", 10), item("MUG-001", 5), item("PEN-100", 3)]
    inv = Inventory.from_levels([("TEA-010", 0, 0), ("MUG-001", 4, 2), ("PEN-100", 2, 0)])
    assert suggest_orders(items, inv) == [("TEA-010", 20), ("MUG-001", 8), ("PEN-100", 4)]
    assert suggest_orders(items, inv, multiplier=1) == [
        ("TEA-010", 10),
        ("MUG-001", 3),
        ("PEN-100", 1),
    ]
    assert suggest_orders(items, inv, multiplier=3) == [
        ("TEA-010", 30),
        ("MUG-001", 13),
        ("PEN-100", 7),
    ]
    # an unknown SKU has nothing available
    assert suggest_orders([item("NB-200", 20)], Inventory()) == [("NB-200", 40)]


def test_pack_rounding() -> None:
    items = [item("TEA-010", 10), item("MUG-001", 5), item("PEN-100", 3)]
    inv = Inventory.from_levels([("TEA-010", 0, 0), ("MUG-001", 4, 2), ("PEN-100", 2, 0)])
    result = suggest_orders(items, inv, pack_sizes={"TEA-010": 12, "MUG-001": 6, "PEN-100": 4})
    assert result == [("TEA-010", 24), ("MUG-001", 12), ("PEN-100", 4)]
    result = suggest_orders(items, inv, pack_sizes={"TEA-010": 20, "MUG-001": 8})
    assert result == [("TEA-010", 20), ("MUG-001", 8), ("PEN-100", 4)]
    result = suggest_orders(items, inv, pack_sizes={"TEA-010": 21, "MUG-001": 1, "ZZZ-999": 7})
    assert result == [("TEA-010", 21), ("MUG-001", 8), ("PEN-100", 4)]
    both = suggest_orders(
        items, inv, pack_sizes={"TEA-010": 5}, incoming={"TEA-010": 3}
    )
    assert both[0] == ("TEA-010", 20)  # need 17 -> 20


@pytest.mark.parametrize(
    "kwargs",
    [
        {"multiplier": 0},
        {"multiplier": -2},
        {"pack_sizes": {"TEA-010": 0}},
        {"pack_sizes": {"ZZZ-999": 0}},
        {"pack_sizes": {"TEA-010": 4, "MUG-001": -1}},
        {"incoming": {"TEA-010": -1}},
        {"incoming": {"ZZZ-999": -5}},
    ],
)
def test_validation_errors(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        suggest_orders([item("TEA-010", 10)], Inventory(), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        suggest_orders([], Inventory(), **kwargs)  # type: ignore[arg-type]


def test_arguments_not_modified() -> None:
    items = [item("TEA-010", 10), item("MUG-001", 5)]
    inv = Inventory.from_levels([("TEA-010", 3, 1), ("MUG-001", 9, 0)])
    packs = {"TEA-010": 6}
    incoming = {"TEA-010": 2, "MUG-001": 0}
    suggest_orders(items, inv, pack_sizes=packs, incoming=incoming)
    assert packs == {"TEA-010": 6} and incoming == {"TEA-010": 2, "MUG-001": 0}
    assert inv.snapshot() == {"MUG-001": (9, 0), "TEA-010": (3, 1)}
    assert [i.sku for i in items] == ["TEA-010", "MUG-001"]


def test_config_default_and_sources(tmp_path: Path) -> None:
    assert DEFAULTS["reorder_multiplier"] == 2
    assert load_config()["reorder_multiplier"] == 2
    ini = tmp_path / "s.ini"
    ini.write_text("[stockroom]\nreorder_multiplier = 4\n")
    assert load_config(ini)["reorder_multiplier"] == 4
    assert load_config(ini, {"STOCKROOM_REORDER_MULTIPLIER": "3"})["reorder_multiplier"] == 3
    assert load_config(env={"STOCKROOM_REORDER_MULTIPLIER": "1"})["reorder_multiplier"] == 1
    assert type(load_config(env={"STOCKROOM_REORDER_MULTIPLIER": "7"})["reorder_multiplier"]) is int
    assert load_config(ini)["tax_bp"] == 0


def test_config_rejects_bad_values(tmp_path: Path) -> None:
    ini = tmp_path / "s.ini"
    for bad in ("0", "-1", "two", "", "1.5"):
        ini.write_text(f"[stockroom]\nreorder_multiplier = {bad}\n")
        with pytest.raises(ConfigError):
            load_config(ini)
        with pytest.raises(ConfigError):
            load_config(env={"STOCKROOM_REORDER_MULTIPLIER": bad})
    # a good file value does not excuse a bad environment value
    ini.write_text("[stockroom]\nreorder_multiplier = 3\n")
    with pytest.raises(ConfigError):
        load_config(ini, {"STOCKROOM_REORDER_MULTIPLIER": "0"})
    # zero stays valid for the settings that allow it
    assert load_config(env={"STOCKROOM_TAX_BP": "0"})["tax_bp"] == 0


def test_cli_defaults() -> None:
    done = run(*BASE)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["MUG-002 6", "TEA-011 11", "PEN-100 3"]


def test_cli_options_and_setting(tmp_path: Path) -> None:
    done = run(*BASE, env={"STOCKROOM_REORDER_MULTIPLIER": "3"})
    assert done.stdout.splitlines() == ["MUG-002 11", "TEA-011 21", "PEN-100 5"]
    done = run(
        *BASE,
        "--incoming", "MUG-002=0", "--incoming", "TEA-011=0",
        "--pack", "TEA-011=6", "--pack", "MUG-002=4",
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["MUG-002 8", "TEA-011 12", "PEN-100 3"]
    # MUG-002: 4 available + 1 incoming = 5 reaches its level of 5, so it drops out
    done = run(*BASE, "--incoming", "MUG-002=1", "--incoming", "TEA-011=0")
    assert done.stdout.splitlines() == ["TEA-011 11", "PEN-100 3"]
    done = run(*BASE, "--pack", "PEN-100=5")
    assert done.stdout.splitlines() == ["MUG-002 6", "TEA-011 11", "PEN-100 5"]
    ini = tmp_path / "s.ini"
    ini.write_text("[stockroom]\nreorder_multiplier = 1\n")
    done = run("--config", str(ini), *BASE)
    assert done.stdout.splitlines() == ["MUG-002 1", "TEA-011 1", "PEN-100 1"]
    done = run("--config", str(ini), *BASE, env={"STOCKROOM_REORDER_MULTIPLIER": "2"})
    assert done.stdout.splitlines() == ["MUG-002 6", "TEA-011 11", "PEN-100 3"]


def test_cli_missing_stock_rows_and_unknown_skus(tmp_path: Path) -> None:
    items = tmp_path / "items.csv"
    items.write_text(
        "sku,name,price,tags,reorder_level\n"
        "MUG-001,Blue Mug,12.50,kitchen,5\n"
        "NB-200,Notebook,6.25,office,0\n"
        "TEA-010,Green Tea,8.95,food,10\n"
    )
    stock = tmp_path / "stock.csv"
    stock.write_text("sku,on_hand,reserved\nMUG-001,9,0\nPEN-100,1,0\n")
    done = run("reorder", "--items", str(items), "--stock", str(stock),
               "--incoming", "ZZZ-999=4", "--pack", "ZZZ-999=3", "--pack", "NB-200=2")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["TEA-010 20"]
    stock.write_text("sku,on_hand,reserved\nMUG-001,9,0\nTEA-010,10,0\n")
    done = run("reorder", "--items", str(items), "--stock", str(stock))
    assert done.returncode == 0 and done.stdout.splitlines() == ["nothing to reorder"]


@pytest.mark.parametrize(
    "extra",
    [
        ["--incoming", "MUG-002"],
        ["--incoming", "MUG-002="],
        ["--incoming", "MUG-002=x"],
        ["--incoming", "MUG-002=-1"],
        ["--incoming", "MUG-002=1.5"],
        ["--incoming", "MUG-002=1_0"],
        ["--incoming", "mug=3"],
        ["--incoming", "=3"],
        ["--incoming", "MUG-002=1", "--incoming", "MUG-002=2"],
        ["--pack", "MUG-002=0"],
        ["--pack", "MUG-002=two"],
        ["--pack", "TEA-011=3", "--pack", "TEA-011=3"],
        ["--pack", "BAD=3"],
    ],
)
def test_cli_bad_values(extra: list[str]) -> None:
    done = run(*BASE, *extra)
    assert done.returncode == 1, (extra, done.stdout, done.stderr)
    assert done.stdout == "" and done.stderr.startswith("stockroom: error: ")
    both = run(*BASE, "--incoming", "MUG-002=1", "--pack", "TEA-011=2")
    assert both.returncode == 0
