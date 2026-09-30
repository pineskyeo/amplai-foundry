import ast
import tomllib
from datetime import date
from pathlib import Path

import pytest

import stockroom
from stockroom.errors import ParseError

PACKAGE = Path(stockroom.__file__).resolve().parent
ROOT = PACKAGE.parent
SHIMS = {
    "money": ("format_money", "parse_money", "split_evenly"),
    "textutil": ("align", "plural", "slugify", "truncate"),
    "dates": (
        "add_months",
        "days_between",
        "days_in_month",
        "format_date",
        "month_key",
        "months_between",
        "parse_date",
    ),
}


def test_the_helpers_live_in_a_util_subpackage() -> None:
    import importlib

    util = importlib.import_module("stockroom.util")
    assert hasattr(util, "__path__")
    assert (PACKAGE / "util" / "__init__.py").is_file()
    for module, names in SHIMS.items():
        real = importlib.import_module(f"stockroom.util.{module}")
        for name in names:
            assert getattr(real, name).__module__ == f"stockroom.util.{module}", (module, name)


def test_the_old_module_paths_still_work_and_give_the_same_objects() -> None:
    import importlib

    for module, names in SHIMS.items():
        old = importlib.import_module(f"stockroom.{module}")
        new = importlib.import_module(f"stockroom.util.{module}")
        for name in names:
            assert getattr(old, name) is getattr(new, name), (module, name)
        assert set(old.__all__) == set(names), module


def test_the_old_modules_are_thin_shims() -> None:
    for module in SHIMS:
        tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
        for node in tree.body:
            is_docstring = isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            is_import = isinstance(node, (ast.Import, ast.ImportFrom))
            is_all = isinstance(node, (ast.Assign, ast.AnnAssign)) and "__all__" in ast.unparse(node)
            assert is_docstring or is_import or is_all, (module, ast.dump(node)[:80])


def test_package_modules_import_the_helpers_from_util() -> None:
    names = set(SHIMS)
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if relative.parent == Path(".") and path.stem in names:
            continue  # the shims themselves
        inside_util = relative.parts[0] == "util"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level == 1 and not inside_util:
                    if module in names or (module == "" and {a.name for a in node.names} & names):
                        offenders.append((str(relative), node.lineno))
                if node.level == 0 and module.split(".")[0] == "stockroom":
                    parts = module.split(".")
                    if parts[1:2] and parts[1] in names:
                        offenders.append((str(relative), node.lineno))
                    if module == "stockroom" and {a.name for a in node.names} & names:
                        offenders.append((str(relative), node.lineno))
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if parts[0] == "stockroom" and parts[1:2] and parts[1] in names:
                        offenders.append((str(relative), node.lineno))
    assert offenders == []


def test_the_new_subpackage_is_part_of_the_distribution() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packages = data["tool"]["setuptools"]["packages"]
    assert isinstance(packages, list)
    assert "stockroom" in packages and "stockroom.util" in packages


def test_money_functions_are_unchanged() -> None:
    from stockroom.util import money

    assert money.parse_money("12.34") == 1234
    assert money.parse_money("7") == 700
    assert money.parse_money("0.5") == 50
    assert money.parse_money("$1,250.00") == 125000
    assert money.parse_money("  $3.10 ") == 310
    assert money.parse_money("-3.50") == -350
    for bad in ("", "abc", "1.234", "--2", "12.", "$", "1 2"):
        with pytest.raises(ParseError):
            money.parse_money(bad)
    assert money.format_money(1234) == "$12.34"
    assert money.format_money(5) == "$0.05"
    assert money.format_money(-123456) == "-$1,234.56"
    assert money.format_money(990, symbol="EUR ") == "EUR 9.90"
    assert money.split_evenly(100, 3) == [34, 33, 33]
    assert money.split_evenly(90, 3) == [30, 30, 30]
    assert money.split_evenly(-5, 2) == [-2, -3]
    with pytest.raises(ValueError):
        money.split_evenly(5, 0)


def test_text_functions_are_unchanged() -> None:
    from stockroom.util import textutil

    assert textutil.slugify("Blue Mug, Large") == "blue-mug-large"
    assert textutil.slugify("  --Hello__World!! ") == "hello-world"
    assert textutil.truncate("short", 10) == "short"
    assert textutil.truncate("exact", 5) == "exact"
    with pytest.raises(ValueError):
        textutil.truncate("x", 0)
    assert textutil.align("ab", 5) == "ab   "
    assert textutil.align("ab", 5, "right") == "   ab"
    assert textutil.align("ab", 5, "center") == "  ab "
    with pytest.raises(ValueError):
        textutil.align("ab", 5, "justify")
    assert textutil.plural(1, "order") == "1 order"
    assert textutil.plural(0, "order") == "0 orders"
    assert textutil.plural(3, "order") == "3 orders"


def test_date_functions_are_unchanged() -> None:
    from stockroom.util import dates

    assert dates.parse_date(" 2023-03-09 ") == date(2023, 3, 9)
    for bad in ("", "2023-13-01", "9 March", "2023-02-30"):
        with pytest.raises(ParseError):
            dates.parse_date(bad)
    assert dates.format_date(date(2023, 3, 9)) == "2023-03-09"
    assert dates.month_key(date(2023, 3, 9)) == "2023-03"
    assert dates.days_in_month(2023, 2) == 28
    assert dates.days_in_month(2023, 4) == 30
    assert dates.days_in_month(2023, 12) == 31
    with pytest.raises(ValueError):
        dates.days_in_month(2023, 13)
    assert dates.add_months(date(2023, 1, 31), 1) == date(2023, 2, 28)
    assert dates.add_months(date(2023, 11, 15), 3) == date(2024, 2, 15)
    assert dates.add_months(date(2023, 3, 31), -1) == date(2023, 2, 28)
    assert dates.add_months(date(2023, 1, 15), -13) == date(2021, 12, 15)
    assert dates.days_between(date(2023, 1, 1), date(2023, 3, 1)) == 59
    assert dates.days_between(date(2023, 3, 1), date(2023, 1, 1)) == -59
    assert dates.months_between(date(2023, 11, 20), date(2024, 2, 1)) == [
        "2023-11",
        "2023-12",
        "2024-01",
        "2024-02",
    ]
    assert dates.months_between(date(2023, 5, 1), date(2023, 4, 1)) == []


def test_modules_that_use_the_helpers_still_work_together() -> None:
    from stockroom.csvio import read_orders, write_orders
    from stockroom.jsonio import dump_orders, load_orders
    from stockroom.orders import OrderBook
    from stockroom.report import sales_summary

    text = (
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        "A-1,Hana Kimura-Lee,2023-01-31,paid,MUG-001,20,12.50\n"
        "A-2,Omar Diaz-Fuentes,2023-02-14,open,TEA-010,40,8.95\n"
    )
    orders = read_orders(text)
    assert write_orders(orders) == text
    dumped = load_orders(dump_orders(orders))
    assert [(o.order_id, o.placed, o.lines) for o in dumped] == [
        (o.order_id, o.placed, o.lines) for o in orders
    ]
    book = OrderBook(orders)
    assert book.revenue_by_month() == {"2023-01": 25000, "2023-02": 35800}
    assert sales_summary(book, symbol="EUR ").splitlines()[1] == "Revenue: EUR 608.00"
