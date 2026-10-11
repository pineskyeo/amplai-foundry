from __future__ import annotations

import dataclasses
import inspect
import os
import random
import subprocess
import sys
import warnings
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path

import pytest
import stockroom
from stockroom import cli, report
from stockroom.csvio import read_items, read_orders
from stockroom.models import Order, OrderLine
from stockroom.money import format_money
from stockroom.orders import OrderBook

ROOT = Path(stockroom.__file__).resolve().parent.parent
OLD_SYMBOL = "the symbol argument of sales_summary() is deprecated; pass currency=Currency(symbol)"
BOTH = "sales_summary() takes currency or symbol, not both"


def currency_class():
    from stockroom.money import Currency

    return Currency


# -- the summary as it was computed and laid out before the change (the oracle) -----------------


def _o_table(headers, rows, align_right=()):
    right = set(align_right)
    cells = [[str(value) for value in row] for row in rows]
    if cells:
        widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    else:
        widths = [len(h) for h in headers]

    def line(values):
        parts = [
            value.rjust(widths[i]) if i in right else value.ljust(widths[i])
            for i, value in enumerate(values)
        ]
        return "  ".join(parts).rstrip()

    out = [line(list(headers)), "  ".join("-" * w for w in widths)]
    out += [line(row) for row in cells]
    return "\n".join(out)


def _o_subtotal(order: Order) -> int:
    return sum(line.quantity * line.unit_price_cents for line in order.lines)


def oracle_figures(orders: list[Order], top: int) -> tuple:
    active = [o for o in orders if o.status != "cancelled"]
    months: dict[str, int] = {}
    totals: dict[str, int] = {}
    for order in active:
        key = f"{order.placed.year:04d}-{order.placed.month:02d}"
        months[key] = months.get(key, 0) + _o_subtotal(order)
        totals[order.customer] = totals.get(order.customer, 0) + _o_subtotal(order)
    ranked = sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))[:top]
    return (
        len(active),
        len(orders) - len(active),
        sum(_o_subtotal(o) for o in active),
        tuple(sorted(months.items())),
        tuple(ranked),
    )


def oracle_render(figures: tuple, money: Callable[[int], str]) -> str:
    count, cancelled, revenue, months, customers = figures
    lines = [
        f"{count} {'order' if count == 1 else 'orders'}, {cancelled} cancelled",
        f"Revenue: {money(revenue)}",
        "",
        _o_table(["Month", "Revenue"], [[m, money(c)] for m, c in months], align_right=[1]),
        "",
        _o_table(["Customer", "Total"], [[n, money(c)] for n, c in customers], align_right=[1]),
    ]
    return "\n".join(lines)


def figures_of(summary) -> tuple:
    assert type(summary) is report.SalesSummary
    assert type(summary.months) is tuple and type(summary.top_customers) is tuple
    assert all(type(pair) is tuple for pair in summary.months + summary.top_customers)
    return (
        summary.order_count,
        summary.cancelled_count,
        summary.revenue_cents,
        summary.months,
        summary.top_customers,
    )


# -- generated order books -----------------------------------------------------------------------

CUSTOMERS = [
    "Hana Kimura-Lee",
    "Omar Diaz-Fuentes",
    "Lena Berg-Holmgren",
    "Ravi Nair-Iyer",
    "Ada Moss-Lovelace",
    "Bo Customer",
]
SKUS = ["MUG-001", "TEA-010", "PEN-100", "NB-200", "LMP-030"]
STATUSES = ["open", "paid", "shipped", "cancelled"]


def make_orders(rng: random.Random) -> list[Order]:
    orders = []
    for i in range(rng.randrange(0, 12)):
        lines = tuple(
            OrderLine(rng.choice(SKUS), rng.randrange(1, 40), rng.randrange(0, 4000))
            for _ in range(rng.randrange(0, 4))
        )
        placed = date(rng.choice([2023, 2024]), rng.randrange(1, 13), rng.randrange(1, 29))
        orders.append(
            Order(f"G-{i:03d}", rng.choice(CUSTOMERS), placed, lines, rng.choice(STATUSES))
        )
    for k, order in enumerate(orders):
        if order.status != "cancelled":
            # one large order keeps every amount column at least as wide as its header
            big = OrderLine("BIG-001", 100, 1000 + rng.randrange(0, 90000))
            orders[k] = dataclasses.replace(order, lines=(*order.lines, big))
            break
    return orders


def test_generated_books_summarize_and_render_as_before() -> None:
    Currency = currency_class()
    rng = random.Random(4417)
    for _ in range(400):
        orders = make_orders(rng)
        top = rng.choice([1, 2, 3, 5])
        symbol = rng.choice(["$", "", "EUR "])
        expected_figures = oracle_figures(orders, top)
        expected_text = oracle_render(expected_figures, lambda c, s=symbol: format_money(c, s))
        summary = report.summarize(iter(orders), top=top)
        assert figures_of(summary) == expected_figures
        assert report.render_summary(summary, Currency(symbol)) == expected_text
        book = OrderBook(orders)
        assert report.sales_summary(book, currency=Currency(symbol), top=top) == expected_text
        if symbol == "$":
            assert report.sales_summary(book, top=top) == expected_text
            assert report.render_summary(summary) == expected_text


def test_sales_summary_record_is_frozen_and_ordered() -> None:
    fields = dataclasses.fields(report.SalesSummary)
    assert [f.name for f in fields] == [
        "order_count",
        "cancelled_count",
        "revenue_cents",
        "months",
        "top_customers",
    ]
    summary = report.SalesSummary(2, 1, 500, (("2024-01", 500),), (("Ann Example", 500),))
    assert summary == report.SalesSummary(
        order_count=2,
        cancelled_count=1,
        revenue_cents=500,
        months=(("2024-01", 500),),
        top_customers=(("Ann Example", 500),),
    )
    assert hash(summary) == hash(dataclasses.replace(summary))
    with pytest.raises(dataclasses.FrozenInstanceError):
        summary.order_count = 3  # type: ignore[misc]


class OnlyOnce:
    """An iterable that may be iterated once."""

    def __init__(self, orders: list[Order]) -> None:
        self.orders = orders
        self.rounds = 0

    def __iter__(self) -> Iterator[Order]:
        self.rounds += 1
        assert self.rounds == 1, "the orders were read twice"
        return iter(self.orders)


def test_summarize_reads_any_iterable_of_orders_once() -> None:
    orders = [
        Order("A-1", "Ann Example", date(2024, 3, 5), (OrderLine("MUG-001", 2, 1250),), "paid"),
        Order("A-2", "Bob Example", date(2024, 1, 9), (), "open"),
        Order("A-3", "Ann Example", date(2024, 1, 2), (OrderLine("TEA-010", 1, 895),), "cancelled"),
        Order("A-4", "Cy Example", date(2024, 3, 1), (OrderLine("PEN-100", 1, 2500),), "shipped"),
        Order("A-5", "Dee Example", date(2023, 12, 31), (OrderLine("NB-200", 4, 625),), "paid"),
    ]
    expected = (
        4,
        1,
        7500,
        (("2023-12", 2500), ("2024-01", 0), ("2024-03", 5000)),
        (("Ann Example", 2500), ("Cy Example", 2500), ("Dee Example", 2500)),
    )
    once = OnlyOnce(orders)
    assert figures_of(report.summarize(once)) == expected
    assert once.rounds == 1
    assert figures_of(report.summarize(o for o in orders)) == expected
    assert figures_of(report.summarize(OrderBook(orders), top=4))[4] == (
        *expected[4],
        ("Bob Example", 0),
    )
    assert figures_of(report.summarize(orders, 1))[4] == (("Ann Example", 2500),)
    assert figures_of(report.summarize([])) == (0, 0, 0, (), ())


def test_render_summary_lays_out_any_record() -> None:
    summary = report.SalesSummary(
        order_count=1,
        cancelled_count=7,
        revenue_cents=123456789,
        months=(("1999-12", 150000), ("2030-01", -5)),
        top_customers=(("Zed Zebra-Longname", 99999999), ("Amy Short-Name", 0)),
    )
    for symbol in ("$", "", "EUR "):
        expected = oracle_render(figures_of(summary), lambda c, s=symbol: format_money(c, s))
        assert report.render_summary(summary, currency_class()(symbol)) == expected
    assert report.render_summary(report.SalesSummary(0, 0, 0, (), ())) == (
        "0 orders, 0 cancelled\nRevenue: $0.00\n\nMonth  Revenue\n-----  -------\n\n"
        "Customer  Total\n--------  -----"
    )


def test_every_amount_is_written_by_the_currency() -> None:
    Currency = currency_class()

    class Bracketed(Currency):
        def format(self, cents: int) -> str:
            return f"[{cents:>9}]"

    summary = report.SalesSummary(
        3, 2, 4321, (("2024-02", 4000), ("2024-03", 321)), (("Hana Kimura-Lee", 4321),)
    )
    expected = oracle_render(figures_of(summary), lambda c: f"[{c:>9}]")
    assert report.render_summary(summary, Bracketed()) == expected
    orders = [
        Order("B-1", "Hana Kimura-Lee", date(2024, 2, 1), (OrderLine("MUG-001", 4, 1000),), "paid"),
        Order("B-2", "Hana Kimura-Lee", date(2024, 3, 1), (OrderLine("TEA-010", 1, 321),), "open"),
        Order(
            "B-3",
            "Omar Diaz-Fuentes",
            date(2024, 3, 2),
            (OrderLine("PEN-100", 1, 99),),
            "cancelled",
        ),
    ]
    assert report.sales_summary(OrderBook(orders), currency=Bracketed()) == oracle_render(
        oracle_figures(orders, 3), lambda c: f"[{c:>9}]"
    )


def test_currency_record_formats_like_format_money() -> None:
    Currency = currency_class()
    assert [f.name for f in dataclasses.fields(Currency)] == ["symbol"]
    assert Currency() == Currency("$") == Currency(symbol="$")
    assert hash(Currency("EUR ")) == hash(Currency("EUR "))
    assert Currency("EUR ") != Currency("$")
    with pytest.raises(dataclasses.FrozenInstanceError):
        Currency().symbol = "x"  # type: ignore[misc]
    rng = random.Random(77)
    values = [0, 5, -5, 99, -99, 100, 123456, -123456, 10**12 + 7]
    values += [rng.randrange(-(10**9), 10**9) for _ in range(300)]
    for symbol in ("$", "", "EUR ", "£"):
        for cents in values:
            assert Currency(symbol).format(cents) == format_money(cents, symbol)


def test_symbol_argument_still_works_with_a_deprecation_warning() -> None:
    Currency = currency_class()
    orders = [
        Order(
            "A-1", "Hana Kimura-Lee", date(2024, 1, 5), (OrderLine("MUG-001", 20, 1250),), "paid"
        ),
        Order(
            "A-2", "Omar Diaz-Fuentes", date(2024, 2, 7), (OrderLine("PEN-100", 5, 3400),), "open"
        ),
    ]
    book = OrderBook(orders)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        euro = report.sales_summary(book, symbol="EUR ")
        dollar = report.sales_summary(book, symbol="$", top=1)
        plain = report.sales_summary(book, top=1)
        chosen = report.sales_summary(book, currency=Currency("EUR "))
    assert [(w.category, str(w.message), w.filename) for w in caught] == [
        (DeprecationWarning, OLD_SYMBOL, __file__),
        (DeprecationWarning, OLD_SYMBOL, __file__),
    ]
    assert (
        euro
        == chosen
        == oracle_render(oracle_figures(orders, 3), lambda c: format_money(c, "EUR "))
    )
    assert dollar == plain == oracle_render(oracle_figures(orders, 1), format_money)


def test_currency_and_symbol_together_are_refused() -> None:
    Currency = currency_class()
    book = OrderBook([])
    for currency in (Currency("$"), Currency("EUR ")):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(TypeError) as raised:
                report.sales_summary(book, currency=currency, symbol="$")
        assert str(raised.value) == BOTH
        assert caught == []
    with pytest.raises(TypeError):
        report.sales_summary(book, Currency("$"))  # type: ignore[misc]
    parameters = inspect.signature(report.sales_summary).parameters
    assert next(iter(parameters)) == "book"
    for name, default in (("currency", None), ("top", 3)):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters[name].default == default


# -- the command line ----------------------------------------------------------------------------

ITEMS_CSV = """sku,name,price,tags,reorder_level
MUG-001,Blue Mug,12.50,kitchen;gift,5
MUG-002,Red Mug,12.50,kitchen,5
TEA-010,Green Tea 100g,8.95,food;tea,10
PEN-100,Fountain Pen,34.00,office;gift,2
LMP-030,Desk Lamp,1249.99,office;home,1
"""
ORDERS_CSV = """order_id,customer,placed,status,sku,quantity,unit_price
Z-1,Hana Kimura-Lee,2024-01-08,shipped,MUG-001,20,12.50
Z-1,Hana Kimura-Lee,2024-01-08,shipped,TEA-010,10,8.95
Z-2,Omar Diaz-Fuentes,2024-01-19,paid,PEN-100,5,34.00
Z-3,Lena Berg-Holmgren,2024-02-03,cancelled,LMP-030,1,49.99
Z-4,Hana Kimura-Lee,2024-02-14,paid,CND-007,30,9.80
Z-5,Lena Berg-Holmgren,2024-03-02,open,TEA-011,20,7.40
"""
STOCK_CSV = "sku,on_hand,reserved\n"

PRICE = ["price", "--items", "{I}", "--sku", "PEN-100", "--quantity", "5", "--discount", "10"]
CLI_CASES = {
    "items": (["items", "--file", "{I}"], {}),
    "items-euro": (
        ["items", "--file", "{I}", "--tag", "office"],
        {"STOCKROOM_CURRENCY_SYMBOL": "EUR "},
    ),
    "sales": (["sales", "--orders", "{O}"], {}),
    "sales-pound": (
        ["sales", "--orders", "{O}", "--from", "2024-02-01"],
        {"STOCKROOM_CURRENCY_SYMBOL": "\u00a3"},
    ),
    "price": (PRICE, {}),
    "price-tax-euro": (PRICE, {"STOCKROOM_TAX_BP": "825", "STOCKROOM_CURRENCY_SYMBOL": "EUR "}),
    "price-unknown": (["price", "--items", "{I}", "--sku", "XYZ-999", "--quantity", "1"], {}),
}

# (exit code, stdout, stderr) recorded from the command before the change.
CLI_GOLD: dict[str, tuple[int, str, str]] = {
    "items": (
        0,
        "SKU      Name            Price\n"
        "-------  --------------  ---------\n"
        "MUG-001  Blue Mug           $12.50\n"
        "MUG-002  Red Mug            $12.50\n"
        "TEA-010  Green Tea 100g      $8.95\n"
        "PEN-100  Fountain Pen       $34.00\n"
        "LMP-030  Desk Lamp       $1,249.99\n",
        "",
    ),
    "items-euro": (
        0,
        "SKU      Name          Price\n"
        "-------  ------------  ------------\n"
        "PEN-100  Fountain Pen     EUR 34.00\n"
        "LMP-030  Desk Lamp     EUR 1,249.99\n",
        "",
    ),
    "price": (
        0,
        "5 x PEN-100 Fountain Pen: $153.00\ntax: $0.00\ntotal: $153.00\n",
        "",
    ),
    "price-tax-euro": (
        0,
        "5 x PEN-100 Fountain Pen: EUR 153.00\ntax: EUR 12.62\ntotal: EUR 165.62\n",
        "",
    ),
    "price-unknown": (
        1,
        "",
        "stockroom: error: no item XYZ-999\n",
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
    "sales-pound": (
        0,
        "2 orders, 1 cancelled\n"
        "Revenue: £442.00\n"
        "\n"
        "Month    Revenue\n"
        "-------  -------\n"
        "2024-02  £294.00\n"
        "2024-03  £148.00\n"
        "\n"
        "Customer              Total\n"
        "------------------  -------\n"
        "Hana Kimura-Lee     £294.00\n"
        "Lena Berg-Holmgren  £148.00\n",
        "",
    ),
}


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    for key in [k for k in os.environ if k.startswith("STOCKROOM_")]:
        monkeypatch.delenv(key)
    (tmp_path / "items.csv").write_text(ITEMS_CSV, encoding="utf-8")
    (tmp_path / "orders.csv").write_text(ORDERS_CSV, encoding="utf-8")
    files = {"{I}": str(tmp_path / "items.csv"), "{O}": str(tmp_path / "orders.csv")}

    def call(name: str) -> tuple[int, str, str]:
        argv, env = CLI_CASES[name]
        with monkeypatch.context() as patch:
            for key, value in env.items():
                patch.setenv(key, value)
            code = cli.main([files.get(a, a) for a in argv])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return call


@pytest.mark.parametrize("name", sorted(CLI_CASES))
def test_command_line_output_is_unchanged(name: str, run) -> None:
    assert run(name) == CLI_GOLD[name]


def _items_listing(items, money: Callable[[int], str]) -> str:
    rows = [[item.sku, item.name, money(item.price_cents)] for item in items]
    widths = [len(h) for h in ("SKU", "Name", "Price")]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row, strict=True)]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(("SKU", "Name", "Price"))).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append(
            "  ".join(
                [row[0].ljust(widths[0]), row[1].ljust(widths[1]), row[2].rjust(widths[2])]
            ).rstrip()
        )
    return "\n".join(lines) + "\n"


def test_command_line_writes_every_amount_through_the_currency(
    run, monkeypatch: pytest.MonkeyPatch
) -> None:
    Currency = currency_class()
    seen: list[str] = []

    def tagged(self, cents: int) -> str:
        seen.append(self.symbol)
        return f"<{self.symbol}|{cents:>8}>"

    monkeypatch.setattr(Currency, "format", tagged)
    code, out, err = run("items-euro")
    items = [i for i in read_items(ITEMS_CSV) if "office" in i.tags]
    assert (code, out, err) == (0, _items_listing(items, lambda c: f"<EUR |{c:>8}>"), "")
    assert set(seen) == {"EUR "}
    seen.clear()
    assert run("price-tax-euro") == (
        0,
        "5 x PEN-100 Fountain Pen: <EUR |   15300>\ntax: <EUR |    1262>\ntotal: <EUR |   16562>\n",
        "",
    )
    assert set(seen) == {"EUR "}
    seen.clear()
    code, out, err = run("sales")
    orders = read_orders(ORDERS_CSV)
    assert out == oracle_render(oracle_figures(orders, 3), lambda c: f"<$|{c:>8}>") + "\n"
    assert set(seen) == {"$"} and (code, err) == (0, "")


def test_command_line_does_not_use_the_deprecated_argument(tmp_path: Path) -> None:
    (tmp_path / "orders.csv").write_text(ORDERS_CSV, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env.update(PYTHONPATH=str(ROOT), STOCKROOM_CURRENCY_SYMBOL="EUR ")
    done = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::DeprecationWarning",
            "-m",
            "stockroom",
            "sales",
            "--orders",
            "orders.csv",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stderr == "" and done.stdout.startswith(
        "4 orders, 1 cancelled\nRevenue: EUR 951.50\n"
    )
