"""stockroom: a small inventory and order ledger."""

from ._version import __version__ as __version__
from .cli import main
from .config import load_config
from .csvio import read_items, read_orders, read_stock, write_items, write_orders
from .dates import format_date, parse_date
from .errors import ConfigError, ParseError, StockError, StockroomError
from .inventory import Inventory
from .jsonio import dump_orders, load_orders
from .models import Item, Order, OrderLine
from .money import format_money, parse_money
from .orders import OrderBook
from .pricing import order_total
from .report import sales_summary

__all__ = [  # noqa: RUF022 - the order of the table in the task
    "StockroomError",
    "ParseError",
    "StockError",
    "ConfigError",
    "Item",
    "OrderLine",
    "Order",
    "parse_money",
    "format_money",
    "parse_date",
    "format_date",
    "read_items",
    "write_items",
    "read_stock",
    "read_orders",
    "write_orders",
    "dump_orders",
    "load_orders",
    "Inventory",
    "OrderBook",
    "order_total",
    "sales_summary",
    "load_config",
    "main",
]
