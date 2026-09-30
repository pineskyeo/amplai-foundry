"""CSV files: the item catalogue, stock levels and orders.

Every reader takes the file's text and every writer returns text, so callers decide where the
bytes come from and go. The code lives in ``items_csv``, ``stock_csv`` and ``orders_csv``; this
module keeps the names callers have always imported.
"""

from __future__ import annotations

from .items_csv import ITEM_HEADER, parse_tags, read_items, write_items
from .orders_csv import ORDER_HEADER, read_orders, write_orders
from .stock_csv import STOCK_HEADER, read_stock

__all__ = [
    "ITEM_HEADER",
    "ORDER_HEADER",
    "STOCK_HEADER",
    "parse_tags",
    "read_items",
    "read_orders",
    "read_stock",
    "write_items",
    "write_orders",
]
