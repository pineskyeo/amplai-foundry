from __future__ import annotations

import ast
import dataclasses
import os
from pathlib import Path

import pytest

import stockroom
from stockroom import cli
from stockroom.errors import ParseError
from stockroom.models import Item

PACKAGE = Path(stockroom.__file__).resolve().parent

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

# (exit code, stdout, stderr) recorded from the command before the change.
GOLD = {
    'items': (0, 'SKU      Name            Price\n-------  --------------  ------\nMUG-001  Blue Mug        $12.50\nMUG-002  Red Mug         $12.50\nTEA-010  Green Tea 100g   $8.95\nTEA-011  Black Tea 100g   $7.40\nPEN-100  Fountain Pen    $34.00\nNB-200   Notebook A5      $6.25\nLMP-030  Desk Lamp       $49.99\nCND-007  Beeswax Candle   $9.80\n', ''),
    'items-tag': (0, 'SKU      Name            Price\n-------  --------------  -----\nTEA-010  Green Tea 100g  $8.95\nTEA-011  Black Tea 100g  $7.40\n', ''),
    'price': (0, '3 x MUG-001 Blue Mug: $37.50\ntax: $0.00\ntotal: $37.50\n', ''),
    'price-discount': (0, '5 x PEN-100 Fountain Pen: $153.00\ntax: $0.00\ntotal: $153.00\n', ''),
    'price-unknown-sku': (1, '', 'stockroom: error: no item XYZ-999\n'),
    'price-zero-quantity': (1, '', 'stockroom: error: quantity must be positive\n'),
    'price-tax': (0, '5 x PEN-100 Fountain Pen: EUR 153.00\ntax: EUR 12.62\ntotal: EUR 165.62\n', ''),
    'price-bad-tax': (1, '', "stockroom: error: tax_bp must be a whole number, not 'lots'\n"),
}

CASES = {
    "items": ["items", "--file", "{I}"],
    "items-tag": ["items", "--file", "{I}", "--tag", "Tea"],
    "price": ["price", "--items", "{I}", "--sku", "MUG-001", "--quantity", "3"],
    "price-discount": ["price", "--items", "{I}", "--sku", "PEN-100", "--quantity", "5", "--discount", "10"],
    "price-unknown-sku": ["price", "--items", "{I}", "--sku", "XYZ-999", "--quantity", "1"],
    "price-zero-quantity": ["price", "--items", "{I}", "--sku", "XYZ-999", "--quantity", "0"],
}


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path):
    (tmp_path / "items.csv").write_text(ITEMS_CSV, encoding="utf-8")
    for key in [k for k in os.environ if k.startswith("STOCKROOM_")]:
        monkeypatch.delenv(key)
    applied: list[str] = []

    def call(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
        for key in applied:
            monkeypatch.delenv(key, raising=False)
        applied.clear()
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
            applied.append(key)
        real = [str(tmp_path / "items.csv") if a == "{I}" else a for a in argv]
        try:
            code = cli.main(real)
        except SystemExit as exc:
            code = exc.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return call


def items() -> list[Item]:
    return [
        Item("MUG-001", "Blue Mug", 1250, ("kitchen", "gift"), 5),
        Item("TEA-010", "Green Tea", 895, ("food", "tea"), 10),
        Item("PEN-100", "Fountain Pen", 3400, ("office", "gift"), 2),
        Item("TEA-011", "Black Tea", 740, ("food", "tea"), 10),
        Item("NB-200", "Notebook", 625, (), 20),
    ]


def test_find_item_returns_the_first_match_or_raises_parse_error() -> None:
    from stockroom.catalogue import find_item

    found = items()
    assert find_item(found, "PEN-100") is found[2]
    assert find_item(tuple(found), "NB-200") is found[4]
    twin = Item("MUG-001", "Other Mug", 1, (), 0)
    assert find_item([*found, twin], "MUG-001") is found[0]
    with pytest.raises(ParseError) as caught:
        find_item(found, "XYZ-999")
    assert str(caught.value) == "no item XYZ-999"
    with pytest.raises(ParseError, match="no item "):
        find_item([], "MUG-001")
    with pytest.raises(ParseError):
        find_item(found, "mug-001")


def test_with_tag_filters_ignoring_case_and_keeps_order() -> None:
    from stockroom.catalogue import with_tag

    found = items()
    assert [i.sku for i in with_tag(found, "tea")] == ["TEA-010", "TEA-011"]
    assert [i.sku for i in with_tag(found, "GIFT")] == ["MUG-001", "PEN-100"]
    assert [i.sku for i in with_tag(found, "Office")] == ["PEN-100"]
    assert with_tag(found, "nothing") == []
    assert with_tag([], "tea") == []
    everything = with_tag(found, "")
    assert everything == found and everything is not found
    assert with_tag(tuple(found), "food") == [found[1], found[3]]
    assert with_tag(found, None) == found  # type: ignore[arg-type]


def test_quote_prices_a_quantity_with_discount_and_tax() -> None:
    from stockroom.pricing import Quote, quote

    assert dataclasses.is_dataclass(Quote)
    assert [f.name for f in dataclasses.fields(Quote)] == [
        "subtotal_cents",
        "tax_cents",
        "total_cents",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        quote(100, 1).total_cents = 5  # type: ignore[misc]
    assert quote(1250, 3) == Quote(3750, 0, 3750)
    assert quote(1250, 3, tax_bp=1000) == Quote(3750, 375, 4125)
    assert quote(3400, 5, 10, 825) == Quote(15300, 1262, 16562)
    assert quote(unit_price_cents=1000, quantity=4, discount_percent=25, tax_bp=0) == Quote(3000, 0, 3000)
    assert quote(8000, 1, 12.5) == Quote(7000, 0, 7000)
    assert quote(1, 1, tax_bp=5000) == Quote(1, 1, 2)
    assert quote(0, 7, 50, 825) == Quote(0, 0, 0)
    with pytest.raises(ValueError):
        quote(1000, 1, 120)
    with pytest.raises(ValueError):
        quote(1000, 1, 0, -1)


@pytest.mark.parametrize("name", sorted(CASES))
def test_items_and_price_commands_behave_as_before(name: str, run, tmp_path: Path) -> None:
    code, out, err = run(CASES[name])
    assert (code, out, err.replace(str(tmp_path), "<D>")) == GOLD[name]


def test_tag_filter_and_settings_reach_the_commands_as_before(run) -> None:
    code, everything, err = run(["items", "--file", "{I}"])
    assert run(["items", "--file", "{I}", "--tag", ""]) == (0, everything, "")
    assert run(["items", "--file", "{I}", "--tag", "TEA"]) == run(["items", "--file", "{I}", "--tag", "tea"])
    assert run(["items", "--file", "{I}", "--tag", "gift"])[1].count("\n") == 2 + 3
    assert run(
        ["price", "--items", "{I}", "--sku", "PEN-100", "--quantity", "5", "--discount", "10"],
        {"STOCKROOM_TAX_BP": "825", "STOCKROOM_CURRENCY_SYMBOL": "EUR "},
    ) == GOLD["price-tax"]
    assert run(
        ["price", "--items", "{I}", "--sku", "PEN-100", "--quantity", "5"],
        {"STOCKROOM_TAX_BP": "lots"},
    ) == GOLD["price-bad-tax"]


def test_the_command_module_holds_no_lookup_or_price_arithmetic() -> None:
    tree = ast.parse((PACKAGE / "cli.py").read_text(encoding="utf-8"))
    defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert "_find" not in defined
    names = {
        n.id if isinstance(n, ast.Name) else n.attr
        for n in ast.walk(tree)
        if isinstance(n, (ast.Name, ast.Attribute))
    }
    assert not names & {"apply_discount", "tax", "tags"}, names & {"apply_discount", "tax", "tags"}
    assert "with_tag" in names and "find_item" in names and "quote" in names
