"""Orders as JSON: a list of order objects, keys sorted, amounts in cents."""

from __future__ import annotations

import io
import json
from collections.abc import Iterable
from typing import Any, Protocol

from .dates import format_date, parse_date
from .errors import ParseError
from .models import Order, OrderLine


def order_to_dict(order: Order) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "customer": order.customer,
        "placed": format_date(order.placed),
        "status": order.status,
        "lines": [
            {"sku": line.sku, "quantity": line.quantity, "unit_price_cents": line.unit_price_cents}
            for line in order.lines
        ],
    }


def order_from_dict(value: dict[str, Any]) -> Order:
    try:
        return Order(
            order_id=value["order_id"],
            customer=value["customer"],
            placed=parse_date(value["placed"]),
            lines=tuple(
                OrderLine(
                    sku=line["sku"],
                    quantity=line["quantity"],
                    unit_price_cents=line["unit_price_cents"],
                )
                for line in value["lines"]
            ),
            status=value.get("state", "open"),
        )
    except (KeyError, TypeError) as exc:
        raise ParseError(f"bad order object: {exc}") from exc


class Writable(Protocol):
    def write(self, text: str, /) -> object: ...


def write_orders_json(orders: Iterable[Order], stream: Writable) -> int:
    """Write the JSON text of ``orders`` to ``stream``, one order at a time; the number written.

    The text is the one ``dump_orders`` returns. An order is written before the next one is
    taken from ``orders``, and ``stream.write`` is the only method used.
    """
    count = 0
    stream.write("[")
    for order in orders:
        block = json.dumps(order_to_dict(order), indent=2, sort_keys=True)
        stream.write(("\n  " if count == 0 else ",\n  ") + block.replace("\n", "\n  "))
        count += 1
    stream.write("\n]\n" if count else "]\n")
    return count


def dump_orders(orders: Iterable[Order]) -> str:
    """JSON text for ``orders``; ``load_orders`` reads it back."""
    buffer = io.StringIO()
    write_orders_json(orders, buffer)
    return buffer.getvalue()


def load_orders(text: str) -> list[Order]:
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ParseError(f"not JSON: {exc}") from exc
    if not isinstance(value, list):
        raise ParseError("an order file holds a JSON list")
    return [order_from_dict(item) for item in value]
