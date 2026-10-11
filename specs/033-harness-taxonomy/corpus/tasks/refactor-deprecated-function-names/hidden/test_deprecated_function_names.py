from __future__ import annotations

import ast
import inspect
import os
import random
import string
import subprocess
import sys
import warnings
from pathlib import Path

import pytest
import stockroom
from stockroom import csvio, money, report, textutil
from stockroom.inventory import Inventory
from stockroom.models import Item

PACKAGE = Path(stockroom.__file__).resolve().parent
ROOT = PACKAGE.parent

# old name -> (module, new name)
RENAMES = {
    "plural": (textutil, "count_label"),
    "top_n": (report, "largest_totals"),
    "split_evenly": (money, "allocate_cents"),
    "parse_tags": (csvio, "split_tags"),
}
METHOD_OLD, METHOD_NEW = "low_stock", "below_reorder_level"
METHOD_MESSAGE = "Inventory.low_stock() is deprecated; use Inventory.below_reorder_level()"


def message(old: str) -> str:
    return f"{old}() is deprecated; use {RENAMES[old][1]}()"


def new(old: str):
    module, name = RENAMES[old]
    return getattr(module, name)


def recorded(call):
    """(result or raised exception, [(category, message, filename)]) of ``call()``."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            result = ("ok", call())
        except Exception as exc:  # the class and text of an error are compared
            result = ("error", type(exc).__name__, str(exc))
    return result, [(w.category, str(w.message), w.filename) for w in caught]


# -- the functions as they were before the change (the oracle) -----------------------------------


def _o_plural(count, word):
    return f"{count} {word if count == 1 else word + 's'}"


def _o_top_n(totals, n):
    ranked = sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))
    return ranked[: n or None]


def _o_split_evenly(total, parts):
    if parts <= 0:
        raise ValueError("parts must be positive")
    base, extra = divmod(total, parts)
    return [base + 1 if i < extra else base for i in range(parts)]


def _o_parse_tags(cell):
    return tuple(tag.strip().lower() for tag in cell.split(";") if tag.strip())


def _o_low_stock(inventory, items):
    return [item for item in items if inventory.available(item.sku) < item.reorder_level]


def _outcome(function, *args):
    try:
        return ("ok", function(*args))
    except Exception as exc:
        return ("error", type(exc).__name__, str(exc))


TAG_CHARS = string.ascii_letters + " ;;;-_Äßİ\t"


def generated_cases(rng: random.Random):
    for _ in range(600):
        word = rng.choice(["order", "item", "box", "", "Bus", "ox", "s", "child"])
        yield "plural", (rng.choice([-2, -1, 0, 1, 1, 2, 3, 11, 10**6]), word)
        names = rng.sample(["ann", "Bob", "cy", "Dee", "eve", "fay", "Gus"], rng.randrange(0, 7))
        totals = {name: rng.choice([0, 5, 5, 120, -3, rng.randrange(-50, 500)]) for name in names}
        yield "top_n", (totals, rng.randrange(1, 9))
        yield "split_evenly", (rng.randrange(-1000, 1001), rng.choice([1, 2, 3, 7, 12, 0, -1]))
        yield "parse_tags", ("".join(rng.choice(TAG_CHARS) for _ in range(rng.randrange(0, 16))),)


ORACLES = {
    "plural": _o_plural,
    "top_n": _o_top_n,
    "split_evenly": _o_split_evenly,
    "parse_tags": _o_parse_tags,
}


def test_new_names_behave_like_the_old_functions() -> None:
    rng = random.Random(5150)
    counts = dict.fromkeys(RENAMES, 0)
    for old, args in generated_cases(rng):
        expected = _outcome(ORACLES[old], *args)
        assert _outcome(new(old), *args) == expected, (old, args)
        result, caught = recorded(lambda old=old, args=args: getattr(RENAMES[old][0], old)(*args))
        assert result == expected, (old, args)
        assert caught == [(DeprecationWarning, message(old), __file__)]
        counts[old] += 1
    assert all(count == 600 for count in counts.values())


def make_inventory(rng: random.Random) -> tuple[Inventory, list[Item]]:
    skus = [f"MUG-{n:03d}" for n in range(1, 9)]
    levels = []
    for sku in rng.sample(skus, rng.randrange(0, 8)):
        on_hand = rng.randrange(0, 20)
        levels.append((sku, on_hand, rng.randrange(0, on_hand + 1)))
    items = [
        Item(sku, f"Item {sku}", rng.randrange(0, 999), (), rng.randrange(0, 15))
        for sku in rng.sample(skus, rng.randrange(0, 8))
    ]
    return Inventory.from_levels(levels), items


def test_inventory_method_behaves_like_the_old_one() -> None:
    rng = random.Random(8080)
    for _ in range(300):
        inventory, items = make_inventory(rng)
        expected = _o_low_stock(inventory, items)
        found = inventory.below_reorder_level(iter(items))
        assert len(found) == len(expected) and all(
            a is b for a, b in zip(found, expected, strict=True)
        )
        result, caught = recorded(
            lambda inventory=inventory, items=items: inventory.low_stock(items)
        )
        assert result[0] == "ok" and result[1] == expected
        assert caught == [(DeprecationWarning, METHOD_MESSAGE, __file__)]


def test_new_functions_have_their_own_names() -> None:
    for module, name in RENAMES.values():
        function = getattr(module, name)
        assert inspect.isfunction(function)
        assert (function.__name__, function.__qualname__, function.__module__) == (
            name,
            name,
            module.__name__,
        )
    method = Inventory.below_reorder_level
    assert (method.__name__, method.__qualname__) == (METHOD_NEW, "Inventory.below_reorder_level")


def test_old_names_warn_once_per_call_before_they_run() -> None:
    cases = [
        ("plural", (2, "order"), ("ok", "2 orders")),
        ("top_n", ({"a": 1, "b": 2}, 1), ("ok", [("b", 2)])),
        ("split_evenly", (10, 0), ("error", "ValueError", "parts must be positive")),
        ("split_evenly", (7, 2), ("ok", [4, 3])),
        (
            "top_n",
            (None, 1),
            ("error", "AttributeError", "'NoneType' object has no attribute 'items'"),
        ),
        ("parse_tags", (5,), ("error", "AttributeError", "'int' object has no attribute 'split'")),
    ]
    for old, args, expected in cases:
        module = RENAMES[old][0]
        result, caught = recorded(
            lambda module=module, old=old, args=args: getattr(module, old)(*args)
        )
        assert result == expected
        assert caught == [(DeprecationWarning, message(old), __file__)]
    result, caught = recorded(lambda: Inventory().low_stock(None))
    assert result[:2] == ("error", "TypeError")
    assert caught == [(DeprecationWarning, METHOD_MESSAGE, __file__)]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        textutil.count_label(2, "order")
        report.largest_totals({"a": 1}, 1)
        money.allocate_cents(5, 2)
        csvio.split_tags("a;b")
        Inventory().below_reorder_level([])
    assert caught == []


def test_old_names_are_described_like_the_new_ones() -> None:
    for old, (module, name) in RENAMES.items():
        alias, target = getattr(module, old), getattr(module, name)
        assert inspect.isfunction(alias), old
        assert (alias.__name__, alias.__qualname__, alias.__module__) == (old, old, module.__name__)
        assert alias.__wrapped__ is target, old
        assert inspect.signature(alias) == inspect.signature(target), old
    alias = Inventory.low_stock
    assert inspect.isfunction(alias)
    assert (alias.__name__, alias.__qualname__) == (METHOD_OLD, "Inventory.low_stock")
    assert alias.__wrapped__ is Inventory.below_reorder_level
    assert inspect.signature(alias) == inspect.signature(Inventory.below_reorder_level)


CALL_ARGS = {
    "plural": (1, "x"),
    "top_n": ({"a": 1}, 2),
    "split_evenly": (1, 2),
    "parse_tags": ("x",),
}


def test_old_names_follow_the_new_ones_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    for old, (module, name) in RENAMES.items():
        monkeypatch.setattr(module, name, lambda *args, name=name: ("replaced", name, args))
        args = CALL_ARGS[old]
        result, _ = recorded(lambda module=module, old=old, args=args: getattr(module, old)(*args))
        assert result == ("ok", ("replaced", name, args)), old

    class Everything(Inventory):
        def below_reorder_level(self, items):
            return ["overridden", list(items)]

    result, caught = recorded(lambda: Everything().low_stock(["x"]))
    assert result == ("ok", ["overridden", ["x"]])
    assert caught == [(DeprecationWarning, METHOD_MESSAGE, __file__)]


def _chain(target: ast.AST) -> list[str]:
    """``Inventory.low_stock.__wrapped__`` -> ``["Inventory", "low_stock", "__wrapped__"]``."""
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.insert(0, target.attr)
        target = target.value
    return [target.id, *parts] if isinstance(target, ast.Name) else []


def _alias_nodes(tree: ast.Module, old: str, in_class: str | None) -> set[int]:
    """ids of the nodes that define the alias ``old``: its def or assignment (in the module, or
    in the class body / as ``Class.old = ...`` for the method), and assignments to the alias's
    own attributes such as ``__wrapped__``."""
    scopes: list[tuple[list[ast.stmt], list[str]]] = [(tree.body, [old])]
    if in_class is not None:
        scopes = [(tree.body, [in_class, old])]
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == in_class:
                scopes.append((node.body, [old]))
    exempt: set[int] = set()
    for body, prefix in scopes:
        for stmt in body:
            own = False
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and prefix == [stmt.name]:
                own = True
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                own = any(_chain(t)[: len(prefix)] == prefix for t in targets)
            if own:
                exempt |= {id(node) for node in ast.walk(stmt)}
    return exempt


def test_nothing_in_the_package_uses_the_old_names() -> None:
    homes = {"textutil.py": ("plural", None), "report.py": ("top_n", None)}
    homes |= {"money.py": ("split_evenly", None), "csvio.py": ("parse_tags", None)}
    homes |= {"inventory.py": (METHOD_OLD, "Inventory")}
    old_names = {*RENAMES, METHOD_OLD}
    offenders = []
    files = sorted(PACKAGE.rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        exempt: set[int] = set()
        if path.parent == PACKAGE and path.name in homes:
            exempt = _alias_nodes(tree, *homes[path.name])
        for node in ast.walk(tree):
            if id(node) in exempt:
                continue
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.alias):
                name = node.asname or node.name.split(".")[-1]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name
            if name in old_names:
                offenders.append((path.name, getattr(node, "lineno", 0), name))
    assert offenders == []


ITEMS_CSV = """sku,name,price,tags,reorder_level
MUG-001,Blue Mug,12.50,Kitchen; GIFT,5
MUG-002,Red Mug,12.50,kitchen,5
TEA-010,Green Tea 100g,8.95,food;tea;,10
TEA-011,Black Tea 100g,7.40, Food ;Tea,10
PEN-100,Fountain Pen,34.00,office;gift,2
NB-200,Notebook A5,6.25,,20
"""
STOCK_CSV = """sku,on_hand,reserved
MUG-001,14,2
MUG-002,4,0
TEA-010,30,5
TEA-011,9,0
PEN-100,3,2
NB-200,40,4
"""
ORDERS_CSV = """order_id,customer,placed,status,sku,quantity,unit_price
Z-1,Hana Kimura-Lee,2024-01-08,shipped,MUG-001,20,12.50
Z-2,Omar Diaz-Fuentes,2024-01-19,paid,PEN-100,5,34.00
Z-3,Lena Berg-Holmgren,2024-02-03,cancelled,NB-200,1,49.99
Z-4,Ravi Nair-Iyer,2024-02-14,paid,TEA-010,30,9.80
Z-5,Lena Berg-Holmgren,2024-03-02,open,TEA-011,20,7.40
"""

CLI_CASES = {
    "items": ["items", "--file", "{I}"],
    "items-tag": ["items", "--file", "{I}", "--tag", "TEA"],
    "stock": ["stock", "--items", "{I}", "--stock", "{S}"],
    "sales": ["sales", "--orders", "{O}"],
    "sales-from": ["sales", "--orders", "{O}", "--from", "2024-02-01"],
}

# (exit code, stdout, stderr) recorded from the command before the change.
CLI_GOLD: dict[str, tuple[int, str, str]] = {
    "items": (
        0,
        "SKU      Name            Price\n"
        "-------  --------------  ------\n"
        "MUG-001  Blue Mug        $12.50\n"
        "MUG-002  Red Mug         $12.50\n"
        "TEA-010  Green Tea 100g   $8.95\n"
        "TEA-011  Black Tea 100g   $7.40\n"
        "PEN-100  Fountain Pen    $34.00\n"
        "NB-200   Notebook A5      $6.25\n",
        "",
    ),
    "items-tag": (
        0,
        "SKU      Name            Price\n"
        "-------  --------------  -----\n"
        "TEA-010  Green Tea 100g  $8.95\n"
        "TEA-011  Black Tea 100g  $7.40\n",
        "",
    ),
    "sales": (
        0,
        "4 orders, 1 cancelled\n"
        "Revenue: $862.00\n"
        "\n"
        "Month    Revenue\n"
        "-------  -------\n"
        "2024-01  $420.00\n"
        "2024-02  $294.00\n"
        "2024-03  $148.00\n"
        "\n"
        "Customer             Total\n"
        "-----------------  -------\n"
        "Ravi Nair-Iyer     $294.00\n"
        "Hana Kimura-Lee    $250.00\n"
        "Omar Diaz-Fuentes  $170.00\n",
        "",
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
        "Ravi Nair-Iyer      $294.00\n"
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
}


def test_command_line_output_is_unchanged_and_warning_free(tmp_path: Path) -> None:
    (tmp_path / "items.csv").write_text(ITEMS_CSV, encoding="utf-8")
    (tmp_path / "stock.csv").write_text(STOCK_CSV, encoding="utf-8")
    (tmp_path / "orders.csv").write_text(ORDERS_CSV, encoding="utf-8")
    files = {"{I}": "items.csv", "{S}": "stock.csv", "{O}": "orders.csv"}
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    for name, argv in sorted(CLI_CASES.items()):
        done = subprocess.run(
            [sys.executable, "-W", "error::DeprecationWarning", "-m", "stockroom"]
            + [files.get(a, a) for a in argv],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert (done.returncode, done.stdout, done.stderr) == CLI_GOLD[name], name
    done = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::DeprecationWarning",
            str(ROOT / "scripts" / "make_sample_data.py"),
            "--out",
            "sample",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr


# recorded before the change: report.sales_summary of ORDERS_CSV with top=2
SUMMARY_GOLD = (
    "4 orders, 1 cancelled\n"
    "Revenue: $862.00\n"
    "\n"
    "Month    Revenue\n"
    "-------  -------\n"
    "2024-01  $420.00\n"
    "2024-02  $294.00\n"
    "2024-03  $148.00\n"
    "\n"
    "Customer           Total\n"
    "---------------  -------\n"
    "Ravi Nair-Iyer   $294.00\n"
    "Hana Kimura-Lee  $250.00"
)


def test_reports_and_readers_are_unchanged() -> None:
    from stockroom.orders import OrderBook

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        items = csvio.read_items(ITEMS_CSV)
        book = OrderBook(csvio.read_orders(ORDERS_CSV))
        text = report.sales_summary(book, top=2)
        listed = Inventory.from_levels(csvio.read_stock(STOCK_CSV)).below_reorder_level(items)
    assert caught == []
    assert [i.tags for i in items] == [
        ("kitchen", "gift"),
        ("kitchen",),
        ("food", "tea"),
        ("food", "tea"),
        ("office", "gift"),
        (),
    ]
    assert text == SUMMARY_GOLD
    assert [i.sku for i in listed] == ["MUG-002", "TEA-011", "PEN-100"]
