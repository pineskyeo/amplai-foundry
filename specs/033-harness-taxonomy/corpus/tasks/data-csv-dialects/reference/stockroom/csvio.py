"""CSV files: the item catalogue, stock levels and orders.

Every reader takes the file's text and every writer returns text, so callers decide where the
bytes come from and go.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Sequence

from .dates import format_date, parse_date
from .errors import ParseError
from .models import Item, Order, OrderLine, check_sku
from .money import format_money, parse_money

ITEM_HEADER = ("sku", "name", "price", "tags", "reorder_level")
STOCK_HEADER = ("sku", "on_hand", "reserved")
ORDER_HEADER = ("order_id", "customer", "placed", "status", "sku", "quantity", "unit_price")
DELIMITERS = (",", ";", "\t", "|")
_EUROPEAN_PRICE = re.compile(r"([0-9]{1,3}(?:\.[0-9]{3})+|[0-9]+)(?:,([0-9]{1,2}))?")
_POINT_PRICE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")


def _check_delimiter(delimiter: str) -> None:
    if delimiter not in DELIMITERS:
        raise ValueError(f"delimiter must be one of {DELIMITERS!r}, not {delimiter!r}")


def detect_delimiter(text: str, header: Sequence[str]) -> str:
    """The delimiter (``,`` ``;`` tab or ``|``) that splits the first line of ``text`` into
    exactly the cells of ``header`` (each cell trimmed); anything else is a ``ParseError``."""
    lines = text.splitlines()
    first = lines[0] if lines else ""
    for candidate in DELIMITERS:
        cells = next(csv.reader([first], delimiter=candidate), [])
        if tuple(cell.strip() for cell in cells) == tuple(header):
            return candidate
    raise ParseError(f"header must be {','.join(header)}, separated by , ; tab or |")


def parse_price(cell: str, delimiter: str) -> int:
    """A price cell in cents. In a ``,`` file: ``parse_money``. In any other file: digits with an
    optional decimal comma and ``.`` thousands (``1.250,50``), or digits with an optional decimal
    point (``1250.5``), no sign and no symbol."""
    if delimiter == ",":
        return parse_money(cell)
    text = cell.strip()
    match = _EUROPEAN_PRICE.fullmatch(text) or _POINT_PRICE.fullmatch(text)
    if match is None:
        raise ParseError(f"not an amount: {cell!r}")
    whole, frac = match.groups()
    return int(whole.replace(".", "")) * 100 + int((frac or "").ljust(2, "0"))


def _int(value: str, what: str, number: int) -> int:
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ParseError(f"line {number}: {what} is not a whole number: {value!r}") from exc


def parse_tags(cell: str) -> tuple[str, ...]:
    """``"Kitchen; gift"`` -> ``("kitchen", "gift")``: ``;``-separated, trimmed, lower-case."""
    return tuple(tag.strip().lower() for tag in cell.split(";") if tag.strip())


def read_items(text: str) -> list[Item]:
    """The items of a catalogue file with the header ``ITEM_HEADER``."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ParseError("empty item file")
    header = tuple(cell.strip() for cell in lines[0].split(","))
    if header != ITEM_HEADER:
        raise ParseError(f"item header must be {','.join(ITEM_HEADER)}")
    items = []
    for number, line in enumerate(lines[1:], start=2):
        cells = line.split(",")
        if len(cells) != len(ITEM_HEADER):
            raise ParseError(f"line {number}: expected {len(ITEM_HEADER)} fields")
        sku, name, price, tags, reorder = cells
        items.append(
            Item(
                sku=sku.strip(),
                name=name.strip(),
                price_cents=parse_money(price),
                tags=parse_tags(tags),
                reorder_level=_int(reorder, "reorder_level", number),
            )
        )
    return items


def write_items(items: Iterable[Item]) -> str:
    """The catalogue file for ``items`` (``read_items`` reads it back)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(ITEM_HEADER)
    for item in items:
        writer.writerow(
            [
                item.sku,
                item.name,
                format_money(item.price_cents, symbol=""),
                ";".join(item.tags),
                item.reorder_level,
            ]
        )
    return out.getvalue()


def read_stock(text: str, delimiter: str | None = None) -> list[tuple[str, int, int]]:
    """``(sku, on_hand, reserved)`` rows of a stock file with the header ``STOCK_HEADER``;
    without ``delimiter`` it is detected from the header line."""
    if delimiter is None:
        delimiter = detect_delimiter(text, STOCK_HEADER)
    else:
        _check_delimiter(delimiter)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows or tuple(cell.strip() for cell in rows[0]) != STOCK_HEADER:
        raise ParseError(f"stock header must be {','.join(STOCK_HEADER)}")
    levels = []
    for number, row in enumerate(rows[1:], start=2):
        if len(row) != len(STOCK_HEADER):
            raise ParseError(f"line {number}: expected {len(STOCK_HEADER)} fields")
        sku = check_sku(row[0].strip())
        levels.append((sku, _int(row[1], "on_hand", number), _int(row[2], "reserved", number)))
    return levels


def read_orders(text: str, delimiter: str | None = None) -> list[Order]:
    """Orders from one row per order line; rows of one order share ``order_id``, ``customer``,
    ``placed`` and ``status``. Orders come back in the order their first row appears. Without
    ``delimiter`` it is detected from the header line."""
    if delimiter is None:
        delimiter = detect_delimiter(text, ORDER_HEADER)
    else:
        _check_delimiter(delimiter)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if tuple(reader.fieldnames or ()) != ORDER_HEADER:
        raise ParseError(f"order header must be {','.join(ORDER_HEADER)}")
    heads: dict[str, dict[str, str]] = {}
    lines: dict[str, list[OrderLine]] = {}
    for number, row in enumerate(reader, start=2):
        order_id = row["order_id"].strip()
        if order_id not in heads:
            heads[order_id] = row
            lines[order_id] = []
        lines[order_id].append(
            OrderLine(
                sku=row["sku"].strip(),
                quantity=_int(row["quantity"], "quantity", number),
                unit_price_cents=parse_price(row["unit_price"], delimiter),
            )
        )
    return [
        Order(
            order_id=order_id,
            customer=head["customer"].strip(),
            placed=parse_date(head["placed"]),
            lines=tuple(lines[order_id]),
            status=head["status"].strip(),
        )
        for order_id, head in heads.items()
    ]


def _price_text(cents: int, delimiter: str, decimal: str) -> str:
    if delimiter == ",":
        return format_money(cents, symbol="")
    units, rest = divmod(cents, 100)
    return f"{units}{decimal}{rest:02d}"


def write_orders(orders: Iterable[Order], *, delimiter: str = ",", decimal: str = ".") -> str:
    """The order file for ``orders`` (``read_orders`` reads it back). Outside a ``,`` file the
    prices are written without thousands separators, with ``decimal`` as the decimal mark."""
    _check_delimiter(delimiter)
    if decimal not in (".", ",") or (delimiter == "," and decimal == ","):
        raise ValueError(f"decimal mark {decimal!r} does not go with delimiter {delimiter!r}")
    out = io.StringIO()
    writer = csv.writer(out, delimiter=delimiter, lineterminator="\n")
    writer.writerow(ORDER_HEADER)
    for order in orders:
        for line in order.lines:
            writer.writerow(
                [
                    order.order_id,
                    order.customer,
                    format_date(order.placed),
                    order.status,
                    line.sku,
                    line.quantity,
                    _price_text(line.unit_price_cents, delimiter, decimal),
                ]
            )
    return out.getvalue()
