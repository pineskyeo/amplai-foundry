"""CSV files: the item catalogue, stock levels and orders.

Every reader takes the file's text and every writer returns text, so callers decide where the
bytes come from and go.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from .dates import format_date, parse_date
from .errors import ParseError
from .models import Item, Order, OrderLine, check_sku
from .money import format_money, parse_money

ITEM_HEADER = ("sku", "name", "price", "tags", "reorder_level")
STOCK_HEADER = ("sku", "on_hand", "reserved")
ORDER_HEADER = ("order_id", "customer", "placed", "status", "sku", "quantity", "unit_price")


def _int(value: str, what: str, number: int) -> int:
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ParseError(f"line {number}: {what} is not a whole number: {value!r}") from exc


def parse_tags(cell: str) -> tuple[str, ...]:
    """``"Kitchen; gift"`` -> ``("kitchen", "gift")``: ``;``-separated, trimmed, lower-case.

    ``\\;`` is a ``;`` and ``\\\\`` a backslash inside a tag; any other backslash is a
    ``ParseError``. Empty tags are dropped.
    """
    segments: list[list[tuple[str, bool]]] = [[]]
    chars = iter(cell)
    for char in chars:
        if char == "\\":
            escaped = next(chars, "")
            if escaped not in (";", "\\"):
                raise ParseError(f"bad escape in tags: {cell!r}")
            segments[-1].append((escaped, True))
        elif char == ";":
            segments.append([])
        else:
            segments[-1].append((char, False))
    tags = []
    for segment in segments:
        while segment and not segment[0][1] and segment[0][0].isspace():
            segment = segment[1:]
        while segment and not segment[-1][1] and segment[-1][0].isspace():
            segment = segment[:-1]
        tag = "".join(char for char, _ in segment).lower()
        if tag:
            tags.append(tag)
    return tuple(tags)


def format_tags(tags: Iterable[str]) -> str:
    """The cell for ``tags``: each tag with ``\\`` and ``;`` escaped, joined by ``;``."""
    return ";".join(tag.replace("\\", "\\\\").replace(";", "\\;") for tag in tags)


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
                format_tags(item.tags),
                item.reorder_level,
            ]
        )
    return out.getvalue()


def read_stock(text: str) -> list[tuple[str, int, int]]:
    """``(sku, on_hand, reserved)`` rows of a stock file with the header ``STOCK_HEADER``."""
    reader = csv.reader(io.StringIO(text))
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


def read_orders(text: str) -> list[Order]:
    """Orders from one row per order line; rows of one order share ``order_id``, ``customer``,
    ``placed`` and ``status``. Orders come back in the order their first row appears."""
    reader = csv.DictReader(io.StringIO(text))
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
                unit_price_cents=parse_money(row["unit_price"]),
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


def write_orders(orders: Iterable[Order]) -> str:
    """The order file for ``orders`` (``read_orders`` reads it back)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
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
                    format_money(line.unit_price_cents, symbol=""),
                ]
            )
    return out.getvalue()
