"""The order file: one row per order line."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from .csv_common import int_cell
from .dates import format_date, parse_date
from .errors import ParseError
from .models import Order, OrderLine
from .money import format_money, parse_money

ORDER_HEADER = ("order_id", "customer", "placed", "status", "sku", "quantity", "unit_price")


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
                quantity=int_cell(row["quantity"], "quantity", number),
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
