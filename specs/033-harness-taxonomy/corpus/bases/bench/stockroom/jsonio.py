"""Orders as JSON: a list of order objects, keys sorted, amounts in cents."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

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


def dump_orders(orders: Iterable[Order]) -> str:
    """JSON text for ``orders``; ``load_orders`` reads it back."""
    return json.dumps([order_to_dict(o) for o in orders], indent=2, sort_keys=True) + "\n"


def load_orders(text: str) -> list[Order]:
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ParseError(f"not JSON: {exc}") from exc
    if not isinstance(value, list):
        raise ParseError("an order file holds a JSON list")
    return [order_from_dict(item) for item in value]
