"""The stock level file."""

from __future__ import annotations

import csv
import io

from .csv_common import int_cell
from .errors import ParseError
from .models import check_sku

STOCK_HEADER = ("sku", "on_hand", "reserved")


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
        levels.append((sku, int_cell(row[1], "on_hand", number), int_cell(row[2], "reserved", number)))
    return levels
