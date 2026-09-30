"""The ``stockroom`` command (``python -m stockroom``).

Exit codes: 0 success, 1 a data or settings error (``stockroom: error: ...`` on stderr),
2 a usage error (from argparse).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Command:
    """One subcommand: how its options are declared and what it does."""

    name: str
    help: str
    configure: Callable[[argparse.ArgumentParser], None]
    run: Callable[[argparse.Namespace, dict[str, Any]], int]


COMMANDS: dict[str, Command] = {}


def _register(
    name: str, help: str, configure: Callable[[argparse.ArgumentParser], None]
) -> Callable[[Callable[[argparse.Namespace, dict[str, Any]], int]], Callable[..., int]]:
    def decorate(run: Callable[[argparse.Namespace, dict[str, Any]], int]) -> Callable[..., int]:
        COMMANDS[name] = Command(name, help, configure, run)
        return run

    return decorate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stockroom", description="Inventory and order ledger.")
    parser.add_argument("--version", action="version", version=f"stockroom {__version__}")
    parser.add_argument("--config", type=Path, help="INI settings file")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in COMMANDS.values():
        command.configure(sub.add_parser(command.name, help=command.help))
    return parser


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _items_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--tag", help="only items with this tag")


@_register("items", "list the catalogue", _items_options)
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


def _stock_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--items", type=Path, required=True)
    parser.add_argument("--stock", type=Path, required=True)


@_register("stock", "list items below their reorder level", _stock_options)
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


def _sales_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--orders", type=Path, required=True)
    parser.add_argument("--from", dest="start", help="first day (YYYY-MM-DD)")
    parser.add_argument("--to", dest="end", help="last day (YYYY-MM-DD)")


@_register("sales", "sales summary", _sales_options)
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


def _price_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--items", type=Path, required=True)
    parser.add_argument("--sku", required=True)
    parser.add_argument("--quantity", type=int, required=True)
    parser.add_argument("--discount", type=float, default=0.0, help="percent off")


@_register("price", "price a quantity of one item", _price_options)
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config, os.environ)
        return COMMANDS[args.command].run(args, config)
    except StockroomError as exc:
        print(f"stockroom: error: {exc}", file=sys.stderr)
        return 1
