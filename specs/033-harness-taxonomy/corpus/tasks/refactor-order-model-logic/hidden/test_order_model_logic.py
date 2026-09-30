import ast
import dataclasses
from datetime import date
from pathlib import Path

import pytest

import stockroom
from stockroom import pricing
from stockroom.models import STATUSES, Order, OrderLine
from stockroom.orders import OrderBook
from stockroom.report import customer_totals, sales_summary

PACKAGE = Path(stockroom.__file__).resolve().parent


def modules() -> dict[str, ast.Module]:
    return {
        str(path.relative_to(PACKAGE)): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(PACKAGE.rglob("*.py"))
    }


def line(quantity: int = 2, price: int = 500) -> OrderLine:
    return OrderLine("MUG-001", quantity, price)


def order(status: str = "paid", lines: tuple[OrderLine, ...] = (), when: date = date(2024, 1, 5)) -> Order:
    return Order("O-1", "Ann Example", when, lines, status)


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


def test_models_carry_the_order_arithmetic_and_the_status_rule() -> None:
    assert line(3, 1250).total_cents == 3750
    assert order(lines=()).subtotal_cents == 0
    assert order(lines=(line(3, 1250), line(1, 7))).subtotal_cents == 3757
    assert {status: order(status).is_active for status in STATUSES} == {
        "open": True,
        "paid": True,
        "shipped": True,
        "cancelled": False,
    }


def test_the_new_members_are_properties_not_fields() -> None:
    assert [f.name for f in dataclasses.fields(OrderLine)] == ["sku", "quantity", "unit_price_cents"]
    assert [f.name for f in dataclasses.fields(Order)] == ["order_id", "customer", "placed", "lines", "status"]
    for owner, name in ((OrderLine, "total_cents"), (Order, "subtotal_cents"), (Order, "is_active")):
        assert isinstance(getattr(owner, name), property), name
    assert order(lines=(line(),)) == order(lines=(line(),))
    assert "total_cents" not in repr(line()) and "is_active" not in repr(order())


def test_pricing_reads_the_model() -> None:
    class FixedLine(OrderLine):
        total_cents = property(lambda self: 7)

    class FixedOrder(Order):
        subtotal_cents = property(lambda self: 12345)

    assert pricing.line_total(FixedLine("MUG-001", 9, 9)) == 7
    assert pricing.order_subtotal(FixedOrder("F-1", "Ann", date(2024, 1, 1), (), "open")) == 12345
    assert pricing.order_total(FixedOrder("F-1", "Ann", date(2024, 1, 1), (), "open")) == 12345
    assert (
        pricing.order_total(
            FixedOrder("F-1", "Ann", date(2024, 1, 1), (), "open"), discount_percent=20, tax_bp=1000
        )
        == 10864
    )
    assert pricing.order_subtotal(order(lines=(FixedLine("MUG-001", 9, 9), FixedLine("MUG-001", 1, 1)))) == 14
    assert pricing.line_total(line(3, 250)) == 750
    assert pricing.order_subtotal(order(lines=(line(3, 250), line(1, 5)))) == 755


def test_book_and_report_ask_the_order_whether_it_is_active() -> None:
    class Hidden(Order):
        is_active = property(lambda self: False)

    class Revived(Order):
        is_active = property(lambda self: True)

    when = date(2024, 4, 2)
    gone = Hidden("H-1", "Ann Example", when, (OrderLine("MUG-001", 2, 500),), "paid")
    back = Revived("R-1", "Bob Example", when, (OrderLine("MUG-001", 3, 100),), "cancelled")
    book = OrderBook([gone, back])
    assert book.revenue_by_month() == {"2024-04": 300}
    assert customer_totals(book) == {"Bob Example": 300}
    assert sales_summary(book).splitlines()[:2] == ["1 order, 1 cancelled", "Revenue: $3.00"]


def test_book_and_report_ask_the_order_for_its_subtotal() -> None:
    class Quoted(Order):
        subtotal_cents = property(lambda self: 99_00)

    book = OrderBook(
        [
            Quoted("Q-1", "Ann Example", date(2024, 4, 2), (), "open"),
            Quoted("Q-2", "Ann Example", date(2024, 5, 2), (OrderLine("MUG-001", 1, 1),), "paid"),
        ]
    )
    assert book.revenue_by_month() == {"2024-04": 9900, "2024-05": 9900}
    assert customer_totals(book) == {"Ann Example": 19800}
    assert sales_summary(book).splitlines()[:2] == ["2 orders, 0 cancelled", "Revenue: $198.00"]


def test_only_the_models_module_knows_the_cancelled_status() -> None:
    offenders = []
    for name, tree in modules().items():
        if name == "models.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "cancelled":
                offenders.append((name, node.lineno))
    assert offenders == []


def test_orders_and_report_no_longer_import_pricing() -> None:
    mods = modules()
    for name in ("orders.py", "report.py"):
        for node in ast.walk(mods[name]):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[-1] != "pricing", name
                assert not (node.level and node.module is None and any(a.name == "pricing" for a in node.names)), name
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[-1] == "pricing" for a in node.names), name


def test_reports_are_unchanged() -> None:
    book = make_book()
    assert book.revenue_by_month() == {"2024-01": 392685, "2024-02": 625000, "2024-03": 1209327}
    assert customer_totals(book) == {
        "Hana Kimura-Lee": 677685,
        "Omar Diaz-Fuentes": 340007,
        "Lena Berg-Holmgren": 1209320,
    }
    assert sales_summary(book) == (
        "5 orders, 1 cancelled\nRevenue: $22,270.12\n\nMonth       Revenue\n-------  ----------\n"
        "2024-01   $3,926.85\n2024-02   $6,250.00\n2024-03  $12,093.27\n\n"
        "Customer                 Total\n------------------  ----------\n"
        "Lena Berg-Holmgren  $12,093.20\nHana Kimura-Lee      $6,776.85\nOmar Diaz-Fuentes    $3,400.07"
    )
    assert pricing.order_total(book.get("B-1"), discount_percent=20, tax_bp=1000) == 46363
    assert [o.quantity for o in book] == [43, 100, 1000, 30, 1234, 7]
