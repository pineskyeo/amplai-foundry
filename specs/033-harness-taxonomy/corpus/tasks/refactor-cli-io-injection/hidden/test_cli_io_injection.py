from __future__ import annotations

import ast
import contextlib
import inspect
import io
import os
import types
from pathlib import Path

import pytest

import stockroom
from stockroom import cli

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
ORDERS_CSV = """order_id,customer,placed,status,sku,quantity,unit_price
Z-1,Hana Kimura-Lee,2024-01-08,shipped,MUG-001,20,12.50
Z-1,Hana Kimura-Lee,2024-01-08,shipped,TEA-010,10,8.95
Z-2,Omar Diaz-Fuentes,2024-01-19,paid,PEN-100,5,34.00
Z-3,Lena Berg-Holmgren,2024-02-03,cancelled,LMP-030,1,49.99
Z-4,Hana Kimura-Lee,2024-02-14,paid,CND-007,30,9.80
Z-5,Lena Berg-Holmgren,2024-03-02,open,TEA-011,20,7.40
"""

# (exit code, stdout, stderr) recorded from the command before the change.
GOLD = {
    'items': (0, 'SKU      Name            Price\n-------  --------------  ------\nMUG-001  Blue Mug        $12.50\nMUG-002  Red Mug         $12.50\nTEA-010  Green Tea 100g   $8.95\nTEA-011  Black Tea 100g   $7.40\nPEN-100  Fountain Pen    $34.00\nNB-200   Notebook A5      $6.25\nLMP-030  Desk Lamp       $49.99\nCND-007  Beeswax Candle   $9.80\n', ''),
    'items-tag': (0, 'SKU      Name            Price\n-------  --------------  -----\nTEA-010  Green Tea 100g  $8.95\nTEA-011  Black Tea 100g  $7.40\n', ''),
    'stock': (0, 'LOW MUG-002 Red Mug: 4/5\nLOW TEA-011 Black Tea 100g: 9/10\nLOW PEN-100 Fountain Pen: 1/2\n', 'warning: 3 item(s) below reorder level\n'),
    'sales': (0, '4 orders, 1 cancelled\nRevenue: $951.50\n\nMonth    Revenue\n-------  -------\n2024-01  $509.50\n2024-02  $294.00\n2024-03  $148.00\n\nCustomer              Total\n------------------  -------\nHana Kimura-Lee     $633.50\nOmar Diaz-Fuentes   $170.00\nLena Berg-Holmgren  $148.00\n', ''),
    'sales-from': (0, '2 orders, 1 cancelled\nRevenue: $442.00\n\nMonth    Revenue\n-------  -------\n2024-02  $294.00\n2024-03  $148.00\n\nCustomer              Total\n------------------  -------\nHana Kimura-Lee     $294.00\nLena Berg-Holmgren  $148.00\n', ''),
    'sales-bad-from': (1, '', "stockroom: error: not a date: 'soon'\n"),
    'price': (0, '3 x MUG-001 Blue Mug: $37.50\ntax: $0.00\ntotal: $37.50\n', ''),
    'price-discount': (0, '5 x PEN-100 Fountain Pen: $153.00\ntax: $0.00\ntotal: $153.00\n', ''),
    'price-unknown-sku': (1, '', 'stockroom: error: no item XYZ-999\n'),
    'price-zero-quantity': (1, '', 'stockroom: error: quantity must be positive\n'),
    'price-tax': (0, '5 x PEN-100 Fountain Pen: EUR 153.00\ntax: EUR 12.62\ntotal: EUR 165.62\n', ''),
    'price-bad-tax': (1, '', "stockroom: error: tax_bp must be a whole number, not 'lots'\n"),
    'bad-config': (1, '', "stockroom: error: unknown setting 'colour' in <D>/s.ini\n"),
    'config-quiet': (0, 'LOW MUG-002 Red Mug: 4/5\nLOW TEA-011 Black Tea 100g: 9/10\nLOW PEN-100 Fountain Pen: 1/2\n', ''),
    'config-symbol': (0, 'SKU      Name            Price\n-------  --------------  --------\nMUG-001  Blue Mug        GBP12.50\nMUG-002  Red Mug         GBP12.50\nTEA-010  Green Tea 100g   GBP8.95\nTEA-011  Black Tea 100g   GBP7.40\nPEN-100  Fountain Pen    GBP34.00\nNB-200   Notebook A5      GBP6.25\nLMP-030  Desk Lamp       GBP49.99\nCND-007  Beeswax Candle   GBP9.80\n', ''),
}

PRICE_TAX = {"STOCKROOM_TAX_BP": "825", "STOCKROOM_CURRENCY_SYMBOL": "EUR "}

# name -> (argv, environment variables)
CASES = {
    "items": (["items", "--file", "{I}"], {}),
    "items-tag": (["items", "--file", "{I}", "--tag", "Tea"], {}),
    "stock": (["stock", "--items", "{I}", "--stock", "{S}"], {}),
    "sales": (["sales", "--orders", "{O}"], {}),
    "sales-from": (["sales", "--orders", "{O}", "--from", "2024-02-01"], {}),
    "sales-bad-from": (["sales", "--orders", "{O}", "--from", "soon"], {}),
    "price": (["price", "--items", "{I}", "--sku", "MUG-001", "--quantity", "3"], {}),
    "price-discount": (
        ["price", "--items", "{I}", "--sku", "PEN-100", "--quantity", "5", "--discount", "10"],
        {},
    ),
    "price-unknown-sku": (["price", "--items", "{I}", "--sku", "XYZ-999", "--quantity", "1"], {}),
    "price-zero-quantity": (["price", "--items", "{I}", "--sku", "XYZ-999", "--quantity", "0"], {}),
    "price-tax": (
        ["price", "--items", "{I}", "--sku", "PEN-100", "--quantity", "5", "--discount", "10"],
        PRICE_TAX,
    ),
    "price-bad-tax": (
        ["price", "--items", "{I}", "--sku", "PEN-100", "--quantity", "5"],
        {"STOCKROOM_TAX_BP": "lots"},
    ),
    "bad-config": (["--config", "{C}", "items", "--file", "{I}"], {}),
    "config-quiet": (["--config", "{C}", "stock", "--items", "{I}", "--stock", "{S}"], {}),
    "config-symbol": (["--config", "{C}", "items", "--file", "{I}"], {}),
}
CONFIGS = {
    "bad-config": "[stockroom]\ncolour = blue\n",
    "config-quiet": "[stockroom]\ncurrency_symbol = GBP \nlow_stock_warning = no\n",
    "config-symbol": "[stockroom]\ncurrency_symbol = GBP \nlow_stock_warning = no\n",
}


@pytest.fixture
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith("STOCKROOM_")]:
        monkeypatch.delenv(key)
    (tmp_path / "items.csv").write_text(ITEMS_CSV, encoding="utf-8")
    (tmp_path / "stock.csv").write_text(STOCK_CSV, encoding="utf-8")
    (tmp_path / "orders.csv").write_text(ORDERS_CSV, encoding="utf-8")
    return {
        "{I}": str(tmp_path / "items.csv"),
        "{S}": str(tmp_path / "stock.csv"),
        "{O}": str(tmp_path / "orders.csv"),
        "{C}": str(tmp_path / "s.ini"),
        "<D>": str(tmp_path),
    }


def prepare(name: str, files: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    argv, env = CASES[name]
    if name in CONFIGS:
        Path(files["{C}"]).write_text(CONFIGS[name], encoding="utf-8")
    return [files.get(a, a) for a in argv], dict(env)


def scrub(text: str, files: dict[str, str]) -> str:
    return text.replace(files["<D>"], "<D>")


@pytest.mark.parametrize("name", sorted(CASES))
def test_injected_streams_receive_all_output(
    name: str, files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    argv, env = prepare(name, files)
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=err, environ=env)
    leaked = capsys.readouterr()
    assert (leaked.out, leaked.err) == ("", "")
    assert (code, scrub(out.getvalue(), files), scrub(err.getvalue(), files)) == GOLD[name]


@pytest.mark.parametrize("name", sorted(CASES))
def test_without_injection_the_current_streams_and_environment_are_used(
    name: str,
    files: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv, env = prepare(name, files)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    code = cli.main(argv)
    captured = capsys.readouterr()
    assert (code, scrub(captured.out, files), scrub(captured.err, files)) == GOLD[name]


def test_streams_are_looked_up_when_main_runs_not_when_it_is_defined(files: dict[str, str]) -> None:
    argv, _ = prepare("items", files)
    first_out, first_err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(first_out), contextlib.redirect_stderr(first_err):
        assert cli.main(argv) == 0
        assert cli.main(["price", "--items", files["{I}"], "--sku", "NOPE-000", "--quantity", "1"]) == 1
    assert first_out.getvalue() == GOLD["items"][1]
    assert first_err.getvalue() == "stockroom: error: no item NOPE-000\n"
    second_out = io.StringIO()
    with contextlib.redirect_stdout(second_out):
        assert cli.main(argv) == 0
    assert second_out.getvalue() == GOLD["items"][1]
    assert first_out.getvalue() == GOLD["items"][1]


def test_one_stream_can_be_injected_without_the_other(
    files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    argv, _ = prepare("stock", files)
    out = io.StringIO()
    assert cli.main(argv, stdout=out) == 0
    captured = capsys.readouterr()
    assert out.getvalue() == GOLD["stock"][1] and captured.out == ""
    assert captured.err == GOLD["stock"][2]
    err = io.StringIO()
    assert cli.main(argv, stderr=err) == 0
    captured = capsys.readouterr()
    assert err.getvalue() == GOLD["stock"][2] and captured.err == ""
    assert captured.out == GOLD["stock"][1]
    # both streams may be the same object: lines come out in the order they were printed
    both = io.StringIO()
    assert cli.main(argv, stdout=both, stderr=both) == 0
    assert both.getvalue() == GOLD["stock"][1] + GOLD["stock"][2]


def test_the_given_environment_replaces_the_process_environment(
    files: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCKROOM_TAX_BP", "lots")
    monkeypatch.setenv("STOCKROOM_CURRENCY_SYMBOL", "ZZZ ")
    argv, _ = prepare("price", files)
    out, err = io.StringIO(), io.StringIO()
    assert cli.main(argv, stdout=out, stderr=err, environ={}) == 0
    assert (out.getvalue(), err.getvalue()) == (GOLD["price"][1], "")
    out = io.StringIO()
    tax_argv, _ = prepare("price-tax", files)
    assert cli.main(tax_argv, stdout=out, environ=types.MappingProxyType(PRICE_TAX)) == 0
    assert out.getvalue() == GOLD["price-tax"][1]
    err = io.StringIO()
    assert cli.main(argv, stdout=io.StringIO(), stderr=err) == 1
    assert err.getvalue() == GOLD["price-bad-tax"][2]


def test_argparse_keeps_writing_to_the_real_streams(
    files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    out, err = io.StringIO(), io.StringIO()
    with pytest.raises(SystemExit) as version:
        cli.main(["--version"], stdout=out, stderr=err, environ={})
    assert version.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == "stockroom 0.4.0\n" and (out.getvalue(), err.getvalue()) == ("", "")
    with pytest.raises(SystemExit) as usage:
        cli.main(["items"], stdout=out, stderr=err, environ={})
    assert usage.value.code == 2
    captured = capsys.readouterr()
    assert "the following arguments are required: --file" in captured.err
    assert (out.getvalue(), err.getvalue()) == ("", "")


def test_main_takes_three_optional_keyword_only_parameters() -> None:
    parameters = inspect.signature(cli.main).parameters
    assert list(parameters) == ["argv", "stdout", "stderr", "environ"]
    assert parameters["argv"].default is None
    for name in ("stdout", "stderr", "environ"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters[name].default is None


def test_the_command_module_looks_up_the_process_streams_and_environment_once() -> None:
    tree = ast.parse((PACKAGE / "cli.py").read_text(encoding="utf-8"))
    bare_prints = []
    process_state: dict[str, int] = {"sys.stdout": 0, "sys.stderr": 0, "os.environ": 0}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
            if not any(k.arg == "file" for k in node.keywords):
                bare_prints.append(node.lineno)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            dotted = f"{node.value.id}.{node.attr}"
            if dotted in process_state:
                process_state[dotted] += 1
    assert bare_prints == []
    assert all(count <= 1 for count in process_state.values()), process_state
