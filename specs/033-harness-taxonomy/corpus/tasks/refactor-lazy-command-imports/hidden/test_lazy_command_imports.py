from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
import stockroom
from stockroom import cli

ROOT = Path(stockroom.__file__).resolve().parent.parent
ALWAYS = ["stockroom", "stockroom.cli", "stockroom.config", "stockroom.errors"]
HOME = {
    "read_items": "csvio",
    "read_orders": "csvio",
    "read_stock": "csvio",
    "parse_date": "dates",
    "Inventory": "inventory",
    "Item": "models",
    "format_money": "money",
    "OrderBook": "orders",
    "apply_discount": "pricing",
    "tax": "pricing",
    "sales_summary": "report",
    "load_config": "config",
    "ParseError": "errors",
    "StockroomError": "errors",
}

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

PROBE = """
import json, sys
import stockroom.cli as cli
argv = json.loads(sys.argv[1])
access = json.loads(sys.argv[2])
code = None
for name in access:
    getattr(cli, name)
if argv is not None:
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
loaded = sorted(m for m in sys.modules if m == "stockroom" or m.startswith("stockroom."))
with open(sys.argv[3], "w", encoding="utf-8") as handle:
    json.dump({"code": code, "modules": loaded}, handle)
"""


def probe(tmp_path: Path, argv: list[str] | None, access: tuple[str, ...] = ()) -> dict:
    """Run ``argv`` through ``cli.main`` in a fresh interpreter; the stockroom modules it loaded."""
    for name, text in (
        ("items.csv", ITEMS_CSV),
        ("stock.csv", STOCK_CSV),
        ("orders.csv", ORDERS_CSV),
    ):
        (tmp_path / name).write_text(text, encoding="utf-8")
    out = tmp_path / "probe.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    env["COLUMNS"] = "80"
    done = subprocess.run(
        [sys.executable, "-c", PROBE, json.dumps(argv), json.dumps(list(access)), str(out)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "argv",
    [
        None,
        ["--version"],
        ["--help"],
        ["items", "--help"],
        ["price", "--help"],
        ["price"],
        ["nope"],
    ],
    ids=["import", "version", "help", "items-help", "price-help", "usage-error", "bad-command"],
)
def test_parsing_alone_loads_only_settings_and_errors(tmp_path: Path, argv) -> None:
    result = probe(tmp_path, argv)
    assert "stockroom.cli" in result["modules"]
    assert set(result["modules"]) <= set(ALWAYS), result["modules"]


COMMAND_MODULES = {
    "items": (
        ["items", "--file", "items.csv", "--tag", "tea"],
        ["csvio", "dates", "models", "money"],
    ),
    "stock": (
        ["stock", "--items", "items.csv", "--stock", "stock.csv"],
        ["csvio", "dates", "inventory", "models", "money"],
    ),
    "sales": (
        ["sales", "--orders", "orders.csv"],
        ["csvio", "dates", "models", "money", "orders", "pricing", "report", "textutil"],
    ),
    "sales-from": (
        ["sales", "--orders", "orders.csv", "--from", "2024-02-01"],
        ["csvio", "dates", "models", "money", "orders", "pricing", "report", "textutil"],
    ),
    "price": (
        ["price", "--items", "items.csv", "--sku", "PEN-100", "--quantity", "5"],
        ["csvio", "dates", "models", "money", "pricing"],
    ),
}


@pytest.mark.parametrize("name", sorted(COMMAND_MODULES))
def test_each_command_loads_exactly_the_modules_it_needs(tmp_path: Path, name: str) -> None:
    argv, needed = COMMAND_MODULES[name]
    result = probe(tmp_path, argv)
    assert result["code"] == 0
    assert result["modules"] == sorted(ALWAYS + [f"stockroom.{m}" for m in needed])


ACCESS_MODULES = {
    "format_money": ["errors", "money"],
    "parse_date": ["dates", "errors"],
    "Inventory": ["errors", "inventory", "models"],
    "Item": ["errors", "models"],
    "tax": ["errors", "models", "pricing"],
    "read_stock": ["csvio", "dates", "errors", "models", "money"],
}


@pytest.mark.parametrize("name", sorted(ACCESS_MODULES))
def test_reading_an_old_name_loads_only_its_module(tmp_path: Path, name: str) -> None:
    result = probe(tmp_path, None, (name,))
    needed = {f"stockroom.{m}" for m in ACCESS_MODULES[name]}
    loaded = set(result["modules"])
    assert needed <= loaded, sorted(needed - loaded)
    assert loaded <= needed | set(ALWAYS), sorted(loaded - needed - set(ALWAYS))


def test_old_names_remain_the_same_objects() -> None:
    listed = dir(cli)
    for name, module in HOME.items():
        home = importlib.import_module(f"stockroom.{module}")
        assert getattr(cli, name) is getattr(home, name), name
        namespace: dict[str, object] = {}
        exec(f"from stockroom.cli import {name}", namespace)
        assert namespace[name] is getattr(home, name), name
        assert name in listed, name
    for name in ("main", "COMMANDS"):
        assert name in listed
    assert not hasattr(cli, "no_such_name")
    with pytest.raises(AttributeError):
        cli.read_everything  # noqa: B018
    with pytest.raises(ImportError):
        exec("from stockroom.cli import read_everything", {})


# -- replaced names are used by the commands -----------------------------------------------------


def _fake_items(text: str):
    from stockroom.models import Item

    assert text.startswith("sku,name,price")
    return [Item("ZZZ-999", "Stand-in Item", 4321, ("tea", "gift"), 7)]


def _fake_stock(text: str):
    assert text.startswith("sku,on_hand")
    return [("MUG-001", 1, 1), ("PEN-100", 40, 0)]


def _fake_orders(text: str):
    from stockroom.models import Order, OrderLine

    assert text.startswith("order_id,")
    return [
        Order(
            "Q-1", "Stand-in Customer", date(2024, 5, 6), (OrderLine("MUG-001", 300, 999),), "paid"
        )
    ]


def _fake_date(text: str) -> date:
    return date(2024, 2, 1)


def _inventory_class():
    from stockroom.inventory import Inventory

    class EverythingStocked(Inventory):
        def low_stock(self, items):
            return []

    return EverythingStocked


def _order_book_class():
    from stockroom.orders import OrderBook

    class FirstOrderOnly(OrderBook):
        def __init__(self, orders=()) -> None:
            super().__init__(list(orders)[:1])

    return FirstOrderOnly


def _fake_money(cents: int, symbol: str = "$") -> str:
    return f"<{symbol}|{cents}>"


def _fake_discount(cents: int, percent: float) -> int:
    return cents // 2


def _fake_tax(cents: int, rate_bp: int) -> int:
    return 777


def _fake_summary(book, *, symbol: str = "$", top: int = 3) -> str:
    return f"{len(book)} order(s) in {symbol}"


def _fake_config(path, env):
    return {"currency_symbol": "EUR ", "low_stock_warning": False, "tax_bp": 1000, "data_dir": "x"}


ITEMS = ["items", "--file", "{I}"]
STOCK = ["stock", "--items", "{I}", "--stock", "{S}"]
SALES = ["sales", "--orders", "{O}"]
PRICE = ["price", "--items", "{I}", "--sku", "PEN-100", "--quantity", "5", "--discount", "10"]
FAKE_PRICE = ["price", "--items", "{I}", "--sku", "ZZZ-999", "--quantity", "2"]

# case -> (name to replace, factory of the replacement, argv)
PATCH_CASES = {
    "read_items-items": ("read_items", lambda: _fake_items, ITEMS),
    "read_items-price": ("read_items", lambda: _fake_items, FAKE_PRICE),
    "read_items-stock": ("read_items", lambda: _fake_items, STOCK),
    "read_stock": ("read_stock", lambda: _fake_stock, STOCK),
    "read_orders": ("read_orders", lambda: _fake_orders, SALES),
    "parse_date": ("parse_date", lambda: _fake_date, [*SALES, "--from", "soon"]),
    "Inventory": ("Inventory", _inventory_class, STOCK),
    "OrderBook": ("OrderBook", _order_book_class, SALES),
    "format_money-items": ("format_money", lambda: _fake_money, ITEMS),
    "format_money-price": ("format_money", lambda: _fake_money, PRICE),
    "apply_discount": ("apply_discount", lambda: _fake_discount, PRICE),
    "tax": ("tax", lambda: _fake_tax, PRICE),
    "sales_summary": ("sales_summary", lambda: _fake_summary, SALES),
    "load_config": ("load_config", lambda: _fake_config, PRICE),
}

# (exit code, stdout, stderr) recorded before the change with the same replacement in place.
PATCH_GOLD: dict[str, tuple[int, str, str]] = {
    "Inventory": (
        0,
        "stock ok\n",
        "",
    ),
    "OrderBook": (
        0,
        "1 order, 0 cancelled\n"
        "Revenue: $339.50\n"
        "\n"
        "Month    Revenue\n"
        "-------  -------\n"
        "2024-01  $339.50\n"
        "\n"
        "Customer           Total\n"
        "---------------  -------\n"
        "Hana Kimura-Lee  $339.50\n",
        "",
    ),
    "apply_discount": (
        0,
        "5 x PEN-100 Fountain Pen: $85.00\ntax: $0.00\ntotal: $85.00\n",
        "",
    ),
    "format_money-items": (
        0,
        "SKU      Name            Price\n"
        "-------  --------------  --------\n"
        "MUG-001  Blue Mug        <$|1250>\n"
        "MUG-002  Red Mug         <$|1250>\n"
        "TEA-010  Green Tea 100g   <$|895>\n"
        "TEA-011  Black Tea 100g   <$|740>\n"
        "PEN-100  Fountain Pen    <$|3400>\n"
        "NB-200   Notebook A5      <$|625>\n"
        "LMP-030  Desk Lamp       <$|4999>\n"
        "CND-007  Beeswax Candle   <$|980>\n",
        "",
    ),
    "format_money-price": (
        0,
        "5 x PEN-100 Fountain Pen: <$|15300>\ntax: <$|0>\ntotal: <$|15300>\n",
        "",
    ),
    "load_config": (
        0,
        "5 x PEN-100 Fountain Pen: EUR 153.00\ntax: EUR 15.30\ntotal: EUR 168.30\n",
        "",
    ),
    "parse_date": (
        0,
        "2 orders, 1 cancelled\n"
        "Revenue: $442.00\n"
        "\n"
        "Month    Revenue\n"
        "-------  -------\n"
        "2024-02  $294.00\n"
        "2024-03  $148.00\n"
        "\n"
        "Customer              Total\n"
        "------------------  -------\n"
        "Hana Kimura-Lee     $294.00\n"
        "Lena Berg-Holmgren  $148.00\n",
        "",
    ),
    "read_items-items": (
        0,
        "SKU      Name           Price\n"
        "-------  -------------  ------\n"
        "ZZZ-999  Stand-in Item  $43.21\n",
        "",
    ),
    "read_items-price": (
        0,
        "2 x ZZZ-999 Stand-in Item: $86.42\ntax: $0.00\ntotal: $86.42\n",
        "",
    ),
    "read_items-stock": (
        0,
        "LOW ZZZ-999 Stand-in Item: 0/7\n",
        "warning: 1 item(s) below reorder level\n",
    ),
    "read_orders": (
        0,
        "1 order, 0 cancelled\n"
        "Revenue: $2,997.00\n"
        "\n"
        "Month      Revenue\n"
        "-------  ---------\n"
        "2024-05  $2,997.00\n"
        "\n"
        "Customer               Total\n"
        "-----------------  ---------\n"
        "Stand-in Customer  $2,997.00\n",
        "",
    ),
    "read_stock": (
        0,
        "LOW MUG-001 Blue Mug: 0/5\n"
        "LOW MUG-002 Red Mug: 0/5\n"
        "LOW TEA-010 Green Tea 100g: 0/10\n"
        "LOW TEA-011 Black Tea 100g: 0/10\n"
        "LOW NB-200 Notebook A5: 0/20\n"
        "LOW LMP-030 Desk Lamp: 0/1\n"
        "LOW CND-007 Beeswax Candle: 0/8\n",
        "warning: 7 item(s) below reorder level\n",
    ),
    "sales_summary": (
        0,
        "5 order(s) in $\n",
        "",
    ),
    "tax": (
        0,
        "5 x PEN-100 Fountain Pen: $153.00\ntax: $7.77\ntotal: $160.77\n",
        "",
    ),
}


@pytest.fixture
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith("STOCKROOM_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("COLUMNS", "80")
    for name, text in (
        ("items.csv", ITEMS_CSV),
        ("stock.csv", STOCK_CSV),
        ("orders.csv", ORDERS_CSV),
    ):
        (tmp_path / name).write_text(text, encoding="utf-8")
    return {
        "{I}": str(tmp_path / "items.csv"),
        "{S}": str(tmp_path / "stock.csv"),
        "{O}": str(tmp_path / "orders.csv"),
    }


def run_main(argv: list[str], files: dict[str, str], capsys) -> tuple[int, str, str]:
    try:
        code = cli.main([files.get(a, a) for a in argv])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("case", sorted(PATCH_CASES))
def test_a_replaced_name_is_used_by_the_commands(
    case: str, files: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    name, factory, argv = PATCH_CASES[case]
    monkeypatch.setattr(cli, name, factory())
    assert run_main(argv, files, capsys) == PATCH_GOLD[case]
    monkeypatch.undo()
    assert getattr(cli, name) is getattr(importlib.import_module(f"stockroom.{HOME[name]}"), name)


# -- output before and after ---------------------------------------------------------------------

CASES = {
    "no-args": [],
    "help": ["--help"],
    "help-items": ["items", "--help"],
    "help-stock": ["stock", "--help"],
    "help-sales": ["sales", "--help"],
    "help-price": ["price", "--help"],
    "version": ["--version"],
    "items-missing-file": ["items"],
    "bad-command": ["frobnicate"],
    "price-bad-quantity-type": ["price", "--items", "{I}", "--sku", "MUG-001", "--quantity", "x"],
    "items": ITEMS,
    "items-tag": [*ITEMS, "--tag", "Tea"],
    "stock": STOCK,
    "sales": SALES,
    "sales-from": [*SALES, "--from", "2024-02-01"],
    "sales-bad-from": [*SALES, "--from", "soon"],
    "price": PRICE,
    "price-unknown-sku": ["price", "--items", "{I}", "--sku", "XYZ-999", "--quantity", "1"],
    "price-zero-quantity": ["price", "--items", "{I}", "--sku", "XYZ-999", "--quantity", "0"],
}

# (exit code, stdout, stderr) recorded from the command before the change.
GOLD: dict[str, tuple[int, str, str]] = {
    "bad-command": (
        2,
        "",
        "usage: stockroom [-h] [--version] [--config CONFIG]\n"
        "                 {items,stock,sales,price} ...\n"
        "stockroom: error: argument command: invalid choice: 'frobnicate' (choose fro"
        "m 'items', 'stock', 'sales', 'price')\n",
    ),
    "help": (
        0,
        "usage: stockroom [-h] [--version] [--config CONFIG]\n"
        "                 {items,stock,sales,price} ...\n"
        "\n"
        "Inventory and order ledger.\n"
        "\n"
        "positional arguments:\n"
        "  {items,stock,sales,price}\n"
        "    items               list the catalogue\n"
        "    stock               list items below their reorder level\n"
        "    sales               sales summary\n"
        "    price               price a quantity of one item\n"
        "\n"
        "options:\n"
        "  -h, --help            show this help message and exit\n"
        "  --version             show program's version number and exit\n"
        "  --config CONFIG       INI settings file\n",
        "",
    ),
    "help-items": (
        0,
        "usage: stockroom items [-h] --file FILE [--tag TAG]\n"
        "\n"
        "options:\n"
        "  -h, --help   show this help message and exit\n"
        "  --file FILE\n"
        "  --tag TAG    only items with this tag\n",
        "",
    ),
    "help-price": (
        0,
        "usage: stockroom price [-h] --items ITEMS --sku SKU --quantity QUANTITY\n"
        "                       [--discount DISCOUNT]\n"
        "\n"
        "options:\n"
        "  -h, --help           show this help message and exit\n"
        "  --items ITEMS\n"
        "  --sku SKU\n"
        "  --quantity QUANTITY\n"
        "  --discount DISCOUNT  percent off\n",
        "",
    ),
    "help-sales": (
        0,
        "usage: stockroom sales [-h] --orders ORDERS [--from START] [--to END]\n"
        "\n"
        "options:\n"
        "  -h, --help       show this help message and exit\n"
        "  --orders ORDERS\n"
        "  --from START     first day (YYYY-MM-DD)\n"
        "  --to END         last day (YYYY-MM-DD)\n",
        "",
    ),
    "help-stock": (
        0,
        "usage: stockroom stock [-h] --items ITEMS --stock STOCK\n"
        "\n"
        "options:\n"
        "  -h, --help     show this help message and exit\n"
        "  --items ITEMS\n"
        "  --stock STOCK\n",
        "",
    ),
    "items": (
        0,
        "SKU      Name            Price\n"
        "-------  --------------  ------\n"
        "MUG-001  Blue Mug        $12.50\n"
        "MUG-002  Red Mug         $12.50\n"
        "TEA-010  Green Tea 100g   $8.95\n"
        "TEA-011  Black Tea 100g   $7.40\n"
        "PEN-100  Fountain Pen    $34.00\n"
        "NB-200   Notebook A5      $6.25\n"
        "LMP-030  Desk Lamp       $49.99\n"
        "CND-007  Beeswax Candle   $9.80\n",
        "",
    ),
    "items-missing-file": (
        2,
        "",
        "usage: stockroom items [-h] --file FILE [--tag TAG]\n"
        "stockroom items: error: the following arguments are required: --file\n",
    ),
    "items-tag": (
        0,
        "SKU      Name            Price\n"
        "-------  --------------  -----\n"
        "TEA-010  Green Tea 100g  $8.95\n"
        "TEA-011  Black Tea 100g  $7.40\n",
        "",
    ),
    "no-args": (
        2,
        "",
        "usage: stockroom [-h] [--version] [--config CONFIG]\n"
        "                 {items,stock,sales,price} ...\n"
        "stockroom: error: the following arguments are required: command\n",
    ),
    "price": (
        0,
        "5 x PEN-100 Fountain Pen: $153.00\ntax: $0.00\ntotal: $153.00\n",
        "",
    ),
    "price-bad-quantity-type": (
        2,
        "",
        "usage: stockroom price [-h] --items ITEMS --sku SKU --quantity QUANTITY\n"
        "                       [--discount DISCOUNT]\n"
        "stockroom price: error: argument --quantity: invalid int value: 'x'\n",
    ),
    "price-unknown-sku": (
        1,
        "",
        "stockroom: error: no item XYZ-999\n",
    ),
    "price-zero-quantity": (
        1,
        "",
        "stockroom: error: quantity must be positive\n",
    ),
    "sales": (
        0,
        "4 orders, 1 cancelled\n"
        "Revenue: $951.50\n"
        "\n"
        "Month    Revenue\n"
        "-------  -------\n"
        "2024-01  $509.50\n"
        "2024-02  $294.00\n"
        "2024-03  $148.00\n"
        "\n"
        "Customer              Total\n"
        "------------------  -------\n"
        "Hana Kimura-Lee     $633.50\n"
        "Omar Diaz-Fuentes   $170.00\n"
        "Lena Berg-Holmgren  $148.00\n",
        "",
    ),
    "sales-bad-from": (
        1,
        "",
        "stockroom: error: not a date: 'soon'\n",
    ),
    "sales-from": (
        0,
        "2 orders, 1 cancelled\n"
        "Revenue: $442.00\n"
        "\n"
        "Month    Revenue\n"
        "-------  -------\n"
        "2024-02  $294.00\n"
        "2024-03  $148.00\n"
        "\n"
        "Customer              Total\n"
        "------------------  -------\n"
        "Hana Kimura-Lee     $294.00\n"
        "Lena Berg-Holmgren  $148.00\n",
        "",
    ),
    "stock": (
        0,
        "LOW MUG-002 Red Mug: 4/5\n"
        "LOW TEA-011 Black Tea 100g: 9/10\n"
        "LOW PEN-100 Fountain Pen: 1/2\n",
        "warning: 3 item(s) below reorder level\n",
    ),
    "version": (
        0,
        "stockroom 0.4.0\n",
        "",
    ),
}


def _choices_without_quotes(text: str) -> str:
    # argparse quotes the choices of an invalid choice in some Python versions only
    return re.sub(
        r"\(choose from ([^)]*)\)",
        lambda m: "(choose from " + m.group(1).replace("'", "") + ")",
        text,
    )


@pytest.mark.parametrize("case", sorted(CASES))
def test_command_output_is_unchanged(case: str, files: dict[str, str], capsys) -> None:
    code, out, err = run_main(CASES[case], files, capsys)
    gold_code, gold_out, gold_err = GOLD[case]
    assert (code, out, _choices_without_quotes(err)) == (
        gold_code,
        gold_out,
        _choices_without_quotes(gold_err),
    )


def test_settings_from_the_environment_still_reach_the_commands(
    files: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("STOCKROOM_TAX_BP", "825")
    monkeypatch.setenv("STOCKROOM_CURRENCY_SYMBOL", "EUR ")
    assert run_main(PRICE, files, capsys) == (
        0,
        "5 x PEN-100 Fountain Pen: EUR 153.00\ntax: EUR 12.62\ntotal: EUR 165.62\n",
        "",
    )
    monkeypatch.setenv("STOCKROOM_TAX_BP", "lots")
    assert run_main(PRICE, files, capsys) == (
        1,
        "",
        "stockroom: error: tax_bp must be a whole number, not 'lots'\n",
    )
