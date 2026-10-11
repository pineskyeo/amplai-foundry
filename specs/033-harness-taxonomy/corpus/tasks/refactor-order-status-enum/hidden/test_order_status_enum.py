from __future__ import annotations

import ast
import csv
import dataclasses
import enum
import hashlib
import io
import json
import os
import random
import subprocess
import sys
import typing
from datetime import date
from pathlib import Path

import pytest
import stockroom
from stockroom import cli, csvio, jsonio, models
from stockroom.errors import ParseError
from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook
from stockroom.report import customer_totals, sales_summary

PACKAGE = Path(stockroom.__file__).resolve().parent
ROOT = PACKAGE.parent
TEXTS = ("open", "paid", "shipped", "cancelled")


def status_enum():
    return models.OrderStatus


def test_order_status_is_a_string_enum() -> None:
    OrderStatus = status_enum()
    assert issubclass(OrderStatus, enum.StrEnum)
    assert [(m.name, m.value) for m in OrderStatus] == [
        ("OPEN", "open"),
        ("PAID", "paid"),
        ("SHIPPED", "shipped"),
        ("CANCELLED", "cancelled"),
    ]
    for member, text in zip(OrderStatus, TEXTS, strict=True):
        assert member == text and str(member) == text and f"{member}" == text
        assert OrderStatus(text) is member
    assert type(models.STATUSES) is tuple
    assert models.STATUSES == TEXTS
    assert all(a is b for a, b in zip(models.STATUSES, OrderStatus, strict=True))


def test_an_order_always_holds_a_member() -> None:
    OrderStatus = status_enum()
    when = date(2024, 1, 5)
    line = OrderLine("MUG-001", 2, 1250)
    assert Order("A-1", "Ann", when).status is OrderStatus.OPEN
    for member, text in zip(OrderStatus, TEXTS, strict=True):
        by_text = Order("A-1", "Ann", when, (line,), text)
        by_member = Order("A-1", "Ann", when, (line,), status=member)
        assert by_text.status is member and by_member.status is member
        assert type(by_text.status) is OrderStatus
        assert by_text == by_member and hash(by_text) == hash(by_member)
        assert f"status=<OrderStatus.{member.name}: '{text}'>" in repr(by_text)
        assert dataclasses.replace(by_member, status=text).status is member
    moved = dataclasses.replace(Order("A-1", "Ann", when), status="shipped")
    assert moved.status is OrderStatus.SHIPPED
    assert Order("A-1", "Ann", when, (), "paid") != Order("A-1", "Ann", when, (), "open")
    with pytest.raises(dataclasses.FrozenInstanceError):
        moved.status = OrderStatus.OPEN  # type: ignore[misc]
    assert [f.name for f in dataclasses.fields(Order)] == [
        "order_id",
        "customer",
        "placed",
        "lines",
        "status",
    ]
    assert dataclasses.fields(Order)[-1].default is OrderStatus.OPEN
    assert typing.get_type_hints(Order)["status"] is OrderStatus


@pytest.mark.parametrize("bad", ["lost", "PAID", " paid", "", "Open", "cancelled ", 1, None])
def test_unknown_statuses_are_refused_as_before(bad) -> None:
    with pytest.raises(ParseError) as raised:
        Order("A-9", "Ann", date(2024, 1, 5), (), bad)
    assert str(raised.value) == f"A-9: unknown status {bad!r}"
    with pytest.raises(ParseError) as raised:
        Order("  ", "Ann", date(2024, 1, 5), (), bad)
    assert str(raised.value) == "empty order id"


# -- text outputs ---------------------------------------------------------------------------------


def _o_write_orders(orders) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(("order_id", "customer", "placed", "status", "sku", "quantity", "unit_price"))
    for order, status in orders:
        for line in order.lines:
            units, rest = divmod(line.unit_price_cents, 100)
            writer.writerow(
                [
                    order.order_id,
                    order.customer,
                    order.placed.isoformat(),
                    status,
                    line.sku,
                    line.quantity,
                    f"{units:,}.{rest:02d}",
                ]
            )
    return out.getvalue()


def _o_dump_orders(orders) -> str:
    objects = [
        {
            "order_id": order.order_id,
            "customer": order.customer,
            "placed": order.placed.isoformat(),
            "status": status,
            "lines": [
                {
                    "sku": line.sku,
                    "quantity": line.quantity,
                    "unit_price_cents": line.unit_price_cents,
                }
                for line in order.lines
            ],
        }
        for order, status in orders
    ]
    return json.dumps(objects, indent=2, sort_keys=True) + "\n"


def make_orders(rng: random.Random):
    """(order, status text) pairs; the status is given as text or as a member at random."""
    OrderStatus = status_enum()
    pairs = []
    for n in range(rng.randrange(0, 9)):
        text = rng.choice(TEXTS)
        given = OrderStatus(text) if rng.random() < 0.5 else text
        lines = tuple(
            OrderLine(
                rng.choice(["MUG-001", "TEA-010", "PEN-100"]),
                rng.randrange(1, 9),
                rng.randrange(0, 99999),
            )
            for _ in range(rng.randrange(1, 4))
        )
        customer = rng.choice(["Hana Kim", "Omar Díaz", 'Lena "LB" Berg'])
        placed = date(2024, rng.randrange(1, 13), rng.randrange(1, 29))
        pairs.append((Order(f"R-{n:03d}", customer, placed, lines, given), text))
    return pairs


def test_files_and_json_keep_the_plain_status_text() -> None:
    OrderStatus = status_enum()
    rng = random.Random(2468)
    for _ in range(300):
        pairs = make_orders(rng)
        orders = [order for order, _ in pairs]
        text = csvio.write_orders(orders)
        assert text == _o_write_orders(pairs)
        assert jsonio.dump_orders(orders) == _o_dump_orders(pairs)
        back = csvio.read_orders(text)
        assert back == orders
        assert all(type(o.status) is OrderStatus for o in back)
        for order, status in pairs:
            converted = jsonio.order_to_dict(order)
            assert type(converted["status"]) is str and converted["status"] == status
    loaded = jsonio.load_orders(
        '[{"order_id": "J-1", "customer": "Ann", "placed": "2024-01-05", "lines": []}]'
    )
    assert loaded[0].status is OrderStatus.OPEN


SAMPLE_DIGESTS: dict[tuple[int, int], dict[str, str]] = {
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
    (11, 5): {
        "items.csv": "9c962e8767fa2f3ab1e566adf1a65f8e154c6a6423aeeda3284e9f49b4ce976f",
        "orders.csv": "d47032d872ae30c9296c19fd31d32b97b5b0ef7a27526ade1bf88158dc49f4ab",
        "stock.csv": "9928dfbad9a8faaeae8be3d2f2c5b403e5b45356b4d3ed6b5fd0a829becbbd58",
    },
    (2, 40): {
        "items.csv": "d79f508cec482c0349caa713c0ba0ea2087854800582a8b5db8ae560438e2586",
        "orders.csv": "d10f8ab3592e32ce6cf963209f600216e15ef49217ee35f9bebd001412928ab7",
        "stock.csv": "b4606ea7f71e1cea5f8393ad3cc948f920c61af5a23cf301b594c4facc1d06cd",
    },
}


def test_sample_data_files_are_byte_identical(tmp_path: Path) -> None:
    for (seed, count), expected in SAMPLE_DIGESTS.items():
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
            timeout=60,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        got = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir())}
        assert got == expected


BOOK_ORDERS = [
    ("C-1", "Hana Kimura-Lee", date(2024, 1, 8), (("MUG-001", 20, 1250),), "shipped"),
    ("C-2", "Omar Diaz-Fuentes", date(2024, 1, 19), (("PEN-100", 5, 3400),), "paid"),
    ("C-3", "Lena Berg-Holmgren", date(2024, 2, 3), (("LMP-030", 9, 4999),), "cancelled"),
    ("C-4", "Hana Kimura-Lee", date(2024, 2, 14), (("CND-007", 30, 980),), "open"),
    ("C-5", "Ravi Nair-Iyer", date(2024, 3, 2), (("TEA-011", 20, 740),), "cancelled"),
    ("C-6", "Ravi Nair-Iyer", date(2024, 3, 9), (("TEA-010", 40, 895),), "paid"),
]

# recorded before the change for BOOK_ORDERS (statuses given as text)
REPORT_GOLD: dict[str, object] = {
    "revenue_by_month": {
        "2024-01": 42000,
        "2024-02": 29400,
        "2024-03": 35800,
    },
    "customer_totals": {
        "Hana Kimura-Lee": 54400,
        "Omar Diaz-Fuentes": 17000,
        "Ravi Nair-Iyer": 35800,
    },
    "sales_summary": "4 orders, 2 cancelled\n"
    "Revenue: $1,072.00\n"
    "\n"
    "Month    Revenue\n"
    "-------  -------\n"
    "2024-01  $420.00\n"
    "2024-02  $294.00\n"
    "2024-03  $358.00\n"
    "\n"
    "Customer             Total\n"
    "-----------------  -------\n"
    "Hana Kimura-Lee    $544.00\n"
    "Ravi Nair-Iyer     $358.00\n"
    "Omar Diaz-Fuentes  $170.00",
    "sales_summary_eur_top2": "4 orders, 2 cancelled\n"
    "Revenue: EUR 1,072.00\n"
    "\n"
    "Month       Revenue\n"
    "-------  ----------\n"
    "2024-01  EUR 420.00\n"
    "2024-02  EUR 294.00\n"
    "2024-03  EUR 358.00\n"
    "\n"
    "Customer              Total\n"
    "---------------  ----------\n"
    "Hana Kimura-Lee  EUR 544.00\n"
    "Ravi Nair-Iyer   EUR 358.00",
}


def test_reports_count_statuses_as_before() -> None:
    OrderStatus = status_enum()
    for as_member in (False, True):
        orders = [
            Order(
                i,
                c,
                d,
                tuple(OrderLine(*line) for line in lines),
                OrderStatus(s) if as_member else s,
            )
            for i, c, d, lines, s in BOOK_ORDERS
        ]
        book = OrderBook(orders)
        assert book.revenue_by_month() == REPORT_GOLD["revenue_by_month"]
        assert customer_totals(book) == REPORT_GOLD["customer_totals"]
        assert sales_summary(book) == REPORT_GOLD["sales_summary"]
        assert sales_summary(book, symbol="EUR ", top=2) == REPORT_GOLD["sales_summary_eur_top2"]


def test_only_the_models_module_spells_the_statuses() -> None:
    offenders = []
    files = sorted(PACKAGE.rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py"))
    for path in files:
        if path.name == "models.py" and path.parent == PACKAGE:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and node.value in TEXTS:
                offenders.append((str(path.relative_to(ROOT)), node.lineno, node.value))
    assert offenders == []


ORDERS_CSV = """order_id,customer,placed,status,sku,quantity,unit_price
Z-1,Hana Kimura-Lee,2024-01-08,shipped,MUG-001,20,12.50
Z-1,Hana Kimura-Lee,2024-01-08,shipped,TEA-010,10,8.95
Z-2,Omar Diaz-Fuentes,2024-01-19,paid,PEN-100,5,34.00
Z-3,Lena Berg-Holmgren,2024-02-03,cancelled,LMP-030,1,49.99
Z-4,Hana Kimura-Lee,2024-02-14,paid,CND-007,30,9.80
Z-5,Lena Berg-Holmgren,2024-03-02,open,TEA-011,20,7.40
"""
BAD_CSV = ORDERS_CSV + "Z-6,Ann Example,2024-03-05,Lost,MUG-001,1,1.00\n"

# (exit code, stdout, stderr) recorded from the command before the change.
CLI_GOLD: dict[str, tuple[int, str, str]] = {
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
    "sales-bad-status": (
        1,
        "",
        "stockroom: error: Z-6: unknown status 'Lost'\n",
    ),
}


def test_sales_command_output_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in [k for k in os.environ if k.startswith("STOCKROOM_")]:
        monkeypatch.delenv(key)
    (tmp_path / "orders.csv").write_text(ORDERS_CSV, encoding="utf-8")
    (tmp_path / "bad.csv").write_text(BAD_CSV, encoding="utf-8")
    cases = {
        "sales": ["sales", "--orders", str(tmp_path / "orders.csv")],
        "sales-from": ["sales", "--orders", str(tmp_path / "orders.csv"), "--from", "2024-02-01"],
        "sales-bad-status": ["sales", "--orders", str(tmp_path / "bad.csv")],
    }
    for name, argv in cases.items():
        code = cli.main(argv)
        captured = capsys.readouterr()
        assert (code, captured.out, captured.err) == CLI_GOLD[name], name
