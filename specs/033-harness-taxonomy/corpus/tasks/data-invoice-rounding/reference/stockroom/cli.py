"""The ``stockroom`` command (``python -m stockroom``).

Exit codes: 0 success, 1 a data or settings error (``stockroom: error: ...`` on stderr),
2 a usage error (from argparse).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from . import __version__
from .config import load_config
from .csvio import read_items, read_orders, read_stock
from .dates import parse_date
from .errors import ParseError, StockroomError
from .inventory import Inventory
from .models import Item
from .money import format_money
from .orders import OrderBook
from .pricing import apply_discount, tax
from .report import sales_summary
from .rounding import MODES, invoice, parse_rate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stockroom", description="Inventory and order ledger.")
    parser.add_argument("--version", action="version", version=f"stockroom {__version__}")
    parser.add_argument("--config", type=Path, help="INI settings file")
    sub = parser.add_subparsers(dest="command", required=True)

    items = sub.add_parser("items", help="list the catalogue")
    items.add_argument("--file", type=Path, required=True)
    items.add_argument("--tag", help="only items with this tag")

    stock = sub.add_parser("stock", help="list items below their reorder level")
    stock.add_argument("--items", type=Path, required=True)
    stock.add_argument("--stock", type=Path, required=True)

    sales = sub.add_parser("sales", help="sales summary")
    sales.add_argument("--orders", type=Path, required=True)
    sales.add_argument("--from", dest="start", help="first day (YYYY-MM-DD)")
    sales.add_argument("--to", dest="end", help="last day (YYYY-MM-DD)")

    price = sub.add_parser("price", help="price a quantity of one item")
    price.add_argument("--items", type=Path, required=True)
    price.add_argument("--sku", required=True)
    price.add_argument("--quantity", type=int, required=True)
    price.add_argument("--discount", type=float, default=0.0, help="percent off")

    bill = sub.add_parser("invoice", help="an invoice or credit note for one order")
    bill.add_argument("--orders", type=Path, required=True)
    bill.add_argument("--order", required=True, help="the order id")
    bill.add_argument("--tax", help="tax rate such as 8.25%% or 825bp")
    bill.add_argument("--discount", default="0bp", help="discount rate such as 10%%")
    bill.add_argument("--rounding", choices=MODES, default="half_even")
    bill.add_argument("--credit", action="store_true", help="a credit note (negative amounts)")
    return parser


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _cmd_items(args: argparse.Namespace, config: dict[str, Any]) -> int:
    items = read_items(_read(args.file))
    if args.tag:
        items = [item for item in items if args.tag.lower() in item.tags]
    symbol = config["currency_symbol"]
    rows = [[item.sku, item.name, format_money(item.price_cents, symbol)] for item in items]
    headers = ["SKU", "Name", "Price"]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip())
    print("  ".join("-" * w for w in widths))
    for row in rows:
        cells = [row[0].ljust(widths[0]), row[1].ljust(widths[1]), row[2].rjust(widths[2])]
        print("  ".join(cells).rstrip())
    return 0


def _cmd_stock(args: argparse.Namespace, config: dict[str, Any]) -> int:
    items = read_items(_read(args.items))
    inventory = Inventory.from_levels(read_stock(_read(args.stock)))
    low = inventory.low_stock(items)
    if not low:
        print("stock ok")
        return 0
    for item in low:
        print(f"LOW {item.sku} {item.name}: {inventory.available(item.sku)}/{item.reorder_level}")
    if config["low_stock_warning"]:
        print(f"warning: {len(low)} item(s) below reorder level", file=sys.stderr)
    return 0


def _cmd_sales(args: argparse.Namespace, config: dict[str, Any]) -> int:
    book = OrderBook(read_orders(_read(args.orders)))
    if args.start or args.end:
        start = parse_date(args.start) if args.start else date.min
        end = parse_date(args.end) if args.end else date.max
        book = OrderBook(book.between(start, end))
    print(sales_summary(book, symbol=config["currency_symbol"]))
    return 0


def _find(items: Sequence[Item], sku: str) -> Item:
    for item in items:
        if item.sku == sku:
            return item
    raise ParseError(f"no item {sku}")


def _cmd_price(args: argparse.Namespace, config: dict[str, Any]) -> int:
    if args.quantity <= 0:
        raise ParseError("quantity must be positive")
    item = _find(read_items(_read(args.items)), args.sku)
    symbol = config["currency_symbol"]
    subtotal = apply_discount(item.price_cents * args.quantity, args.discount)
    taxed = tax(subtotal, config["tax_bp"])
    print(f"{args.quantity} x {item.sku} {item.name}: {format_money(subtotal, symbol)}")
    print(f"tax: {format_money(taxed, symbol)}")
    print(f"total: {format_money(subtotal + taxed, symbol)}")
    return 0


def _cmd_invoice(args: argparse.Namespace, config: dict[str, Any]) -> int:
    tax_bp = config["tax_bp"] if args.tax is None else parse_rate(args.tax)
    if tax_bp > 10000:
        raise ParseError("tax rate above 100%")
    discount_bp = parse_rate(args.discount)
    order = OrderBook(read_orders(_read(args.orders))).get(args.order)
    bill = invoice(
        order, tax_bp=tax_bp, discount_bp=discount_bp, mode=args.rounding, credit=args.credit
    )
    symbol = config["currency_symbol"]

    def money(cents: int) -> str:
        return format_money(cents, symbol)

    title = "credit note" if args.credit else "invoice"
    out = [f"{title} {order.order_id} {order.customer} {order.placed.isoformat()}"]
    for line, source in zip(bill.lines, order.lines, strict=True):
        out.append(
            f"{line.sku} {line.quantity} x {money(source.unit_price_cents)}:"
            f" amount {money(line.amount)} discount {money(line.discount)}"
            f" net {money(line.net)} tax {money(line.tax)}"
        )
    out += [
        f"amount: {money(bill.amount)}",
        f"discount: {money(bill.discount)}",
        f"net: {money(bill.net)}",
        f"tax: {money(bill.tax)}",
        f"total: {money(bill.total)}",
        f"tax on total: {money(bill.tax_on_total)}"
        f" (difference {money(bill.tax_on_total - bill.tax)})",
    ]
    print("\n".join(out))
    return 0


COMMANDS = {
    "items": _cmd_items,
    "stock": _cmd_stock,
    "sales": _cmd_sales,
    "price": _cmd_price,
    "invoice": _cmd_invoice,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config, os.environ)
        return COMMANDS[args.command](args, config)
    except StockroomError as exc:
        print(f"stockroom: error: {exc}", file=sys.stderr)
        return 1
