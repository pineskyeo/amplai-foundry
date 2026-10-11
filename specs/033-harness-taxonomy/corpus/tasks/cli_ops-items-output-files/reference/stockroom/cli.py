"""The ``stockroom`` command (``python -m stockroom``).

Exit codes: 0 success, 1 a data or settings error (``stockroom: error: ...`` on stderr),
2 a usage error (from argparse).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from . import __version__
from .config import load_config
from .csvio import read_items, read_orders, read_stock, write_items
from .dates import parse_date
from .errors import ParseError, StockroomError
from .inventory import Inventory
from .models import Item
from .money import format_money
from .orders import OrderBook
from .pricing import apply_discount, tax
from .report import sales_summary

FORMATS = ("text", "csv", "tsv")
SUFFIXES = {".csv": "csv", ".tsv": "tsv", ".txt": "text"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stockroom", description="Inventory and order ledger.")
    parser.add_argument("--version", action="version", version=f"stockroom {__version__}")
    parser.add_argument("--config", type=Path, help="INI settings file")
    sub = parser.add_subparsers(dest="command", required=True)

    items = sub.add_parser("items", help="list the catalogue")
    items.add_argument("--file", type=Path, required=True)
    items.add_argument("--tag", help="only items with this tag")
    items.add_argument("--out", help="write the listing to this file ('-': stdout)")
    items.add_argument("--format", choices=FORMATS, help="text, csv or tsv")
    items.add_argument("--newline", choices=("lf", "crlf"), default="lf")
    items.add_argument("--bom", action="store_true", help="start with a UTF-8 byte order mark")
    items.add_argument("--force", action="store_true", help="replace an existing --out file")

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
    return parser


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _table_lines(items: Sequence[Item], symbol: str) -> list[str]:
    rows = [[item.sku, item.name, format_money(item.price_cents, symbol)] for item in items]
    headers = ["SKU", "Name", "Price"]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        cells = [row[0].ljust(widths[0]), row[1].ljust(widths[1]), row[2].rjust(widths[2])]
        lines.append("  ".join(cells).rstrip())
    return lines


def _listing(items: Sequence[Item], fmt: str, symbol: str) -> list[str]:
    if fmt == "csv":
        return write_items(items).splitlines()
    if fmt == "tsv":
        rows = [["sku", "name", "price", "tags", "reorder_level"]]
        for item in items:
            price = format_money(item.price_cents, symbol="")
            rows.append([item.sku, item.name, price, ";".join(item.tags), str(item.reorder_level)])
        return ["\t".join(row) for row in rows]
    return _table_lines(items, symbol)


def _check_out(out: str, source: Path) -> Path:
    path = Path(out)
    if os.path.realpath(out) == os.path.realpath(source):
        raise StockroomError(f"refusing to overwrite the input file {out}")
    if path.is_dir() or not path.parent.is_dir():
        raise StockroomError(f"cannot write {out}: not a file in an existing directory")
    return path


def _replace(path: Path, data: bytes) -> None:
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with open(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    except OSError as exc:
        Path(temp).unlink(missing_ok=True)
        raise StockroomError(f"cannot write {path}: {exc}") from exc


def _cmd_items(args: argparse.Namespace, config: dict[str, Any]) -> int:
    out = None if args.out in (None, "-") else args.out
    if out is not None:
        target = _check_out(out, args.file)
        if target.exists() and not args.force:
            raise StockroomError(f"{out} already exists (use --force to replace it)")
    items = read_items(_read(args.file))
    if args.tag:
        items = [item for item in items if args.tag.lower() in item.tags]
    fmt = args.format
    if fmt is None and out is not None:
        fmt = SUFFIXES.get(Path(out).suffix.lower())
    if fmt is None:
        fmt = config["items_format"]
    eol = "\r\n" if args.newline == "crlf" else "\n"
    text = "".join(line + eol for line in _listing(items, fmt, config["currency_symbol"]))
    data = ("\ufeff" if args.bom else "").encode("utf-8") + text.encode("utf-8")
    if out is None:
        sys.stdout.flush()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        _replace(target, data)
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config, os.environ)
        return COMMANDS[args.command](args, config)
    except StockroomError as exc:
        print(f"stockroom: error: {exc}", file=sys.stderr)
        return 1
