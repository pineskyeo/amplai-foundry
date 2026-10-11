from __future__ import annotations

import ast
import importlib
import os
from datetime import date
from pathlib import Path

import pytest
import stockroom
from stockroom import cli

PACKAGE = Path(stockroom.__file__).resolve().parent

TEMPLATES = {
    "NO_ITEM": "no item {sku}",
    "QUANTITY_NOT_POSITIVE": "quantity must be positive",
    "SETTING_NOT_WHOLE": "{key} must be a whole number, not {value!r}",
    "SETTING_NEGATIVE": "{key} must not be negative",
    "SETTINGS_UNREADABLE": "cannot read {path}: {error}",
    "SETTING_UNKNOWN": "unknown setting {key!r} in {path}",
    "SETTING_NOT_BOOLEAN": "{key} must be a boolean",
    "CELL_NOT_WHOLE": "line {line}: {field} is not a whole number: {value!r}",
    "ITEM_FILE_EMPTY": "empty item file",
    "ITEM_HEADER_WRONG": "item header must be {header}",
    "FIELD_COUNT_WRONG": "line {line}: expected {count} fields",
    "STOCK_HEADER_WRONG": "stock header must be {header}",
    "ORDER_HEADER_WRONG": "order header must be {header}",
    "DATE_INVALID": "not a date: {text!r}",
    "MONTH_INVALID": "no month {month}",
    "LEVELS_INCONSISTENT": "{sku}: inconsistent levels {on_hand}/{reserved}",
    "ONLY_AVAILABLE": "{sku}: only {count} available",
    "ONLY_RESERVED": "{sku}: only {count} reserved",
    "ORDER_OBJECT_BAD": "bad order object: {error}",
    "JSON_INVALID": "not JSON: {error}",
    "JSON_NOT_A_LIST": "an order file holds a JSON list",
    "SKU_INVALID": "bad sku: {sku!r}",
    "NAME_EMPTY": "{sku}: empty name",
    "PRICE_NEGATIVE": "{sku}: negative price",
    "REORDER_LEVEL_NEGATIVE": "{sku}: negative reorder level",
    "LINE_QUANTITY_NOT_POSITIVE": "{sku}: quantity must be positive",
    "UNIT_PRICE_NEGATIVE": "{sku}: negative unit price",
    "ORDER_ID_EMPTY": "empty order id",
    "STATUS_UNKNOWN": "{order_id}: unknown status {status!r}",
    "AMOUNT_INVALID": "not an amount: {text!r}",
    "PARTS_NOT_POSITIVE": "parts must be positive",
    "ORDER_ID_DUPLICATE": "duplicate order id {order_id}",
    "ORDER_MISSING": "no order {order_id}",
    "PERCENT_OUT_OF_RANGE": "percent must be between 0 and 100",
    "RATE_NEGATIVE": "rate must not be negative",
    "WIDTH_NOT_POSITIVE": "width must be positive",
    "ALIGNMENT_UNKNOWN": "unknown alignment: {how!r}",
}

ITEMS_CSV = """sku,name,price,tags,reorder_level
MUG-001,Blue Mug,12.50,kitchen;gift,5
PEN-100,Fountain Pen,34.00,office;gift,2
"""
ITEM_HEADER = "sku,name,price,tags,reorder_level\n"
STOCK_HEADER = "sku,on_hand,reserved\n"
ORDER_HEADER = "order_id,customer,placed,status,sku,quantity,unit_price\n"


def _cli(tmp_path: Path, argv: list[str]) -> str:
    """The message of a data error reported by the command line."""
    import contextlib
    import io

    items = tmp_path / "items.csv"
    items.write_text(ITEMS_CSV, encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    saved = {k: os.environ.pop(k) for k in [k for k in os.environ if k.startswith("STOCKROOM_")]}
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([str(items) if a == "{I}" else a for a in argv])
    finally:
        os.environ.update(saved)
    assert code == 1 and out.getvalue() == ""
    text = err.getvalue()
    assert text.startswith("stockroom: error: ") and text.endswith("\n")
    return text[len("stockroom: error: ") : -1]


def _settings(tmp_path: Path, text: str | None, env: dict[str, str] | None = None):
    """Load settings from ``text`` (no file for ``None``, a missing file for ``"missing"``)."""
    from stockroom.config import load_config

    path = None
    if text == "missing":
        path = tmp_path / "missing.ini"
    elif text is not None:
        path = tmp_path / "s.ini"
        path.write_text(text, encoding="utf-8")
    return load_config(path, env)


def _inventory():
    from stockroom.inventory import Inventory

    inventory = Inventory()
    inventory.receive("MUG-001", 2)
    return inventory


def _order(order_id: str = "A-1", status: str = "open"):
    from stockroom.models import Order

    return Order(order_id, "Ann Example", date(2024, 1, 5), (), status)


def _call(module: str, name: str, *args):
    return getattr(importlib.import_module(f"stockroom.{module}"), name)(*args)


# site -> (template, how to make the error happen); "cli" sites report through cli.main
SITES = {
    "cli-no-item": (
        "NO_ITEM",
        "cli",
        ["price", "--items", "{I}", "--sku", "XYZ-999", "--quantity", "1"],
    ),
    "cli-quantity": (
        "QUANTITY_NOT_POSITIVE",
        "cli",
        ["price", "--items", "{I}", "--sku", "MUG-001", "--quantity", "0"],
    ),
    "inventory-quantity": (
        "QUANTITY_NOT_POSITIVE",
        lambda tmp: _call("inventory", "Inventory").receive("MUG-001", 0),
    ),
    "settings-not-whole": (
        "SETTING_NOT_WHOLE",
        lambda tmp: _settings(tmp, None, {"STOCKROOM_TAX_BP": "lots"}),
    ),
    "settings-negative": (
        "SETTING_NEGATIVE",
        lambda tmp: _settings(tmp, "[stockroom]\ntax_bp = -5\n"),
    ),
    "settings-unreadable": ("SETTINGS_UNREADABLE", lambda tmp: _settings(tmp, "missing")),
    "settings-unknown": (
        "SETTING_UNKNOWN",
        lambda tmp: _settings(tmp, "[stockroom]\ncolour = blue\n"),
    ),
    "settings-not-boolean": (
        "SETTING_NOT_BOOLEAN",
        lambda tmp: _settings(tmp, "[stockroom]\nlow_stock_warning = maybe\n"),
    ),
    "items-cell": (
        "CELL_NOT_WHOLE",
        lambda tmp: _call("csvio", "read_items", ITEM_HEADER + "MUG-001,Mug,1.00,,x\n"),
    ),
    "orders-cell": (
        "CELL_NOT_WHOLE",
        lambda tmp: _call(
            "csvio", "read_orders", ORDER_HEADER + "A-1,Ann,2024-01-05,paid,MUG-001,two,1.00\n"
        ),
    ),
    "items-empty": ("ITEM_FILE_EMPTY", lambda tmp: _call("csvio", "read_items", "")),
    "items-header": (
        "ITEM_HEADER_WRONG",
        lambda tmp: _call("csvio", "read_items", "sku,name\nMUG-001,Mug\n"),
    ),
    "items-field-count": (
        "FIELD_COUNT_WRONG",
        lambda tmp: _call("csvio", "read_items", ITEM_HEADER + "MUG-001,Mug\n"),
    ),
    "stock-field-count": (
        "FIELD_COUNT_WRONG",
        lambda tmp: _call("csvio", "read_stock", STOCK_HEADER + "MUG-001,1\n"),
    ),
    "stock-header": (
        "STOCK_HEADER_WRONG",
        lambda tmp: _call("csvio", "read_stock", "sku,count\nMUG-001,3\n"),
    ),
    "orders-header": (
        "ORDER_HEADER_WRONG",
        lambda tmp: _call("csvio", "read_orders", "order_id,customer\nA-1,Ann\n"),
    ),
    "date": ("DATE_INVALID", lambda tmp: _call("dates", "parse_date", "soon")),
    "month": ("MONTH_INVALID", lambda tmp: _call("dates", "days_in_month", 2024, 13)),
    "levels": (
        "LEVELS_INCONSISTENT",
        lambda tmp: _call("inventory", "Inventory").from_levels([("MUG-001", 1, 2)]),
    ),
    "reserve": ("ONLY_AVAILABLE", lambda tmp: _inventory().reserve("MUG-001", 3)),
    "release": ("ONLY_RESERVED", lambda tmp: _inventory().release("MUG-001", 1)),
    "ship": ("ONLY_RESERVED", lambda tmp: _inventory().ship("MUG-001", 1)),
    "json-object": (
        "ORDER_OBJECT_BAD",
        lambda tmp: _call("jsonio", "load_orders", '[{"order_id": "A-1"}]'),
    ),
    "json-text": ("JSON_INVALID", lambda tmp: _call("jsonio", "load_orders", "{not json")),
    "json-list": (
        "JSON_NOT_A_LIST",
        lambda tmp: _call("jsonio", "load_orders", '{"order_id": "A-1"}'),
    ),
    "sku": ("SKU_INVALID", lambda tmp: _call("models", "check_sku", "mug-1")),
    "item-name": ("NAME_EMPTY", lambda tmp: _call("models", "Item", "MUG-001", "  ", 100)),
    "item-price": ("PRICE_NEGATIVE", lambda tmp: _call("models", "Item", "MUG-001", "Mug", -1)),
    "item-reorder": (
        "REORDER_LEVEL_NEGATIVE",
        lambda tmp: _call("models", "Item", "MUG-001", "Mug", 1, (), -1),
    ),
    "line-quantity": (
        "LINE_QUANTITY_NOT_POSITIVE",
        lambda tmp: _call("models", "OrderLine", "MUG-001", 0, 100),
    ),
    "line-price": (
        "UNIT_PRICE_NEGATIVE",
        lambda tmp: _call("models", "OrderLine", "MUG-001", 1, -1),
    ),
    "order-id": ("ORDER_ID_EMPTY", lambda tmp: _order(" ")),
    "order-status": ("STATUS_UNKNOWN", lambda tmp: _order("A-7", "lost")),
    "amount": ("AMOUNT_INVALID", lambda tmp: _call("money", "parse_money", "abc")),
    "parts": ("PARTS_NOT_POSITIVE", lambda tmp: _call("money", "split_evenly", 10, 0)),
    "duplicate": (
        "ORDER_ID_DUPLICATE",
        lambda tmp: _call("orders", "OrderBook", [_order("A-2"), _order("A-2")]),
    ),
    "missing": ("ORDER_MISSING", lambda tmp: _call("orders", "OrderBook").get("A-9")),
    "percent": ("PERCENT_OUT_OF_RANGE", lambda tmp: _call("pricing", "apply_discount", 100, 120)),
    "rate": ("RATE_NEGATIVE", lambda tmp: _call("pricing", "tax", 100, -1)),
    "width": ("WIDTH_NOT_POSITIVE", lambda tmp: _call("textutil", "truncate", "x", 0)),
    "alignment": ("ALIGNMENT_UNKNOWN", lambda tmp: _call("textutil", "align", "x", 3, "middle")),
}

# site -> (exception class, message, class of __cause__ or None, __suppress_context__),
# recorded before the change; "<D>" stands for the test's temporary directory.
GOLD: dict[str, tuple] = {
    "alignment": (
        "ValueError",
        "unknown alignment: 'middle'",
        None,
        False,
    ),
    "amount": (
        "ParseError",
        "not an amount: 'abc'",
        None,
        False,
    ),
    "cli-no-item": (
        "cli",
        "no item XYZ-999",
        None,
        False,
    ),
    "cli-quantity": (
        "cli",
        "quantity must be positive",
        None,
        False,
    ),
    "date": (
        "ParseError",
        "not a date: 'soon'",
        "ValueError",
        True,
    ),
    "duplicate": (
        "StockroomError",
        "duplicate order id A-2",
        None,
        False,
    ),
    "inventory-quantity": (
        "StockError",
        "quantity must be positive",
        None,
        False,
    ),
    "item-name": (
        "ParseError",
        "MUG-001: empty name",
        None,
        False,
    ),
    "item-price": (
        "ParseError",
        "MUG-001: negative price",
        None,
        False,
    ),
    "item-reorder": (
        "ParseError",
        "MUG-001: negative reorder level",
        None,
        False,
    ),
    "items-cell": (
        "ParseError",
        "line 2: reorder_level is not a whole number: 'x'",
        "ValueError",
        True,
    ),
    "items-empty": (
        "ParseError",
        "empty item file",
        None,
        False,
    ),
    "items-field-count": (
        "ParseError",
        "line 2: expected 5 fields",
        None,
        False,
    ),
    "items-header": (
        "ParseError",
        "item header must be sku,name,price,tags,reorder_level",
        None,
        False,
    ),
    "json-list": (
        "ParseError",
        "an order file holds a JSON list",
        None,
        False,
    ),
    "json-object": (
        "ParseError",
        "bad order object: 'customer'",
        "KeyError",
        True,
    ),
    "json-text": (
        "ParseError",
        "not JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
        "JSONDecodeError",
        True,
    ),
    "levels": (
        "StockError",
        "MUG-001: inconsistent levels 1/2",
        None,
        False,
    ),
    "line-price": (
        "ParseError",
        "MUG-001: negative unit price",
        None,
        False,
    ),
    "line-quantity": (
        "ParseError",
        "MUG-001: quantity must be positive",
        None,
        False,
    ),
    "missing": (
        "StockroomError",
        "no order A-9",
        None,
        True,
    ),
    "month": (
        "ValueError",
        "no month 13",
        None,
        False,
    ),
    "order-id": (
        "ParseError",
        "empty order id",
        None,
        False,
    ),
    "order-status": (
        "ParseError",
        "A-7: unknown status 'lost'",
        None,
        False,
    ),
    "orders-cell": (
        "ParseError",
        "line 2: quantity is not a whole number: 'two'",
        "ValueError",
        True,
    ),
    "orders-header": (
        "ParseError",
        "order header must be order_id,customer,placed,status,sku,quantity,unit_price",
        None,
        False,
    ),
    "parts": (
        "ValueError",
        "parts must be positive",
        None,
        False,
    ),
    "percent": (
        "ValueError",
        "percent must be between 0 and 100",
        None,
        False,
    ),
    "rate": (
        "ValueError",
        "rate must not be negative",
        None,
        False,
    ),
    "release": (
        "StockError",
        "MUG-001: only 0 reserved",
        None,
        False,
    ),
    "reserve": (
        "StockError",
        "MUG-001: only 2 available",
        None,
        False,
    ),
    "settings-negative": (
        "ConfigError",
        "tax_bp must not be negative",
        None,
        False,
    ),
    "settings-not-boolean": (
        "ConfigError",
        "low_stock_warning must be a boolean",
        "ValueError",
        True,
    ),
    "settings-not-whole": (
        "ConfigError",
        "tax_bp must be a whole number, not 'lots'",
        "ValueError",
        True,
    ),
    "settings-unknown": (
        "ConfigError",
        "unknown setting 'colour' in <D>/s.ini",
        None,
        False,
    ),
    "settings-unreadable": (
        "ConfigError",
        "cannot read <D>/missing.ini: [Errno 2] No such file or directory: '<D>/missing.ini'",
        "FileNotFoundError",
        True,
    ),
    "ship": (
        "StockError",
        "MUG-001: only 0 reserved",
        None,
        False,
    ),
    "sku": (
        "ParseError",
        "bad sku: 'mug-1'",
        None,
        False,
    ),
    "stock-field-count": (
        "ParseError",
        "line 2: expected 3 fields",
        None,
        False,
    ),
    "stock-header": (
        "ParseError",
        "stock header must be sku,on_hand,reserved",
        None,
        False,
    ),
    "width": (
        "ValueError",
        "width must be positive",
        None,
        False,
    ),
}


def outcome(site: str, tmp_path: Path) -> tuple:
    _, how, *rest = SITES[site]
    if how == "cli":
        return ("cli", _cli(tmp_path, rest[0]).replace(str(tmp_path), "<D>"), None, False)
    with pytest.raises(Exception) as raised:
        how(tmp_path)
    exc = raised.value
    cause = type(exc.__cause__).__name__ if exc.__cause__ is not None else None
    message = str(exc).replace(str(tmp_path), "<D>")
    return (type(exc).__name__, message, cause, exc.__suppress_context__)


@pytest.mark.parametrize("site", sorted(SITES))
def test_every_error_keeps_class_message_and_cause(site: str, tmp_path: Path) -> None:
    assert outcome(site, tmp_path) == GOLD[site]


@pytest.mark.parametrize("site", sorted(SITES))
def test_a_replaced_template_changes_the_message(
    site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stockroom import messages

    name = SITES[site][0]
    monkeypatch.setattr(messages, name, "«" + TEMPLATES[name] + "»")
    kind, message, cause, suppressed = GOLD[site]
    assert outcome(site, tmp_path) == (kind, "«" + message + "»", cause, suppressed)
    monkeypatch.undo()
    assert outcome(site, tmp_path) == GOLD[site]


def test_the_templates_live_in_one_module() -> None:
    from stockroom import messages

    public = {k: v for k, v in vars(messages).items() if k.isupper()}
    assert public == TEMPLATES
    assert all(type(v) is str for v in public.values())
    tree = ast.parse((PACKAGE / "messages.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # the docstring
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        assert isinstance(node, (ast.Assign, ast.AnnAssign)), ast.dump(node)[:80]
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        assert all(isinstance(t, ast.Name) for t in targets)
        names = [t.id for t in targets]
        assert names == ["__all__"] or (
            isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        ), names


def test_no_raise_statement_spells_out_a_message() -> None:
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name == "messages.py" and path.parent == PACKAGE:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Raise):
                continue
            for part in ast.walk(node):
                if isinstance(part, ast.JoinedStr):
                    offenders.append((path.name, part.lineno, "f-string"))
                elif (
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and any(ch.isalpha() for ch in part.value)
                ):
                    offenders.append((path.name, part.lineno, part.value))
    assert offenders == []


def test_templates_are_not_copied_into_other_modules() -> None:
    texts = set(TEMPLATES.values())
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name == "messages.py" and path.parent == PACKAGE:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and node.value in texts:
                offenders.append((path.name, node.lineno, node.value))
    assert offenders == []


def test_the_message_module_depends_on_nothing_in_the_package() -> None:
    tree = ast.parse((PACKAGE / "messages.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "__future__", ast.dump(node)
        assert not isinstance(node, ast.Import)


def test_command_line_reports_the_same_errors(tmp_path: Path) -> None:
    cases = {
        ("price", "--items", "{I}", "--sku", "XYZ-999", "--quantity", "1"): "no item XYZ-999",
        (
            "price",
            "--items",
            "{I}",
            "--sku",
            "XYZ-999",
            "--quantity",
            "0",
        ): "quantity must be positive",
        ("sales", "--orders", str(tmp_path / "o.csv"), "--from", "later"): "not a date: 'later'",
        (
            "items",
            "--file",
            str(tmp_path / "bad.csv"),
        ): "line 3: reorder_level is not a whole number: ' many'",
        (
            "stock",
            "--items",
            "{I}",
            "--stock",
            str(tmp_path / "s.csv"),
        ): "stock header must be sku,on_hand,reserved",
    }
    (tmp_path / "o.csv").write_text(ORDER_HEADER, encoding="utf-8")
    (tmp_path / "bad.csv").write_text(
        ITEM_HEADER + "MUG-001,Mug,1.00,,3\nPEN-100,Pen,2.00,, many\n", encoding="utf-8"
    )
    (tmp_path / "s.csv").write_text("sku,count\n", encoding="utf-8")
    for argv, message in cases.items():
        assert _cli(tmp_path, list(argv)) == message
