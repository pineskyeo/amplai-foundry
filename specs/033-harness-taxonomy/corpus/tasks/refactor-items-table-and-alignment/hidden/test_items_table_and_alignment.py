import ast
from pathlib import Path

import pytest

import stockroom
from stockroom import cli, report
from stockroom.csvio import write_items
from stockroom.models import Item

PACKAGE = Path(stockroom.__file__).resolve().parent
SCRIPTS = PACKAGE.parent / "scripts"

SHORT = [Item("PEN-100", "Pen", 100, ("office",), 1), Item("CUP-200", "Cup", 250, (), 0)]
LONG = [
    Item("MUG-001", "Hand-painted ceramic mug (large)", 99999, ("kitchen",), 5),
    Item("TEA-010", "Tea", 895, ("food", "tea"), 10),
    Item("NB-200", "Notebook A5", 12345, ("office",), 20),
]
ONE = [Item("LMP-030", "Desk Lamp", 4999, (), 1)]

SAMPLE_CSV = """sku,name,price,tags,reorder_level
MUG-001,Blue Mug,12.50,kitchen;gift,5
MUG-002,Red Mug,12.50,kitchen,5
TEA-010,Green Tea 100g,8.95,food;tea,10
TEA-011,Black Tea 100g,7.40,food;tea,10
PEN-100,Fountain Pen,34.00,office;gift,2
NB-200,Notebook A5,6.25,office,20
LMP-030,Desk Lamp,49.99,office;home,1
CND-007,Beeswax Candle,9.80,home;gift,8
"""

TABLES = {
    ("short", "$"): "SKU      Name  Price\n-------  ----  -----\nPEN-100  Pen   $1.00\nCUP-200  Cup   $2.50",
    ("short", ""): "SKU      Name  Price\n-------  ----  -----\nPEN-100  Pen    1.00\nCUP-200  Cup    2.50",
    ("short", "EUR "): "SKU      Name  Price\n-------  ----  --------\nPEN-100  Pen   EUR 1.00\nCUP-200  Cup   EUR 2.50",
    ("long", "$"): "SKU      Name                              Price\n-------  --------------------------------  -------\nMUG-001  Hand-painted ceramic mug (large)  $999.99\nTEA-010  Tea                                 $8.95\nNB-200   Notebook A5                       $123.45",
    ("long", ""): "SKU      Name                              Price\n-------  --------------------------------  ------\nMUG-001  Hand-painted ceramic mug (large)  999.99\nTEA-010  Tea                                 8.95\nNB-200   Notebook A5                       123.45",
    ("one", "$"): "SKU      Name       Price\n-------  ---------  ------\nLMP-030  Desk Lamp  $49.99",
    ("one", "EUR "): "SKU      Name       Price\n-------  ---------  ---------\nLMP-030  Desk Lamp  EUR 49.99",
    ("empty", "$"): "SKU  Name  Price\n---  ----  -----",
    ("empty", "EUR "): "SKU  Name  Price\n---  ----  -----",
}
CATALOGUES = {"short": SHORT, "long": LONG, "one": ONE, "empty": []}

SAMPLE_OUT = (
    "SKU      Name            Price\n-------  --------------  ------\n"
    "MUG-001  Blue Mug        $12.50\nMUG-002  Red Mug         $12.50\n"
    "TEA-010  Green Tea 100g   $8.95\nTEA-011  Black Tea 100g   $7.40\n"
    "PEN-100  Fountain Pen    $34.00\nNB-200   Notebook A5      $6.25\n"
    "LMP-030  Desk Lamp       $49.99\nCND-007  Beeswax Candle   $9.80\n"
)
GIFT_OUT = (
    "SKU      Name            Price\n-------  --------------  ------\n"
    "MUG-001  Blue Mug        $12.50\nPEN-100  Fountain Pen    $34.00\n"
    "CND-007  Beeswax Candle   $9.80\n"
)


@pytest.mark.parametrize(("name", "symbol"), sorted(TABLES))
def test_items_table_layout(name: str, symbol: str) -> None:
    assert report.items_table(CATALOGUES[name], symbol) == TABLES[(name, symbol)]


def test_items_table_defaults_to_the_dollar_sign_and_has_clean_lines() -> None:
    text = report.items_table(SHORT)
    assert text == TABLES[("short", "$")]
    assert not text.endswith("\n")
    assert all(line == line.rstrip(" ") for line in text.split("\n"))


def run(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, symbol: str | None, *args: str):
    for key in ("STOCKROOM_CURRENCY_SYMBOL", "STOCKROOM_LOW_STOCK_WARNING", "STOCKROOM_TAX_BP"):
        monkeypatch.delenv(key, raising=False)
    if symbol is not None:
        monkeypatch.setenv("STOCKROOM_CURRENCY_SYMBOL", symbol)
    code = cli.main(["items", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_items_command_prints_the_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = tmp_path / "sample.csv"
    sample.write_text(SAMPLE_CSV, encoding="utf-8")
    assert run(capsys, monkeypatch, None, "--file", str(sample)) == (0, SAMPLE_OUT, "")
    assert run(capsys, monkeypatch, None, "--file", str(sample), "--tag", "GIFT") == (0, GIFT_OUT, "")
    assert run(capsys, monkeypatch, None, "--file", str(sample), "--tag", "nothing") == (
        0,
        "SKU  Name  Price\n---  ----  -----\n",
        "",
    )
    for name, symbol in (("short", ""), ("short", "EUR "), ("long", "$"), ("empty", "$")):
        path = tmp_path / f"{name}.csv"
        path.write_text(write_items(CATALOGUES[name]), encoding="utf-8")
        code, out, err = run(capsys, monkeypatch, symbol, "--file", str(path))
        assert (code, err) == (0, "")
        assert out == TABLES[(name, symbol)] + "\n"


def test_no_module_pads_with_spaces_except_the_align_helper() -> None:
    offenders = []
    files = sorted(PACKAGE.rglob("*.py")) + sorted(SCRIPTS.rglob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = set()
        if path.name == "textutil.py" and path.parent == PACKAGE:
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "align":
                    allowed = {id(n) for n in ast.walk(node)}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"ljust", "rjust", "center"}
                and len(node.args) == 1
                and not node.keywords
                and id(node) not in allowed
            ):
                offenders.append((str(path.relative_to(PACKAGE.parent)), node.func.attr, node.lineno))
    assert offenders == []


def test_align_helper_is_unchanged() -> None:
    from stockroom.textutil import align

    assert align("ab", 5) == "ab   "
    assert align("ab", 5, "right") == "   ab"
    assert align("ab", 5, "center") == "  ab "
    assert align("abcdef", 3, "right") == "abcdef"
    with pytest.raises(ValueError):
        align("ab", 5, "justify")
