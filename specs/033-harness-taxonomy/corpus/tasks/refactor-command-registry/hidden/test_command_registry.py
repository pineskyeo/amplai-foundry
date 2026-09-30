from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path

import pytest

from stockroom import cli

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
    'no-args': (2, '', 'usage: stockroom [-h] [--version] [--config CONFIG]\n                 {items,stock,sales,price} ...\nstockroom: error: the following arguments are required: command\n'),
    'help': (0, "usage: stockroom [-h] [--version] [--config CONFIG]\n                 {items,stock,sales,price} ...\n\nInventory and order ledger.\n\npositional arguments:\n  {items,stock,sales,price}\n    items               list the catalogue\n    stock               list items below their reorder level\n    sales               sales summary\n    price               price a quantity of one item\n\noptions:\n  -h, --help            show this help message and exit\n  --version             show program's version number and exit\n  --config CONFIG       INI settings file\n", ''),
    'help-items': (0, 'usage: stockroom items [-h] --file FILE [--tag TAG]\n\noptions:\n  -h, --help   show this help message and exit\n  --file FILE\n  --tag TAG    only items with this tag\n', ''),
    'help-stock': (0, 'usage: stockroom stock [-h] --items ITEMS --stock STOCK\n\noptions:\n  -h, --help     show this help message and exit\n  --items ITEMS\n  --stock STOCK\n', ''),
    'help-sales': (0, 'usage: stockroom sales [-h] --orders ORDERS [--from START] [--to END]\n\noptions:\n  -h, --help       show this help message and exit\n  --orders ORDERS\n  --from START     first day (YYYY-MM-DD)\n  --to END         last day (YYYY-MM-DD)\n', ''),
    'help-price': (0, 'usage: stockroom price [-h] --items ITEMS --sku SKU --quantity QUANTITY\n                       [--discount DISCOUNT]\n\noptions:\n  -h, --help           show this help message and exit\n  --items ITEMS\n  --sku SKU\n  --quantity QUANTITY\n  --discount DISCOUNT  percent off\n', ''),
    'version': (0, 'stockroom 0.4.0\n', ''),
    'items-missing-file': (2, '', 'usage: stockroom items [-h] --file FILE [--tag TAG]\nstockroom items: error: the following arguments are required: --file\n'),
    'stock-missing': (2, '', 'usage: stockroom stock [-h] --items ITEMS --stock STOCK\nstockroom stock: error: the following arguments are required: --stock\n'),
    'sales-missing': (2, '', 'usage: stockroom sales [-h] --orders ORDERS [--from START] [--to END]\nstockroom sales: error: the following arguments are required: --orders\n'),
    'price-missing': (2, '', 'usage: stockroom price [-h] --items ITEMS --sku SKU --quantity QUANTITY\n                       [--discount DISCOUNT]\nstockroom price: error: the following arguments are required: --quantity\n'),
    'bad-command': (2, '', "usage: stockroom [-h] [--version] [--config CONFIG]\n                 {items,stock,sales,price} ...\nstockroom: error: argument command: invalid choice: 'frobnicate' (choose from 'items', 'stock', 'sales', 'price')\n"),
    'price-bad-quantity-type': (2, '', "usage: stockroom price [-h] --items ITEMS --sku SKU --quantity QUANTITY\n                       [--discount DISCOUNT]\nstockroom price: error: argument --quantity: invalid int value: 'x'\n"),
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

CASES = {
    "no-args": [],
    "help": ["--help"],
    "help-items": ["items", "--help"],
    "help-stock": ["stock", "--help"],
    "help-sales": ["sales", "--help"],
    "help-price": ["price", "--help"],
    "version": ["--version"],
    "items-missing-file": ["items"],
    "stock-missing": ["stock", "--items", "x"],
    "sales-missing": ["sales"],
    "price-missing": ["price", "--items", "x", "--sku", "MUG-001"],
    "bad-command": ["frobnicate"],
    "price-bad-quantity-type": ["price", "--items", "{I}", "--sku", "MUG-001", "--quantity", "x"],
    "items": ["items", "--file", "{I}"],
    "items-tag": ["items", "--file", "{I}", "--tag", "Tea"],
    "stock": ["stock", "--items", "{I}", "--stock", "{S}"],
    "sales": ["sales", "--orders", "{O}"],
    "sales-from": ["sales", "--orders", "{O}", "--from", "2024-02-01"],
    "sales-bad-from": ["sales", "--orders", "{O}", "--from", "soon"],
    "price": ["price", "--items", "{I}", "--sku", "MUG-001", "--quantity", "3"],
    "price-discount": ["price", "--items", "{I}", "--sku", "PEN-100", "--quantity", "5", "--discount", "10"],
    "price-unknown-sku": ["price", "--items", "{I}", "--sku", "XYZ-999", "--quantity", "1"],
    "price-zero-quantity": ["price", "--items", "{I}", "--sku", "XYZ-999", "--quantity", "0"],
}


@pytest.fixture
def files(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "items.csv").write_text(ITEMS_CSV, encoding="utf-8")
    (tmp_path / "stock.csv").write_text(STOCK_CSV, encoding="utf-8")
    (tmp_path / "orders.csv").write_text(ORDERS_CSV, encoding="utf-8")
    return {
        "{I}": str(tmp_path / "items.csv"),
        "{S}": str(tmp_path / "stock.csv"),
        "{O}": str(tmp_path / "orders.csv"),
        "<D>": str(tmp_path),
    }


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], files: dict[str, str]):
    monkeypatch.setenv("COLUMNS", "80")
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
        real = [files.get(a, a) for a in argv]
        try:
            code = cli.main(real)
        except SystemExit as exc:
            code = exc.code
        captured = capsys.readouterr()
        return code, captured.out.replace(files["<D>"], "<D>"), captured.err.replace(files["<D>"], "<D>")

    return call


def _choices_without_quotes(text: str) -> str:
    # argparse prints the choice list of an invalid choice with quotes or without
    # them depending on the Python version (3.12.x drops them); compare without.
    return re.sub(
        r"\(choose from ([^)]*)\)",
        lambda m: "(choose from " + m.group(1).replace("'", "") + ")",
        text,
    )


def _plain(result: tuple[int, str, str]) -> tuple[int, str, str]:
    code, out, err = result
    return code, out, _choices_without_quotes(err)


@pytest.mark.parametrize("name", sorted(CASES))
def test_existing_commands_behave_as_before(name: str, run) -> None:
    assert _plain(run(CASES[name])) == _plain(GOLD[name])


def test_settings_reach_the_commands_as_before(run, tmp_path: Path, files: dict[str, str]) -> None:
    assert run(
        ["price", "--items", "{I}", "--sku", "PEN-100", "--quantity", "5", "--discount", "10"],
        {"STOCKROOM_TAX_BP": "825", "STOCKROOM_CURRENCY_SYMBOL": "EUR "},
    ) == GOLD["price-tax"]
    assert run(
        ["price", "--items", "{I}", "--sku", "PEN-100", "--quantity", "5"],
        {"STOCKROOM_TAX_BP": "lots"},
    ) == GOLD["price-bad-tax"]
    config = tmp_path / "s.ini"
    config.write_text("[stockroom]\ncolour = blue\n", encoding="utf-8")
    assert run(["--config", str(config), "items", "--file", "{I}"]) == GOLD["bad-config"]
    config.write_text("[stockroom]\ncurrency_symbol = GBP \nlow_stock_warning = no\n", encoding="utf-8")
    assert run(["--config", str(config), "stock", "--items", "{I}", "--stock", "{S}"]) == GOLD["config-quiet"]
    assert run(["--config", str(config), "items", "--file", "{I}"]) == GOLD["config-symbol"]


def test_command_is_a_frozen_record_of_four_fields() -> None:
    assert dataclasses.is_dataclass(cli.Command)
    assert [f.name for f in dataclasses.fields(cli.Command)] == ["name", "help", "configure", "run"]
    record = cli.Command("x", "h", lambda parser: None, lambda args, config: 0)
    assert (record.name, record.help) == ("x", "h")
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.name = "other"  # type: ignore[misc]


def test_the_registry_holds_the_four_commands_in_order() -> None:
    assert isinstance(cli.COMMANDS, dict)
    assert list(cli.COMMANDS) == ["items", "stock", "sales", "price"]
    helps = {
        "items": "list the catalogue",
        "stock": "list items below their reorder level",
        "sales": "sales summary",
        "price": "price a quantity of one item",
    }
    for key, command in cli.COMMANDS.items():
        assert isinstance(command, cli.Command)
        assert command.name == key and command.help == helps[key]
        assert callable(command.configure) and callable(command.run)


def make_command(name: str, help: str = "say hello") -> cli.Command:
    def configure(parser) -> None:
        parser.add_argument("--who", default="world")
        parser.add_argument("--shout", action="store_true")

    def run(args, config) -> int:
        text = f"hello {args.who} {config['currency_symbol']}"
        print(text.upper() if args.shout else text)
        return 3

    return cli.Command(name=name, help=help, configure=configure, run=run)


def test_a_registered_command_is_parsed_run_and_listed(
    run, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(cli.COMMANDS, "hello", make_command("hello"))
    assert run(["hello"]) == (3, "hello world $\n", "")
    assert run(["hello", "--who", "Zed", "--shout"]) == (3, "HELLO ZED $\n", "")
    code, out, err = run(["--help"])
    assert code == 0 and err == ""
    assert "{items,stock,sales,price,hello}" in out
    assert "    hello               say hello\n" in out
    code, out, err = run(["hello", "--help"])
    assert code == 0 and "usage: stockroom hello [-h] [--who WHO] [--shout]" in out
    code, out, err = run(["hello", "--bogus"])
    assert code == 2 and "stockroom: error: unrecognized arguments: --bogus" in err


def test_a_command_gets_the_loaded_settings(run, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(cli.COMMANDS, "hello", make_command("hello"))
    config = tmp_path / "s.ini"
    config.write_text("[stockroom]\ncurrency_symbol = GBP\n", encoding="utf-8")
    assert run(["--config", str(config), "hello"]) == (3, "hello world GBP\n", "")
    assert run(["--config", str(config), "hello"], {"STOCKROOM_CURRENCY_SYMBOL": "EUR"}) == (
        3,
        "hello world EUR\n",
        "",
    )


def test_data_errors_of_a_registered_command_exit_with_one(
    run, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stockroom.errors import ParseError, StockError

    for exc in (ParseError("bad input"), StockError("no stock")):
        def boom(args, config, exc=exc) -> int:
            print("partial")
            raise exc

        monkeypatch.setitem(cli.COMMANDS, "boom", cli.Command("boom", "fails", lambda p: None, boom))
        assert run(["boom"]) == (1, "partial\n", f"stockroom: error: {exc}\n")


@pytest.mark.parametrize("name", ["items", "stock", "sales", "price"])
def test_every_command_is_built_from_its_record(
    name: str, run, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []

    def configure(parser) -> None:
        parser.add_argument("--flag", action="store_true")

    def runner(args, config) -> int:
        seen.append((args.command, args.flag))
        return 7

    monkeypatch.setitem(cli.COMMANDS, name, cli.Command(name, "custom help text", configure, runner))
    assert run([name, "--flag"]) == (7, "", "")
    assert seen == [(name, True)]
    code, out, err = run(["--help"])
    assert f"    {name}" in out and "custom help text" in out
    code, out, err = run([name, "--help"])
    assert code == 0 and f"usage: stockroom {name} [-h] [--flag]" in out


def test_a_removed_command_is_no_longer_accepted(run, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(cli.COMMANDS, "price")
    code, out, err = run(["price", "--items", "{I}", "--sku", "MUG-001", "--quantity", "1"])
    assert code == 2 and out == "" and "invalid choice: 'price'" in err
    assert "(choose from items, stock, sales)" in _choices_without_quotes(err)
    code, out, err = run(["--help"])
    assert "{items,stock,sales}" in out and "price" not in out
    assert run(["items", "--file", "{I}"])[0] == 0


def test_the_registry_is_read_when_main_runs_not_when_the_module_loads(
    run, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = make_command("later", "added after import")
    assert run(["later"])[0] == 2
    monkeypatch.setitem(cli.COMMANDS, "later", first)
    assert run(["later"])[0] == 3
