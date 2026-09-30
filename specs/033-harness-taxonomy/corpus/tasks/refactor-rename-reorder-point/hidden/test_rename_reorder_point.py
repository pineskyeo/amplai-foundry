from __future__ import annotations

import ast
import dataclasses
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

import stockroom
from stockroom import cli
from stockroom.csvio import ITEM_HEADER, read_items, write_items
from stockroom.errors import ParseError
from stockroom.inventory import Inventory
from stockroom.models import Item

PACKAGE = Path(stockroom.__file__).resolve().parent
ROOT = PACKAGE.parent

ITEMS_CSV = """sku,name,price,tags,reorder_level
MUG-001,Blue Mug,12.50,kitchen;gift,5
MUG-002,Red Mug,12.50,kitchen,5
TEA-010,Green Tea 100g,8.95,food;tea,10
TEA-011,Black Tea 100g,7.40,food;tea,10
PEN-100,Fountain Pen,34.00,office;gift,2
NB-200,Notebook A5,6.25,office,20
LMP-030,Desk Lamp,49.99,office;home,1
CND-007,Beeswax Candle,9.80,home;gift,8
"""
STOCK_CSV = """sku,on_hand,reserved
MUG-001,14,2
MUG-002,4,0
TEA-010,30,5
TEA-011,9,0
PEN-100,3,2
NB-200,40,4
LMP-030,2,0
CND-007,12,1
"""


def test_the_item_field_is_called_reorder_point() -> None:
    assert [f.name for f in dataclasses.fields(Item)] == [
        "sku",
        "name",
        "price_cents",
        "tags",
        "reorder_point",
    ]
    item = Item("MUG-001", "Blue Mug", 1250, ("kitchen",), 5)
    assert item.reorder_point == 5
    assert Item("MUG-001", "Blue Mug", 1250).reorder_point == 0
    assert Item(sku="MUG-001", name="Blue Mug", price_cents=1250, reorder_point=9).reorder_point == 9
    assert dataclasses.asdict(item) == {
        "sku": "MUG-001",
        "name": "Blue Mug",
        "price_cents": 1250,
        "tags": ("kitchen",),
        "reorder_point": 5,
    }
    assert "reorder_point=5" in repr(item) and "reorder_level" not in repr(item)
    assert item == Item("MUG-001", "Blue Mug", 1250, ("kitchen",), 5)
    assert item != Item("MUG-001", "Blue Mug", 1250, ("kitchen",), 6)
    assert hash(item) == hash(Item("MUG-001", "Blue Mug", 1250, ("kitchen",), 5))


def test_validation_messages_are_unchanged() -> None:
    with pytest.raises(ParseError) as caught:
        Item("MUG-001", "Blue Mug", 1250, (), -1)
    assert str(caught.value) == "MUG-001: negative reorder level"
    with pytest.raises(ParseError) as caught:
        Item("MUG-001", "Blue Mug", -5, (), 1)
    assert str(caught.value) == "MUG-001: negative price"
    assert Item("MUG-001", "Blue Mug", 0, (), 0).reorder_point == 0


def test_the_old_name_stays_readable_as_a_read_only_alias() -> None:
    item = Item("MUG-001", "Blue Mug", 1250, (), 7)
    assert isinstance(Item.reorder_level, property)
    assert item.reorder_level == 7 == item.reorder_point
    assert "reorder_level" not in {f.name for f in dataclasses.fields(Item)}
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        item.reorder_level = 3  # type: ignore[misc]


def _alias_nodes(tree: ast.Module) -> set[ast.AST]:
    """Nodes of the alias definition in `class Item`, whatever its form.

    Covers `@property def reorder_level`, `reorder_level = property(...)` and
    `reorder_level: T = property(...)` in the class body.
    """
    exempt: set[ast.AST] = set()
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Item"):
        for stmt in cls.body:
            if isinstance(stmt, ast.FunctionDef) and stmt.name == "reorder_level":
                exempt.add(stmt)
            elif isinstance(stmt, ast.Assign):
                exempt.update(t for t in stmt.targets if isinstance(t, ast.Name) and t.id == "reorder_level")
            elif isinstance(stmt, ast.AnnAssign):
                if isinstance(stmt.target, ast.Name) and stmt.target.id == "reorder_level":
                    exempt.add(stmt.target)
    return exempt


def test_no_code_uses_the_old_name_except_the_alias() -> None:
    offenders = []
    files = sorted(PACKAGE.rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        exempt = _alias_nodes(tree) if path.name == "models.py" and path.parent == PACKAGE else set()
        for node in ast.walk(tree):
            if node in exempt:
                continue
            if isinstance(node, ast.Attribute) and node.attr == "reorder_level":
                offenders.append((path.name, node.lineno, "attribute"))
            if isinstance(node, ast.keyword) and node.arg == "reorder_level":
                offenders.append((path.name, node.value.lineno, "keyword"))
            if isinstance(node, ast.arg) and node.arg == "reorder_level":
                offenders.append((path.name, node.lineno, "parameter"))
            if isinstance(node, ast.Name) and node.id == "reorder_level":
                offenders.append((path.name, node.lineno, "name"))
            if isinstance(node, ast.FunctionDef) and node.name == "reorder_level":
                offenders.append((path.name, node.lineno, "definition"))
    assert offenders == []


def test_the_file_format_and_its_messages_keep_the_old_wording() -> None:
    assert ITEM_HEADER == ("sku", "name", "price", "tags", "reorder_level")
    items = read_items(ITEMS_CSV)
    assert [i.reorder_point for i in items] == [5, 5, 10, 10, 2, 20, 1, 8]
    assert write_items(items) == ITEMS_CSV
    with pytest.raises(ParseError) as caught:
        read_items("sku,name,price,tags,reorder_level\nMUG-001,Mug,1.00,,x\n")
    assert str(caught.value) == "line 2: reorder_level is not a whole number: 'x'"
    with pytest.raises(ParseError) as caught:
        read_items("sku,name,price,tags,reorder_level\nMUG-001,Mug,1.00,,-2\n")
    assert str(caught.value) == "MUG-001: negative reorder level"
    with pytest.raises(ParseError) as caught:
        read_items("sku,name,price,tags,reorder_point\nMUG-001,Mug,1.00,,2\n")
    assert str(caught.value) == "item header must be sku,name,price,tags,reorder_level"


def test_low_stock_compares_with_the_reorder_point() -> None:
    inventory = Inventory.from_levels([("MUG-001", 5, 0), ("TEA-010", 5, 0), ("PEN-100", 5, 1)])
    items = [
        Item("MUG-001", "Mug", 100, (), 5),
        Item("TEA-010", "Tea", 100, (), 6),
        Item("PEN-100", "Pen", 100, (), 4),
        Item("NB-200", "Notebook", 100, (), 0),
        Item("LMP-030", "Lamp", 100, (), 1),
    ]
    assert [i.sku for i in inventory.low_stock(items)] == ["TEA-010", "LMP-030"]


def test_the_stock_command_output_is_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in [k for k in os.environ if k.startswith("STOCKROOM_")]:
        monkeypatch.delenv(key)
    (tmp_path / "items.csv").write_text(ITEMS_CSV, encoding="utf-8")
    (tmp_path / "stock.csv").write_text(STOCK_CSV, encoding="utf-8")
    code = cli.main(["stock", "--items", str(tmp_path / "items.csv"), "--stock", str(tmp_path / "stock.csv")])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == (
        "LOW MUG-002 Red Mug: 4/5\nLOW TEA-011 Black Tea 100g: 9/10\nLOW PEN-100 Fountain Pen: 1/2\n"
    )
    assert captured.err == "warning: 3 item(s) below reorder level\n"
    monkeypatch.setenv("COLUMNS", "80")
    with pytest.raises(SystemExit):
        cli.main(["stock", "--help"])
    assert "usage: stockroom stock [-h] --items ITEMS --stock STOCK" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "list items below their reorder level" in capsys.readouterr().out


def test_the_sample_data_script_writes_the_same_files(tmp_path: Path) -> None:
    expected = {
        "items.csv": "1eb8b9a547ef855651a80c8ccff7d44ec14cf47e0a82acde9407b0eeda104be6",
        "orders.csv": "6d44cad1d322b12d90a388670123d89a22820eca017f2b109856852f95615f7e",
        "stock.csv": "792768bfa9e664aa9c28e412b612f70d38fd864e8f575a88bdd0cdb953f6ad74",
    }
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_sample_data.py"), "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    got = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    assert got == expected
