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


def _typed(value: Any, kind: type, what: str) -> Any:
    if kind is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    else:
        ok = isinstance(value, kind)
    if not ok:
        raise ParseError(f"bad order object: {what} must be a {kind.__name__}")
    return value


def order_from_dict(value: dict[str, Any]) -> Order:
    if not isinstance(value, dict):
        raise ParseError("bad order object: not an object")
    try:
        lines = _typed(value["lines"], list, "lines")
        for line in lines:
            _typed(line, dict, "a line")
        return Order(
            order_id=_typed(value["order_id"], str, "order_id"),
            customer=_typed(value["customer"], str, "customer"),
            placed=parse_date(_typed(value["placed"], str, "placed")),
            lines=tuple(
                OrderLine(
                    sku=_typed(line["sku"], str, "sku"),
                    quantity=_typed(line["quantity"], int, "quantity"),
                    unit_price_cents=_typed(line["unit_price_cents"], int, "unit_price_cents"),
                )
                for line in lines
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
