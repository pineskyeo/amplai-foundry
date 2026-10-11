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
from .config import data_dir_for, load_config
from .csvio import read_items, read_orders, read_stock
from .dates import parse_date
from .errors import ParseError, StockroomError
from .inventory import Inventory
from .models import Item
from .money import format_money
from .orders import OrderBook
from .pricing import apply_discount, tax
from .report import sales_summary


def _non_empty(text: str) -> str:
    if not text:
        raise argparse.ArgumentTypeError("must not be empty")
    return text


# (command, attribute, option, default file name)
DEFAULT_FILES = [
    ("items", "file", "--file", "items.csv"),
    ("stock", "items", "--items", "items.csv"),
    ("stock", "stock", "--stock", "stock.csv"),
    ("sales", "orders", "--orders", "orders.csv"),
    ("price", "items", "--items", "items.csv"),
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stockroom", description="Inventory and order ledger.")
    parser.add_argument("--version", action="version", version=f"stockroom {__version__}")
    parser.add_argument("--config", type=Path, help="INI settings file")
    parser.add_argument("--data-dir", type=_non_empty, help="directory of the default files")
    sub = parser.add_subparsers(dest="command", required=True)

    items = sub.add_parser("items", help="list the catalogue")
    items.add_argument("--file", type=Path)
    items.add_argument("--tag", help="only items with this tag")

    stock = sub.add_parser("stock", help="list items below their reorder level")
    stock.add_argument("--items", type=Path)
    stock.add_argument("--stock", type=Path)

    sales = sub.add_parser("sales", help="sales summary")
    sales.add_argument("--orders", type=Path)
    sales.add_argument("--from", dest="start", help="first day (YYYY-MM-DD)")
    sales.add_argument("--to", dest="end", help="last day (YYYY-MM-DD)")

    price = sub.add_parser("price", help="price a quantity of one item")
    price.add_argument("--items", type=Path)
    price.add_argument("--sku", required=True)
    price.add_argument("--quantity", type=int, required=True)
    price.add_argument("--discount", type=float, default=0.0, help="percent off")
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


COMMANDS = {"items": _cmd_items, "stock": _cmd_stock, "sales": _cmd_sales, "price": _cmd_price}


def _fill_defaults(
    parser: argparse.ArgumentParser, args: argparse.Namespace, environ: dict[str, str]
) -> None:
    missing = [
        (attr, option, name)
        for command, attr, option, name in DEFAULT_FILES
        if command == args.command and getattr(args, attr) is None
    ]
    if not missing:
        return
    folder = data_dir_for(args.data_dir, args.config, environ)
    if folder is None:
        options = ", ".join(option for _, option, _ in missing)
        parser.error(f"the following arguments are required: {options} (or set a data directory)")
    if not folder.is_dir():
        raise StockroomError(f"data directory {folder} is not a directory")
    for attr, _, name in missing:
        setattr(args, attr, folder / name)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, os.environ)
        _fill_defaults(parser, args, dict(os.environ))
        return COMMANDS[args.command](args, config)
    except StockroomError as exc:
        print(f"stockroom: error: {exc}", file=sys.stderr)
        return 1
