"""The ``stockroom`` command (``python -m stockroom``).

Exit codes: 0 success, 1 a data or settings error (``stockroom: error: ...`` on stderr),
2 a usage error (from argparse).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import __version__
from .config import load_config
from .csvio import read_items, read_orders, read_stock
from .dates import parse_date
from .errors import ParseError, StockroomError
from .inventory import Inventory
from .lots import LotStore, read_lots
from .models import Item, check_sku
from .money import format_money
from .orders import OrderBook
from .pricing import apply_discount, tax
from .report import sales_summary


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

    lots = sub.add_parser("lots", help="list stock lots by expiry")
    lots.add_argument("--lots", type=Path, required=True)
    lots.add_argument("--day", required=True, help="YYYY-MM-DD")
    lots.add_argument("--within", type=_days, default=7, help="days ahead that count as expiring")
    lots.add_argument("--pick", action="append", default=[], metavar="SKU=N")
    return parser


def _days(text: str) -> int:
    if not (text.isascii() and text.isdigit()):
        raise ValueError(f"not a number of days: {text!r}")
    return int(text)


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


def _pick_spec(spec: str) -> tuple[str, int]:
    sku, sep, number = spec.partition("=")
    if not sep or not number.isascii() or not number.isdigit() or int(number) < 1:
        raise ParseError(f"--pick: expected SKU=N, got {spec!r}")
    return check_sku(sku), int(number)


def _cmd_lots(args: argparse.Namespace, config: dict[str, Any]) -> int:
    day = parse_date(args.day)
    picks = [_pick_spec(spec) for spec in args.pick]
    store = LotStore(Inventory())
    received = read_lots(_read(args.lots))
    for lot in received:
        store.receive(lot)
    out = []
    for sku, units in picks:
        taken = store.pick(sku, units, day)
        out.append(f"picked {units} {sku}: " + ", ".join(f"{lot} x{n}" for lot, n in taken))
    horizon = day + timedelta(days=args.within)
    usable = expiring = expired = 0
    for sku in sorted({lot.sku for lot in received}):
        for lot in store.lots(sku):
            if lot.expires < day:
                state = "expired"
                expired += lot.quantity
            elif lot.expires <= horizon:
                state = "expiring"
                expiring += lot.quantity
                usable += lot.quantity
            else:
                state = "ok"
                usable += lot.quantity
            out.append(f"{lot.sku:<10}{lot.lot_id:<10}{lot.expires}{lot.quantity:>6}  {state}")
    out.append(f"usable {usable}  expiring {expiring}  expired {expired}")
    print("\n".join(out))
    return 0


COMMANDS = {
    "items": _cmd_items,
    "stock": _cmd_stock,
    "sales": _cmd_sales,
    "price": _cmd_price,
    "lots": _cmd_lots,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config, os.environ)
        return COMMANDS[args.command](args, config)
    except StockroomError as exc:
        print(f"stockroom: error: {exc}", file=sys.stderr)
        return 1
