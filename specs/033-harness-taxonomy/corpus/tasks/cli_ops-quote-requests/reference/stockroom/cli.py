"""The ``stockroom`` command (``python -m stockroom``).

Exit codes: 0 success, 1 a data or settings error (``stockroom: error: ...`` on stderr),
2 a usage error (from argparse).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
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
from .quotes import price_requests
from .report import sales_summary


def _basis_points(text: str) -> int:
    if not re.fullmatch(r"[0-9]+", text):
        raise argparse.ArgumentTypeError(f"not a whole number of basis points: {text!r}")
    return int(text)


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

    quote = sub.add_parser("quote", help="price a list of SKU QUANTITY requests")
    quote.add_argument("--items", type=Path, required=True)
    quote.add_argument("--tax-bp", type=_basis_points, help="tax rate in basis points")
    quote.add_argument("--out", help="write the quote to this file ('-': stdout)")
    quote.add_argument("requests", nargs="?", default="-", metavar="REQUESTS")
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


def _input_text(name: str, *, stdin: bool) -> str:
    try:
        data = sys.stdin.buffer.read() if stdin else Path(name).read_bytes()
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        where = "standard input" if stdin else name
        raise ParseError(f"cannot read {where}: {exc}") from exc


def _same_file(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _write_atomic(name: str, text: str) -> None:
    path = Path(name)
    try:
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    except OSError as exc:
        raise StockroomError(f"cannot write {name}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temp, path)
    except OSError as exc:
        Path(temp).unlink(missing_ok=True)
        raise StockroomError(f"cannot write {name}: {exc}") from exc


def _cmd_quote(args: argparse.Namespace, config: dict[str, Any]) -> int:
    out = args.out if args.out not in (None, "-") else None
    if out is not None:
        sources = [str(args.items)] + ([args.requests] if args.requests != "-" else [])
        for source in sources:
            if _same_file(out, source):
                raise StockroomError(f"refusing to overwrite the input file {out}")
        if Path(out).is_dir() or not Path(out).parent.is_dir():
            raise StockroomError(f"cannot write {out}: not a file in an existing directory")
    items = read_items(_input_text(str(args.items), stdin=False))
    priced, failed = price_requests(_input_text(args.requests, stdin=args.requests == "-"), items)
    for number, reason in failed:
        print(f"stockroom: error: line {number}: {reason}", file=sys.stderr)
    if failed and not priced:
        return 1
    symbol = config["currency_symbol"]
    rate = args.tax_bp if args.tax_bp is not None else config["tax_bp"]
    lines = [
        f"{q.number}: {q.quantity} x {q.item.sku} {q.item.name} = "
        f"{format_money(q.amount_cents, symbol)}"
        for q in priced
    ]
    subtotal = sum(q.amount_cents for q in priced)
    taxed = tax(subtotal, rate)
    lines.append(f"subtotal: {format_money(subtotal, symbol)}")
    lines.append(f"tax: {format_money(taxed, symbol)}")
    lines.append(f"total: {format_money(subtotal + taxed, symbol)}")
    text = "".join(line + "\n" for line in lines)
    if out is None:
        sys.stdout.write(text)
    else:
        _write_atomic(out, text)
    return 3 if failed else 0


COMMANDS = {
    "items": _cmd_items,
    "stock": _cmd_stock,
    "sales": _cmd_sales,
    "price": _cmd_price,
    "quote": _cmd_quote,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config, os.environ)
        return COMMANDS[args.command](args, config)
    except StockroomError as exc:
        print(f"stockroom: error: {exc}", file=sys.stderr)
        return 1
