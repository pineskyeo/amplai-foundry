import ast
import hashlib
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

import stockroom
from stockroom import csvio
from stockroom.errors import StockroomError
from stockroom.models import Item, Order, OrderLine

PACKAGE = Path(stockroom.__file__).resolve().parent
ROOT = PACKAGE.parent
NINE = {
    "ITEM_HEADER",
    "STOCK_HEADER",
    "ORDER_HEADER",
    "parse_tags",
    "read_items",
    "write_items",
    "read_stock",
    "read_orders",
    "write_orders",
}

IH = "sku,name,price,tags,reorder_level"
SH = "sku,on_hand,reserved"
OH = "order_id,customer,placed,status,sku,quantity,unit_price"

ITEM_INPUTS = [
    "",
    "\n\n  \n",
    "sku,name\nMUG-001,Mug\n",
    IH + "\n",
    IH + "\nMUG-001,Blue Mug,12.50,Kitchen; gift;,5\n",
    "\n\n" + IH + "\n\nMUG-001,Mug,1.00,a;b,3\n\n  \nTEA-010,Tea,$8.95,,0\n\n",
    " sku , name ,price,tags, reorder_level \nMUG-001, Blue Mug ,7,,2\n",
    IH + "\nMUG-001,Mug,1.00,,3\n\nTEA-010,Tea,1.00,,x\n",
    IH + "\nMUG-001,Mug,1.00,,3\n\n\nTEA-010,Tea\n",
    IH + "\nmug-1,Mug,1.00,,3\n",
    IH + "\nMUG-001,Mug,abc,,3\n",
    IH + "\nMUG-001,Mug,1.00,,-1\n",
    IH + "\nMUG-001,   ,1.00,,1\n",
    IH + "\nMUG-001,Mug,1.00,,1.5\n",
    IH + "\nMUG-001,Mug,1.00,,3,extra\n",
    IH + "\r\nMUG-001,Mug,2.25,x,4\r\nTEA-010,Tea,3.00,,0\r\n",
    IH + "\nMUG-001,Mug,0.5,,4\nMUG-002,Mug 2,100,,4",
]

STOCK_INPUTS = [
    "",
    "\n\n",
    "sku,count\nMUG-001,3\n",
    SH + "\n",
    SH + "\nMUG-001,14,2\nTEA-010,0,0\n",
    "\n\n" + SH + "\n\nMUG-001,14,2\n,,\n  , ,\nTEA-010,1,1\n\n",
    " sku , on_hand , reserved \nMUG-001, 5 , 1 \n",
    SH + "\nMUG-001,14\n",
    SH + "\nMUG-001,14,2\n\nTEA-010,x,0\n",
    SH + "\nMUG-001,14,2\n\n\nTEA-010,1,y\n",
    SH + "\nbad,1,1\n",
    SH + "\nMUG-001,14,2,9\n",
    SH + "\nMUG-001,-1,0\n",
    SH + "\nMUG-001,1.5,0\n",
    SH + "\r\nMUG-001,3,0\r\nTEA-010,4,1\r\n",
    SH + "\n\"MUG-001\",\"14\",\"2\"\n",
    SH + "\nMUG-001,14,2",
]

ORDER_INPUTS = [
    "",
    "\n",
    "order_id,customer\nA-1,Ann\n",
    OH + "\n",
    "\n" + OH + "\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50\n",
    " " + OH + "\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50\n",
    OH.replace(",", ", ") + "\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50\n",
    OH
    + "\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50\nB-2,Bob,2024-01-06,open,TEA-010,1,8.95\n"
    + "A-1,Ann,2024-01-05,paid,PEN-100,1,34.00\nB-2,Bob,2024-01-06,open,NB-200,3,6.25\n",
    OH
    + "\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50\n\nB-2,Bob,2024-01-06,open,TEA-010,1,8.95\n\n",
    OH + "\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50\n,,,,,,\nB-2,Bob,2024-01-06,open,TEA-010,1,8.95\n",
    OH + "\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50\nA-1,Zed,2024-09-09,cancelled,PEN-100,1,34.00\n",
    OH + "\n A-1 , Ann , 2024-01-05 , paid , MUG-001 , 2 , 12.50 \n",
    OH + "\nA-1,Ann,2024-01-05,paid,MUG-001,x,12.50\n",
    OH + "\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50\nA-2,Bob,2024-01-06,open,TEA-010,one,8.95\n",
    OH + "\nA-1,Ann,soon,paid,MUG-001,2,12.50\nA-2,Bob,2024-01-06,open,TEA-010,one,8.95\n",
    OH + "\nA-1,Ann,soon,paid,MUG-001,2,12.50\n",
    OH + "\nA-1,Ann,2024-01-05,lost,MUG-001,2,12.50\n",
    OH + "\n,Ann,2024-01-05,paid,MUG-001,2,12.50\n",
    OH + "\nA-1,Ann,2024-01-05,paid,mug,2,12.50\n",
    OH + "\nA-1,Ann,2024-01-05,paid,MUG-001,0,12.50\n",
    OH + "\nA-1,Ann,2024-01-05,paid,MUG-001,2,-3.50\n",
    OH + "\nA-1,Ann,2024-01-05,paid,MUG-001,2,abc\n",
    OH + "\nA-1,\"Ann, Jr\",2024-01-05,paid,MUG-001,2,\"1,250.00\"\n",
    OH + "\r\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50\r\nA-2,Bob,2024-02-01,shipped,TEA-010,1,8.95\r\n",
    OH + "\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50",
    OH + "\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50,extra\n",
]

# EXTRA
STOCK_INPUTS += [
    SH + ",\nMUG-001,1,0\n",
    "\n\n\n" + SH + "\nMUG-001,1,0\n,\n",
    SH + "\nMUG-001, 1 , 0 \n",
    SH + "\nMUG-001,1,0\n\n\nTEA-010,2\n",
]
ORDER_INPUTS += [
    OH + "\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50\n\nA-2,Bob,2024-01-06,open,TEA-010,x,8.95\n",
    OH + "\n\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50\n",
    OH + ",\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50,\n",
    OH.upper() + "\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50\n",
    OH + "\nA-1,Ann,2024-01-05,paid,MUG-001,2,12.50\n\n\nA-2,Bob,2024-01-06,open,TEA-010,1,8.95\nA-3,Cy,nope,open,TEA-010,1,8.95\n",
]

# Outcomes recorded from the readers before the change: ("ok", repr(result)) or
# ("error", exception class, message).
GOLD = {
    ('items', 0): ('error', 'ParseError', 'empty item file'),
    ('items', 1): ('error', 'ParseError', 'empty item file'),
    ('items', 2): ('error', 'ParseError', 'item header must be sku,name,price,tags,reorder_level'),
    ('items', 3): ('ok', '[]'),
    ('items', 4): ('ok', "[Item(sku='MUG-001', name='Blue Mug', price_cents=1250, tags=('kitchen', 'gift'), reorder_level=5)]"),
    ('items', 5): ('ok', "[Item(sku='MUG-001', name='Mug', price_cents=100, tags=('a', 'b'), reorder_level=3), Item(sku='TEA-010', name='Tea', price_cents=895, tags=(), reorder_level=0)]"),
    ('items', 6): ('ok', "[Item(sku='MUG-001', name='Blue Mug', price_cents=700, tags=(), reorder_level=2)]"),
    ('items', 7): ('error', 'ParseError', "line 3: reorder_level is not a whole number: 'x'"),
    ('items', 8): ('error', 'ParseError', 'line 3: expected 5 fields'),
    ('items', 9): ('error', 'ParseError', "bad sku: 'mug-1'"),
    ('items', 10): ('error', 'ParseError', "not an amount: 'abc'"),
    ('items', 11): ('error', 'ParseError', 'MUG-001: negative reorder level'),
    ('items', 12): ('error', 'ParseError', 'MUG-001: empty name'),
    ('items', 13): ('error', 'ParseError', "line 2: reorder_level is not a whole number: '1.5'"),
    ('items', 14): ('error', 'ParseError', 'line 2: expected 5 fields'),
    ('items', 15): ('ok', "[Item(sku='MUG-001', name='Mug', price_cents=225, tags=('x',), reorder_level=4), Item(sku='TEA-010', name='Tea', price_cents=300, tags=(), reorder_level=0)]"),
    ('items', 16): ('ok', "[Item(sku='MUG-001', name='Mug', price_cents=50, tags=(), reorder_level=4), Item(sku='MUG-002', name='Mug 2', price_cents=10000, tags=(), reorder_level=4)]"),
    ('stock', 0): ('error', 'ParseError', 'stock header must be sku,on_hand,reserved'),
    ('stock', 1): ('error', 'ParseError', 'stock header must be sku,on_hand,reserved'),
    ('stock', 2): ('error', 'ParseError', 'stock header must be sku,on_hand,reserved'),
    ('stock', 3): ('ok', '[]'),
    ('stock', 4): ('ok', "[('MUG-001', 14, 2), ('TEA-010', 0, 0)]"),
    ('stock', 5): ('ok', "[('MUG-001', 14, 2), ('TEA-010', 1, 1)]"),
    ('stock', 6): ('ok', "[('MUG-001', 5, 1)]"),
    ('stock', 7): ('error', 'ParseError', 'line 2: expected 3 fields'),
    ('stock', 8): ('error', 'ParseError', "line 3: on_hand is not a whole number: 'x'"),
    ('stock', 9): ('error', 'ParseError', "line 3: reserved is not a whole number: 'y'"),
    ('stock', 10): ('error', 'ParseError', "bad sku: 'bad'"),
    ('stock', 11): ('error', 'ParseError', 'line 2: expected 3 fields'),
    ('stock', 12): ('ok', "[('MUG-001', -1, 0)]"),
    ('stock', 13): ('error', 'ParseError', "line 2: on_hand is not a whole number: '1.5'"),
    ('stock', 14): ('ok', "[('MUG-001', 3, 0), ('TEA-010', 4, 1)]"),
    ('stock', 15): ('ok', "[('MUG-001', 14, 2)]"),
    ('stock', 16): ('ok', "[('MUG-001', 14, 2)]"),
    ('stock', 17): ('error', 'ParseError', 'stock header must be sku,on_hand,reserved'),
    ('stock', 18): ('ok', "[('MUG-001', 1, 0)]"),
    ('stock', 19): ('ok', "[('MUG-001', 1, 0)]"),
    ('stock', 20): ('error', 'ParseError', 'line 3: expected 3 fields'),
    ('orders', 0): ('error', 'ParseError', 'order header must be order_id,customer,placed,status,sku,quantity,unit_price'),
    ('orders', 1): ('error', 'ParseError', 'order header must be order_id,customer,placed,status,sku,quantity,unit_price'),
    ('orders', 2): ('error', 'ParseError', 'order header must be order_id,customer,placed,status,sku,quantity,unit_price'),
    ('orders', 3): ('ok', '[]'),
    ('orders', 4): ('error', 'ParseError', 'order header must be order_id,customer,placed,status,sku,quantity,unit_price'),
    ('orders', 5): ('error', 'ParseError', 'order header must be order_id,customer,placed,status,sku,quantity,unit_price'),
    ('orders', 6): ('error', 'ParseError', 'order header must be order_id,customer,placed,status,sku,quantity,unit_price'),
    ('orders', 7): ('ok', "[Order(order_id='A-1', customer='Ann', placed=datetime.date(2024, 1, 5), lines=(OrderLine(sku='MUG-001', quantity=2, unit_price_cents=1250), OrderLine(sku='PEN-100', quantity=1, unit_price_cents=3400)), status='paid'), Order(order_id='B-2', customer='Bob', placed=datetime.date(2024, 1, 6), lines=(OrderLine(sku='TEA-010', quantity=1, unit_price_cents=895), OrderLine(sku='NB-200', quantity=3, unit_price_cents=625)), status='open')]"),
    ('orders', 8): ('ok', "[Order(order_id='A-1', customer='Ann', placed=datetime.date(2024, 1, 5), lines=(OrderLine(sku='MUG-001', quantity=2, unit_price_cents=1250),), status='paid'), Order(order_id='B-2', customer='Bob', placed=datetime.date(2024, 1, 6), lines=(OrderLine(sku='TEA-010', quantity=1, unit_price_cents=895),), status='open')]"),
    ('orders', 9): ('error', 'ParseError', "line 3: quantity is not a whole number: ''"),
    ('orders', 10): ('ok', "[Order(order_id='A-1', customer='Ann', placed=datetime.date(2024, 1, 5), lines=(OrderLine(sku='MUG-001', quantity=2, unit_price_cents=1250), OrderLine(sku='PEN-100', quantity=1, unit_price_cents=3400)), status='paid')]"),
    ('orders', 11): ('ok', "[Order(order_id='A-1', customer='Ann', placed=datetime.date(2024, 1, 5), lines=(OrderLine(sku='MUG-001', quantity=2, unit_price_cents=1250),), status='paid')]"),
    ('orders', 12): ('error', 'ParseError', "line 2: quantity is not a whole number: 'x'"),
    ('orders', 13): ('error', 'ParseError', "line 3: quantity is not a whole number: 'one'"),
    ('orders', 14): ('error', 'ParseError', "line 3: quantity is not a whole number: 'one'"),
    ('orders', 15): ('error', 'ParseError', "not a date: 'soon'"),
    ('orders', 16): ('error', 'ParseError', "A-1: unknown status 'lost'"),
    ('orders', 17): ('error', 'ParseError', 'empty order id'),
    ('orders', 18): ('error', 'ParseError', "bad sku: 'mug'"),
    ('orders', 19): ('error', 'ParseError', 'MUG-001: quantity must be positive'),
    ('orders', 20): ('error', 'ParseError', 'MUG-001: negative unit price'),
    ('orders', 21): ('error', 'ParseError', "not an amount: 'abc'"),
    ('orders', 22): ('ok', "[Order(order_id='A-1', customer='Ann, Jr', placed=datetime.date(2024, 1, 5), lines=(OrderLine(sku='MUG-001', quantity=2, unit_price_cents=125000),), status='paid')]"),
    ('orders', 23): ('ok', "[Order(order_id='A-1', customer='Ann', placed=datetime.date(2024, 1, 5), lines=(OrderLine(sku='MUG-001', quantity=2, unit_price_cents=1250),), status='paid'), Order(order_id='A-2', customer='Bob', placed=datetime.date(2024, 2, 1), lines=(OrderLine(sku='TEA-010', quantity=1, unit_price_cents=895),), status='shipped')]"),
    ('orders', 24): ('ok', "[Order(order_id='A-1', customer='Ann', placed=datetime.date(2024, 1, 5), lines=(OrderLine(sku='MUG-001', quantity=2, unit_price_cents=1250),), status='paid')]"),
    ('orders', 25): ('ok', "[Order(order_id='A-1', customer='Ann', placed=datetime.date(2024, 1, 5), lines=(OrderLine(sku='MUG-001', quantity=2, unit_price_cents=1250),), status='paid')]"),
    ('orders', 26): ('error', 'ParseError', "line 3: quantity is not a whole number: 'x'"),
    ('orders', 27): ('ok', "[Order(order_id='A-1', customer='Ann', placed=datetime.date(2024, 1, 5), lines=(OrderLine(sku='MUG-001', quantity=2, unit_price_cents=1250),), status='paid')]"),
    ('orders', 28): ('error', 'ParseError', 'order header must be order_id,customer,placed,status,sku,quantity,unit_price'),
    ('orders', 29): ('error', 'ParseError', 'order header must be order_id,customer,placed,status,sku,quantity,unit_price'),
    ('orders', 30): ('error', 'ParseError', "not a date: 'nope'"),
}


def parse(name: str) -> ast.Module:
    return ast.parse((PACKAGE / name).read_text(encoding="utf-8"))


def test_each_file_format_has_its_own_module() -> None:
    from stockroom import items_csv, orders_csv, stock_csv

    assert items_csv.ITEM_HEADER == ("sku", "name", "price", "tags", "reorder_level")
    assert stock_csv.STOCK_HEADER == ("sku", "on_hand", "reserved")
    assert orders_csv.ORDER_HEADER == (
        "order_id",
        "customer",
        "placed",
        "status",
        "sku",
        "quantity",
        "unit_price",
    )
    expected = {
        items_csv: ("parse_tags", "read_items", "write_items"),
        stock_csv: ("read_stock",),
        orders_csv: ("read_orders", "write_orders"),
    }
    for module, names in expected.items():
        for name in names:
            assert getattr(module, name).__module__ == module.__name__, name


def test_csvio_only_re_exports_the_public_names() -> None:
    from stockroom import items_csv, orders_csv, stock_csv

    assert set(csvio.__all__) == NINE and len(csvio.__all__) == 9
    owner = {
        "ITEM_HEADER": items_csv,
        "parse_tags": items_csv,
        "read_items": items_csv,
        "write_items": items_csv,
        "STOCK_HEADER": stock_csv,
        "read_stock": stock_csv,
        "ORDER_HEADER": orders_csv,
        "read_orders": orders_csv,
        "write_orders": orders_csv,
    }
    for name, module in owner.items():
        assert getattr(csvio, name) is getattr(module, name), name
    tree = parse("csvio.py")
    for node in tree.body:
        is_docstring = isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        is_import = isinstance(node, (ast.Import, ast.ImportFrom))
        is_all = (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and "__all__" in ast.unparse(node).split("=")[0]
        )
        assert is_docstring or is_import or is_all, ast.dump(node)[:80]


def test_the_format_modules_do_not_depend_on_each_other_or_on_csvio() -> None:
    names = {"csvio", "items_csv", "stock_csv", "orders_csv"}
    for own in ("items_csv", "stock_csv", "orders_csv"):
        for node in ast.walk(parse(own + ".py")):
            if isinstance(node, ast.ImportFrom):
                target = (node.module or "").split(".")[-1]
                imported = {a.name for a in node.names}
                assert target not in names, (own, target)
                assert not (node.level and not node.module and imported & names), (own, imported)
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[-1] in names for a in node.names), own


def test_the_whole_number_parser_is_not_copied_into_each_module() -> None:
    hits = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "is not a whole number" in node.value:
                    hits.append((path.name, node.lineno))
    assert len(hits) == 1, hits


def outcome(func, text: str):
    try:
        return ("ok", repr(func(text)))
    except StockroomError as exc:
        return ("error", type(exc).__name__, str(exc))
    except Exception as exc:  # recorded as a crash, the same way the recording did
        return ("crash", type(exc).__name__)


@pytest.mark.parametrize("index", range(len(ITEM_INPUTS)))
def test_read_items_is_unchanged(index: int) -> None:
    assert outcome(csvio.read_items, ITEM_INPUTS[index]) == GOLD[("items", index)]


@pytest.mark.parametrize("index", range(len(STOCK_INPUTS)))
def test_read_stock_is_unchanged(index: int) -> None:
    assert outcome(csvio.read_stock, STOCK_INPUTS[index]) == GOLD[("stock", index)]


@pytest.mark.parametrize("index", range(len(ORDER_INPUTS)))
def test_read_orders_is_unchanged(index: int) -> None:
    assert outcome(csvio.read_orders, ORDER_INPUTS[index]) == GOLD[("orders", index)]


def test_writers_and_round_trips_are_unchanged() -> None:
    items = [
        Item("MUG-001", "Blue Mug", 1250, ("kitchen", "gift"), 5),
        Item("NB-200", "Notebook A5", 625, (), 0),
        Item("TEA-010", "Green Tea 100g", 99999, ("tea",), 12),
    ]
    text = csvio.write_items(items)
    assert text == (
        "sku,name,price,tags,reorder_level\n"
        "MUG-001,Blue Mug,12.50,kitchen;gift,5\n"
        "NB-200,Notebook A5,6.25,,0\n"
        "TEA-010,Green Tea 100g,999.99,tea,12\n"
    )
    assert csvio.read_items(text) == items
    assert csvio.write_items([]) == "sku,name,price,tags,reorder_level\n"
    orders = [
        Order(
            "A-1",
            "Ann, Jr",
            date(2024, 1, 5),
            (OrderLine("MUG-001", 2, 1250), OrderLine("PEN-100", 1, 125000)),
            "paid",
        ),
        Order("B-2", "Bob", date(2024, 2, 9), (OrderLine("TEA-010", 3, 895),), "cancelled"),
    ]
    text = csvio.write_orders(orders)
    assert text == (
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        'A-1,"Ann, Jr",2024-01-05,paid,MUG-001,2,12.50\n'
        'A-1,"Ann, Jr",2024-01-05,paid,PEN-100,1,"1,250.00"\n'
        "B-2,Bob,2024-02-09,cancelled,TEA-010,3,8.95\n"
    )
    assert csvio.read_orders(text) == orders
    assert csvio.write_orders([]) == "order_id,customer,placed,status,sku,quantity,unit_price\n"
    assert csvio.read_stock("sku,on_hand,reserved\nMUG-001,14,2\n") == [("MUG-001", 14, 2)]


def test_callers_of_csvio_keep_working(tmp_path: Path) -> None:
    namespace: dict[str, object] = {}
    exec("from stockroom.csvio import *", namespace)
    assert {k for k in namespace if not k.startswith("__")} == NINE
    digests = {
        (7, 20): {
            "items.csv": "1eb8b9a547ef855651a80c8ccff7d44ec14cf47e0a82acde9407b0eeda104be6",
            "orders.csv": "6d44cad1d322b12d90a388670123d89a22820eca017f2b109856852f95615f7e",
            "stock.csv": "792768bfa9e664aa9c28e412b612f70d38fd864e8f575a88bdd0cdb953f6ad74",
        },
        (3, 9): {
            "items.csv": "4579ad5d1eecf5de9c185e173fdcf4d1a70a4215a8dc0fd5d06643a971ff4238",
            "orders.csv": "fe606b981b7291c8c96fdc8145f945420daf23d12a45ad4c4014642239da30b3",
            "stock.csv": "60459fea289ccb7faff78209e821afc7094ea699e9470bb93125afb4ab7c4014",
        },
    }
    for (seed, count), expected in digests.items():
        out = tmp_path / f"{seed}-{count}"
        done = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "make_sample_data.py"),
                "--out",
                str(out),
                "--seed",
                str(seed),
                "--orders",
                str(count),
            ],
            capture_output=True,
            text=True,
            timeout=50,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        got = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir()}
        assert got == expected
