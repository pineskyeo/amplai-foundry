from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from stockroom.errors import ParseError
from stockroom.invoice import Invoice, InvoiceLine, build_invoice, render_invoice
from stockroom.models import Item, Order, OrderLine

ROOT = Path(__file__).resolve().parents[3]

ITEMS = [
    Item("MUG-001", "Blue Mug", 1250, ("kitchen", "gift"), 5),
    Item("TEA-010", "Green Tea 100g", 895, ("food", "tea"), 10),
    Item("PEN-100", "Fountain Pen", 3400, ("office", "gift"), 2),
    Item("NB-200", "Notebook A5", 625, (), 20),
]


def order(*lines: tuple[str, int, int], order_id: str = "A-1", customer: str = "Hana Kim") -> Order:
    return Order(
        order_id,
        customer,
        date(2024, 3, 1),
        tuple(OrderLine(sku, qty, price) for sku, qty, price in lines),
        "open",
    )


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_line_fields_come_from_order_and_catalogue() -> None:
    # the order's price (1000) differs from the catalogue price (1250) and is the one used
    invoice = build_invoice(order(("MUG-001", 3, 1000), ("NB-200", 4, 625)), ITEMS)
    first, second = invoice.lines
    assert first == InvoiceLine("MUG-001", "Blue Mug", 3, 1000, 3000, 0, 0)
    assert second == InvoiceLine("NB-200", "Notebook A5", 4, 625, 2500, 0, 0)
    assert (invoice.order_id, invoice.customer) == ("A-1", "Hana Kim")
    assert isinstance(invoice.lines, tuple)
    again = build_invoice(order(("TEA-010", 1, 895), ("MUG-001", 1, 1250)), ITEMS)
    assert [line.sku for line in again.lines] == ["TEA-010", "MUG-001"]
    twice = build_invoice(order(("MUG-001", 1, 100), ("MUG-001", 2, 100)), ITEMS)
    assert [(line.quantity, line.net_cents) for line in twice.lines] == [(1, 100), (2, 200)]


def test_dataclasses_are_frozen_and_ordered() -> None:
    assert [f.name for f in dataclasses.fields(InvoiceLine)] == [
        "sku",
        "name",
        "quantity",
        "unit_price_cents",
        "net_cents",
        "tax_bp",
        "tax_cents",
    ]
    assert [f.name for f in dataclasses.fields(Invoice)] == [
        "order_id",
        "customer",
        "lines",
        "subtotal_cents",
        "tax_cents",
        "total_cents",
    ]
    invoice = build_invoice(order(("NB-200", 1, 625)), ITEMS)
    with pytest.raises(dataclasses.FrozenInstanceError):
        invoice.total_cents = 1  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        invoice.lines[0].tax_bp = 1  # type: ignore[misc]
    assert Invoice("A-1", "Hana Kim", (), 0, 0, 0) == build_invoice(order(), ITEMS)


def test_default_rate_and_tag_rates() -> None:
    lines = (("MUG-001", 2, 1250), ("TEA-010", 1, 895), ("NB-200", 4, 625))
    plain = build_invoice(order(*lines), ITEMS, default_tax_bp=1000)
    assert [line.tax_bp for line in plain.lines] == [1000, 1000, 1000]
    assert [line.tax_cents for line in plain.lines] == [250, 90, 250]
    mapped = build_invoice(
        order(*lines), ITEMS, default_tax_bp=1000, tag_tax_bp={"food": 0, "gift": 1200, "zzz": 77}
    )
    assert [line.tax_bp for line in mapped.lines] == [1200, 0, 1000]
    assert [line.tax_cents for line in mapped.lines] == [300, 0, 250]
    only_tags = build_invoice(order(*lines), ITEMS, tag_tax_bp={"kitchen": 500})
    assert [line.tax_bp for line in only_tags.lines] == [500, 0, 0]
    assert [line.tax_cents for line in only_tags.lines] == [125, 0, 0]


def test_highest_tag_rate_wins_even_below_default() -> None:
    items = [Item("MUG-001", "Mug", 100, ("a", "b", "c"), 0), Item("NB-200", "Nb", 100, ("a",), 0)]
    rates = {"a": 300, "b": 900, "c": 100}
    invoice = build_invoice(order(("MUG-001", 1, 10000), ("NB-200", 1, 10000)), items, tag_tax_bp=rates)
    assert [line.tax_bp for line in invoice.lines] == [900, 300]
    low = build_invoice(order(("MUG-001", 1, 10000)), items, default_tax_bp=2000, tag_tax_bp=rates)
    assert low.lines[0].tax_bp == 900 and low.lines[0].tax_cents == 900
    zero = build_invoice(order(("NB-200", 1, 10000)), items, default_tax_bp=2000, tag_tax_bp={"a": 0})
    assert zero.lines[0].tax_bp == 0 and zero.lines[0].tax_cents == 0
    # tags are compared exactly: a differently cased key does not match
    exact = build_invoice(order(("NB-200", 1, 10000)), items, default_tax_bp=2000, tag_tax_bp={"A": 5})
    assert exact.lines[0].tax_bp == 2000


def test_tax_is_rounded_per_line() -> None:
    items = [Item("MUG-001", "Mug", 105, (), 0), Item("NB-200", "Nb", 105, (), 0)]
    invoice = build_invoice(order(("MUG-001", 1, 105), ("NB-200", 1, 105)), items, default_tax_bp=500)
    # 105 * 5 % = 5.25 -> 5 per line; the tax on the 210 subtotal would be 10.5 -> 11
    assert [line.tax_cents for line in invoice.lines] == [5, 5]
    assert invoice.tax_cents == 10 and invoice.total_cents == 220
    half = build_invoice(order(("MUG-001", 1, 10)), items, default_tax_bp=500)
    assert half.lines[0].tax_cents == 1  # 0.5 rounds up
    tiny = build_invoice(order(("MUG-001", 1, 9)), items, default_tax_bp=500)
    assert tiny.lines[0].tax_cents == 0  # 0.45 rounds down
    many = build_invoice(order(("MUG-001", 3, 333)), items, default_tax_bp=825)
    assert many.lines[0].net_cents == 999 and many.lines[0].tax_cents == 82  # 82.4175


def test_sums_and_empty_order() -> None:
    invoice = build_invoice(
        order(("MUG-001", 2, 1250), ("TEA-010", 1, 895), ("PEN-100", 1, 3400)),
        ITEMS,
        default_tax_bp=825,
        tag_tax_bp={"gift": 1000},
    )
    assert invoice.subtotal_cents == 2500 + 895 + 3400
    assert [line.tax_cents for line in invoice.lines] == [250, 74, 340]  # 895 * 8.25 % = 73.84
    assert invoice.tax_cents == 250 + 74 + 340
    assert invoice.total_cents == invoice.subtotal_cents + invoice.tax_cents == 6795 + 664
    empty = build_invoice(order(), ITEMS, default_tax_bp=825)
    assert empty.lines == () and (empty.subtotal_cents, empty.tax_cents, empty.total_cents) == (0, 0, 0)


def test_errors_and_argument_handling() -> None:
    with pytest.raises(ParseError):
        build_invoice(order(("MUG-001", 1, 1250), ("ZZZ-999", 1, 100)), ITEMS)
    with pytest.raises(ValueError):
        build_invoice(order(("MUG-001", 1, 1250)), ITEMS, default_tax_bp=-1)
    with pytest.raises(ValueError):
        build_invoice(order(("MUG-001", 1, 1250)), ITEMS, tag_tax_bp={"gift": 100, "food": -5})
    with pytest.raises(ValueError):  # an unused negative rate still counts
        build_invoice(order(("NB-200", 1, 625)), ITEMS, tag_tax_bp={"zzz": -5})
    with pytest.raises(ValueError):  # checked before the catalogue lookup
        build_invoice(order(("ZZZ-999", 1, 1)), ITEMS, default_tax_bp=-1)
    rates = {"gift": 1000}
    items = list(ITEMS)
    build_invoice(order(("MUG-001", 1, 1250)), iter(items), tag_tax_bp=rates)
    build_invoice(order(("MUG-001", 1, 1250)), (i for i in items), tag_tax_bp=rates)
    assert rates == {"gift": 1000} and items == ITEMS


def test_render_layout() -> None:
    invoice = build_invoice(
        order(("MUG-001", 3, 1250), ("TEA-010", 1, 895), order_id="A-1004", customer="Hana Kim"),
        ITEMS,
        default_tax_bp=825,
        tag_tax_bp={"gift": 1000},
    )
    assert render_invoice(invoice).split("\n") == [
        "Invoice A-1004 for Hana Kim",
        "MUG-001  Blue Mug  3 x $12.50 = $37.50  tax $3.75 (10.00%)",
        "TEA-010  Green Tea 100g  1 x $8.95 = $8.95  tax $0.74 (8.25%)",
        "Subtotal: $46.45",
        "Tax: $4.49",
        "Total: $50.94",
    ]
    assert not render_invoice(invoice).endswith("\n")
    empty = render_invoice(build_invoice(order(order_id="B-2", customer="Omar Diaz"), ITEMS))
    assert empty.split("\n") == [
        "Invoice B-2 for Omar Diaz",
        "Subtotal: $0.00",
        "Tax: $0.00",
        "Total: $0.00",
    ]


def test_render_rate_format_and_symbol() -> None:
    cases = {0: "0.00%", 5: "0.05%", 99: "0.99%", 100: "1.00%", 825: "8.25%", 1000: "10.00%", 12345: "123.45%"}
    for bp, text in cases.items():
        invoice = build_invoice(order(("NB-200", 1, 100000)), ITEMS, default_tax_bp=bp)
        assert render_invoice(invoice).split("\n")[1].endswith(f"({text})")
    invoice = build_invoice(order(("PEN-100", 2, 123456)), ITEMS, default_tax_bp=0)
    text = render_invoice(invoice, symbol="EUR ")
    assert "2 x EUR 1,234.56 = EUR 2,469.12  tax EUR 0.00 (0.00%)" in text
    assert text.endswith("Total: EUR 2,469.12")
    assert render_invoice(invoice, "$") == render_invoice(invoice)


def test_cli_invoice_with_tag_rates() -> None:
    done = run(
        "invoice", "--items", "data/items.csv", "--orders", "data/orders.csv", "--order", "A-1004",
        "--tag-tax", "gift=500", "--tag-tax", "HOME=1200",
        env={"STOCKROOM_TAX_BP": "1000"},
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "Invoice A-1004 for Hana Kim",
        "CND-007  Beeswax Candle  3 x $9.80 = $29.40  tax $3.53 (12.00%)",
        "NB-200  Notebook A5  4 x $6.25 = $25.00  tax $2.50 (10.00%)",
        "Subtotal: $54.40",
        "Tax: $6.03",
        "Total: $60.43",
    ]
    done = run(
        "invoice", "--items", "data/items.csv", "--orders", "data/orders.csv", "--order", "A-1001",
        "--tag-tax", "food=0",
        env={"STOCKROOM_TAX_BP": "825"},
    )  # fmt: skip
    assert done.stdout.splitlines() == [
        "Invoice A-1001 for Hana Kim",
        "MUG-001  Blue Mug  2 x $12.50 = $25.00  tax $2.06 (8.25%)",
        "TEA-010  Green Tea 100g  1 x $8.95 = $8.95  tax $0.00 (0.00%)",
        "Subtotal: $33.95",
        "Tax: $2.06",
        "Total: $36.01",
    ]


def test_cli_default_rate_and_symbol() -> None:
    done = run("invoice", "--items", "data/items.csv", "--orders", "data/orders.csv", "--order", "A-1002")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "Invoice A-1002 for Omar Diaz",
        "PEN-100  Fountain Pen  1 x $34.00 = $34.00  tax $0.00 (0.00%)",
        "Subtotal: $34.00",
        "Tax: $0.00",
        "Total: $34.00",
    ]
    done = run(
        "invoice", "--items", "data/items.csv", "--orders", "data/orders.csv", "--order", "A-1002",
        env={"STOCKROOM_TAX_BP": "825", "STOCKROOM_CURRENCY_SYMBOL": "EUR "},
    )  # fmt: skip
    assert done.stdout.splitlines()[1:] == [
        "PEN-100  Fountain Pen  1 x EUR 34.00 = EUR 34.00  tax EUR 2.81 (8.25%)",
        "Subtotal: EUR 34.00",
        "Tax: EUR 2.81",
        "Total: EUR 36.81",
    ]


@pytest.mark.parametrize(
    "extra",
    [
        ["--tag-tax", "gift"],
        ["--tag-tax", "gift="],
        ["--tag-tax", "=500"],
        ["--tag-tax", "gift=-5"],
        ["--tag-tax", "gift=5.5"],
        ["--tag-tax", "gift=abc"],
        ["--tag-tax", "gift=1", "--tag-tax", "gift=2"],
        ["--order", "A-9999"],
    ],
)
def test_cli_errors(extra: list[str]) -> None:
    args = ["invoice", "--items", "data/items.csv", "--orders", "data/orders.csv", "--order", "A-1002"]
    if extra[0] == "--order":
        args[-1] = extra[1]
    else:
        args += extra
    done = run(*args)
    assert done.returncode == 1, (extra, done.stdout, done.stderr)
    assert done.stdout == "" and done.stderr.startswith("stockroom: error: ")


def test_cli_unknown_sku(tmp_path: Path) -> None:
    items = tmp_path / "items.csv"
    items.write_text("sku,name,price,tags,reorder_level\nMUG-001,Blue Mug,12.50,kitchen,5\n")
    orders = tmp_path / "orders.csv"
    orders.write_text(
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        "A-1,Hana Kim,2024-03-01,open,MUG-001,1,12.50\n"
        "A-1,Hana Kim,2024-03-01,open,TEA-010,1,8.95\n"
    )
    done = run("invoice", "--items", str(items), "--orders", str(orders), "--order", "A-1")
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error: ")
