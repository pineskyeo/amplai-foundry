import ast
from datetime import date
from pathlib import Path

import pytest

import stockroom
from stockroom import report
from stockroom.models import Order, OrderLine
from stockroom.money import format_money
from stockroom.orders import OrderBook
from stockroom.report import sales_summary

PACKAGE = Path(stockroom.__file__).resolve().parent


def modules() -> dict[str, ast.Module]:
    return {
        str(path.relative_to(PACKAGE)): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(PACKAGE.rglob("*.py"))
    }


def make_book() -> OrderBook:
    return OrderBook(
        [
            Order("B-1", "Hana Kimura-Lee", date(2024, 1, 5), (OrderLine("MUG-001", 40, 1250), OrderLine("TEA-010", 3, 895)), "paid"),
            Order("B-2", "Omar Diaz-Fuentes", date(2024, 1, 20), (OrderLine("PEN-100", 100, 3400),), "shipped"),
            Order("B-3", "Hana Kimura-Lee", date(2024, 2, 2), (OrderLine("NB-200", 1000, 625),), "open"),
            Order("B-4", "Lena Berg-Holmgren", date(2024, 2, 9), (OrderLine("LMP-030", 30, 4999),), "cancelled"),
            Order("B-5", "Lena Berg-Holmgren", date(2024, 3, 1), (OrderLine("CND-007", 1234, 980),), "paid"),
            Order("B-6", "Omar Diaz-Fuentes", date(2024, 3, 15), (OrderLine("MUG-002", 7, 1),), "paid"),
        ]
    )


def test_report_has_no_private_copy_of_the_formatter() -> None:
    tree = modules()["report.py"]
    defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_cents" not in defined
    assert not hasattr(report, "_cents")


def test_report_formats_amounts_with_the_money_module() -> None:
    tree = modules()["report.py"]
    used = [
        n
        for n in ast.walk(tree)
        if (isinstance(n, ast.Name) and n.id == "format_money")
        or (isinstance(n, ast.Attribute) and n.attr == "format_money")
    ]
    assert used, "report.py never refers to money.format_money"
    imported = any(
        isinstance(n, ast.ImportFrom)
        and (n.module or "").split(".")[-1] == "money"
        and any(a.name == "format_money" for a in n.names)
        for n in ast.walk(tree)
    ) or any(
        isinstance(n, (ast.Import, ast.ImportFrom))
        and any(a.name.split(".")[-1] == "money" for a in n.names)
        for n in ast.walk(tree)
    )
    assert imported


def test_only_the_money_module_splits_cents_or_groups_thousands() -> None:
    offenders = []
    for name, tree in modules().items():
        if name == "money.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if called in {"divmod", "zfill"}:
                    offenders.append((name, called, node.lineno))
            if isinstance(node, ast.FormattedValue) and node.format_spec is not None:
                spec = "".join(
                    part.value for part in ast.walk(node.format_spec) if isinstance(part, ast.Constant)
                )
                if "," in spec or "_" in spec:
                    offenders.append((name, f"format spec {spec!r}", node.lineno))
    assert offenders == []


@pytest.mark.parametrize(
    ("symbol", "top", "expected"),
    [
        ("$", 3, '5 orders, 1 cancelled\nRevenue: $22,270.12\n\nMonth       Revenue\n-------  ----------\n2024-01   $3,926.85\n2024-02   $6,250.00\n2024-03  $12,093.27\n\nCustomer                 Total\n------------------  ----------\nLena Berg-Holmgren  $12,093.20\nHana Kimura-Lee      $6,776.85\nOmar Diaz-Fuentes    $3,400.07'),
        ("$", 1, '5 orders, 1 cancelled\nRevenue: $22,270.12\n\nMonth       Revenue\n-------  ----------\n2024-01   $3,926.85\n2024-02   $6,250.00\n2024-03  $12,093.27\n\nCustomer                 Total\n------------------  ----------\nLena Berg-Holmgren  $12,093.20'),
        ("EUR ", 3, '5 orders, 1 cancelled\nRevenue: EUR 22,270.12\n\nMonth          Revenue\n-------  -------------\n2024-01   EUR 3,926.85\n2024-02   EUR 6,250.00\n2024-03  EUR 12,093.27\n\nCustomer                    Total\n------------------  -------------\nLena Berg-Holmgren  EUR 12,093.20\nHana Kimura-Lee      EUR 6,776.85\nOmar Diaz-Fuentes    EUR 3,400.07'),
        ("EUR ", 2, '5 orders, 1 cancelled\nRevenue: EUR 22,270.12\n\nMonth          Revenue\n-------  -------------\n2024-01   EUR 3,926.85\n2024-02   EUR 6,250.00\n2024-03  EUR 12,093.27\n\nCustomer                    Total\n------------------  -------------\nLena Berg-Holmgren  EUR 12,093.20\nHana Kimura-Lee      EUR 6,776.85'),
        ("", 3, '5 orders, 1 cancelled\nRevenue: 22,270.12\n\nMonth      Revenue\n-------  ---------\n2024-01   3,926.85\n2024-02   6,250.00\n2024-03  12,093.27\n\nCustomer                Total\n------------------  ---------\nLena Berg-Holmgren  12,093.20\nHana Kimura-Lee      6,776.85\nOmar Diaz-Fuentes    3,400.07'),
        ("", 1, '5 orders, 1 cancelled\nRevenue: 22,270.12\n\nMonth      Revenue\n-------  ---------\n2024-01   3,926.85\n2024-02   6,250.00\n2024-03  12,093.27\n\nCustomer                Total\n------------------  ---------\nLena Berg-Holmgren  12,093.20'),
    ],
)
def test_sales_summary_output_is_unchanged(symbol: str, top: int, expected: str) -> None:
    assert sales_summary(make_book(), symbol=symbol, top=top) == expected


def test_sales_summary_of_extreme_books_is_unchanged() -> None:
    big = OrderBook(
        [Order("C-1", "Mega Customer Inc", date(2024, 5, 1), (OrderLine("MUG-001", 999999, 99999),), "paid")]
    )
    assert sales_summary(big) == (
        "1 order, 0 cancelled\nRevenue: $999,989,000.01\n\nMonth            Revenue\n"
        "-------  ---------------\n2024-05  $999,989,000.01\n\nCustomer                     Total\n"
        "-----------------  ---------------\nMega Customer Inc  $999,989,000.01"
    )
    assert sales_summary(OrderBook([])) == (
        "0 orders, 0 cancelled\nRevenue: $0.00\n\nMonth  Revenue\n-----  -------\n\nCustomer  Total\n--------  -----"
    )


def test_format_money_itself_is_unchanged() -> None:
    assert format_money(0) == "$0.00"
    assert format_money(5, "") == "0.05"
    assert format_money(-99, "EUR ") == "-EUR 0.99"
    assert format_money(123456789, "$") == "$1,234,567.89"
