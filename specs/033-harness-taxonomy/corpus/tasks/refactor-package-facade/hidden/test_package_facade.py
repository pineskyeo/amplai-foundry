from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
from pathlib import Path

import stockroom

PACKAGE = Path(stockroom.__file__).resolve().parent
ROOT = PACKAGE.parent

FACADE = [
    ("StockroomError", "errors"),
    ("ParseError", "errors"),
    ("StockError", "errors"),
    ("ConfigError", "errors"),
    ("Item", "models"),
    ("OrderLine", "models"),
    ("Order", "models"),
    ("parse_money", "money"),
    ("format_money", "money"),
    ("parse_date", "dates"),
    ("format_date", "dates"),
    ("read_items", "csvio"),
    ("write_items", "csvio"),
    ("read_stock", "csvio"),
    ("read_orders", "csvio"),
    ("write_orders", "csvio"),
    ("dump_orders", "jsonio"),
    ("load_orders", "jsonio"),
    ("Inventory", "inventory"),
    ("OrderBook", "orders"),
    ("order_total", "pricing"),
    ("sales_summary", "report"),
    ("load_config", "config"),
    ("main", "cli"),
]


def module_names() -> dict[str, Path]:
    """Dotted name -> file of every module of the package."""
    found = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        parts = ["stockroom", *path.relative_to(PACKAGE).with_suffix("").parts]
        if parts[-1] == "__init__":
            parts = parts[:-1]
        found[".".join(parts)] = path
    return found


def public_names(tree: ast.Module) -> list[str]:
    """Names bound directly in the module body by def, class or assignment, not starting with _."""
    names: list[str] = []
    for stmt in tree.body:
        bound: list[str] = []
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound = [stmt.name]
        elif isinstance(stmt, ast.Assign):
            bound = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            bound = [stmt.target.id] if isinstance(stmt.target, ast.Name) else []
        for name in bound:
            if not name.startswith("_") and name not in names:
                names.append(name)
    return names


def test_every_module_lists_its_public_names_in_definition_order() -> None:
    checked = []
    for name, path in module_names().items():
        if name in ("stockroom", "stockroom.__main__"):
            continue
        module = importlib.import_module(name)
        expected = public_names(ast.parse(path.read_text(encoding="utf-8")))
        assert type(getattr(module, "__all__", None)) is list, name
        assert module.__all__ == expected, name
        namespace: dict[str, object] = {}
        exec(f"from {name} import *", namespace)
        assert sorted(k for k in namespace if k != "__builtins__") == sorted(expected), name
        checked.append(name)
    for name in ("stockroom.cli", "stockroom.config", "stockroom.models", "stockroom.textutil"):
        assert name in checked
    assert importlib.import_module("stockroom.models").__all__ == [
        "SKU",
        "STATUSES",
        "check_sku",
        "Item",
        "OrderLine",
        "Order",
    ]


def test_the_package_offers_the_facade() -> None:
    assert type(stockroom.__all__) is list
    assert stockroom.__all__ == [name for name, _ in FACADE]
    for name, module in FACADE:
        home = importlib.import_module(f"stockroom.{module}")
        assert getattr(stockroom, name) is getattr(home, name), name
    namespace: dict[str, object] = {}
    exec("from stockroom import *", namespace)
    assert {k for k in namespace if k != "__builtins__"} == {name for name, _ in FACADE}
    assert stockroom.__version__ == "0.4.0"


def import_graph() -> dict[str, set[str]]:
    modules = module_names()
    graph: dict[str, set[str]] = {name: set() for name in modules}
    for name, path in modules.items():
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for stmt in tree.body:
            targets: set[str] = set()
            if isinstance(stmt, ast.Import):
                targets |= {a.name for a in stmt.names if a.name in modules}
            elif isinstance(stmt, ast.ImportFrom):
                if stmt.level:
                    base = package.split(".")
                    base = base[: len(base) - (stmt.level - 1)]
                    source = ".".join(base + ([stmt.module] if stmt.module else []))
                else:
                    source = stmt.module or ""
                if source not in modules:
                    continue
                for alias in stmt.names:
                    sub = f"{source}.{alias.name}"
                    if sub in modules:
                        targets.add(sub)
                        if stmt.module:
                            targets.add(source)
                    else:
                        targets.add(source)
            graph[name] |= targets - {name}
    return graph


def find_cycle(graph: dict[str, set[str]]) -> list[str]:
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(node: str) -> list[str]:
        state[node] = 1
        path.append(node)
        for nxt in sorted(graph[node]):
            if state.get(nxt) == 1:
                return [*path[path.index(nxt) :], nxt]
            if nxt not in state:
                found = visit(nxt)
                if found:
                    return found
        path.pop()
        state[node] = 2
        return []

    for node in sorted(graph):
        if node not in state:
            found = visit(node)
            if found:
                return found
    return []


def test_the_import_graph_has_no_cycle() -> None:
    assert find_cycle(import_graph()) == []


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return env


def test_every_module_can_be_imported_first(tmp_path: Path) -> None:
    for name in sorted(module_names()):
        if name == "stockroom.__main__":
            continue
        done = subprocess.run(
            [sys.executable, "-c", f"import {name}"],
            cwd=tmp_path,
            env=_env(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == 0, (name, done.stderr[-500:])


ITEMS_CSV = """sku,name,price,tags,reorder_level
MUG-001,Blue Mug,12.50,kitchen;gift,5
PEN-100,Fountain Pen,34.00,office;gift,2
"""
ORDERS_CSV = """order_id,customer,placed,status,sku,quantity,unit_price
Z-1,Hana Kimura-Lee,2024-01-08,shipped,MUG-001,20,12.50
Z-2,Omar Diaz-Fuentes,2024-02-19,paid,PEN-100,5,34.00
"""


def test_the_command_line_and_script_still_work(tmp_path: Path) -> None:
    (tmp_path / "items.csv").write_text(ITEMS_CSV, encoding="utf-8")
    (tmp_path / "orders.csv").write_text(ORDERS_CSV, encoding="utf-8")
    cases = [
        (["--version"], "stockroom 0.4.0\n"),
        (
            ["items", "--file", "items.csv"],
            "SKU      Name          Price\n-------  ------------  ------\n"
            "MUG-001  Blue Mug      $12.50\nPEN-100  Fountain Pen  $34.00\n",
        ),
        (
            ["price", "--items", "items.csv", "--sku", "PEN-100", "--quantity", "2"],
            "2 x PEN-100 Fountain Pen: $68.00\ntax: $0.00\ntotal: $68.00\n",
        ),
        (
            ["sales", "--orders", "orders.csv"],
            "2 orders, 0 cancelled\nRevenue: $420.00\n\nMonth    Revenue\n-------  -------\n"
            "2024-01  $250.00\n2024-02  $170.00\n\nCustomer             Total\n"
            "-----------------  -------\nHana Kimura-Lee    $250.00\nOmar Diaz-Fuentes  $170.00\n",
        ),
    ]
    for argv, expected in cases:
        done = subprocess.run(
            [sys.executable, "-m", "stockroom", *argv],
            cwd=tmp_path,
            env=_env(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert (done.returncode, done.stdout, done.stderr) == (0, expected, ""), argv
    done = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "make_sample_data.py"),
            "--out",
            "sample",
            "--orders",
            "4",
        ],
        cwd=tmp_path,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "wrote 8 items and 4 orders to sample\n"
