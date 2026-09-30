"""The item catalogue file."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from .csv_common import int_cell
from .errors import ParseError
from .models import Item
from .money import format_money, parse_money

ITEM_HEADER = ("sku", "name", "price", "tags", "reorder_level")


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
                reorder_level=int_cell(reorder, "reorder_level", number),
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
