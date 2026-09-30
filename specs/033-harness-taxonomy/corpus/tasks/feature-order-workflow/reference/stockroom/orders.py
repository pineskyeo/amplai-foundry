"""An order book: orders by id, by customer, by date range, and revenue per month."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date

from .dates import month_key
from .errors import StockroomError
from .models import Order
from .pricing import order_subtotal


class OrderBook:
    def __init__(self, orders: Iterable[Order] = ()) -> None:
        self._orders: dict[str, Order] = {}
        self._history: dict[str, list[tuple[str, str]]] = {}
        for order in orders:
            self.add(order)

    def __len__(self) -> int:
        return len(self._orders)

    def __iter__(self) -> Iterator[Order]:
        return iter(self._orders.values())

    def add(self, order: Order) -> None:
        if order.order_id in self._orders:
            raise StockroomError(f"duplicate order id {order.order_id}")
        self._orders[order.order_id] = order

    def get(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError:
            raise StockroomError(f"no order {order_id}") from None

    def replace(self, order: Order) -> None:
        """Swap in ``order`` for the stored order with the same id, keeping its position.

        A changed status is appended to that order's history as ``(old, new)``."""
        old = self.get(order.order_id)
        if old.status != order.status:
            self._history.setdefault(order.order_id, []).append((old.status, order.status))
        self._orders[order.order_id] = order

    def history(self, order_id: str) -> list[tuple[str, str]]:
        """The status changes recorded by ``replace`` for ``order_id``, oldest first."""
        self.get(order_id)
        return list(self._history.get(order_id, []))

    def by_customer(self, customer: str) -> list[Order]:
        """The customer's orders, oldest first (ties in insertion order)."""
        found = [o for o in self._orders.values() if o.customer == customer]
        return sorted(found, key=lambda o: o.placed)

    def between(self, start: date, end: date) -> list[Order]:
        """Orders placed from ``start`` to ``end``, both dates included, oldest first."""
        found = [o for o in self._orders.values() if start <= o.placed < end]
        return sorted(found, key=lambda o: o.placed)

    def revenue_by_month(self) -> dict[str, int]:
        """``{"YYYY-MM": cents}`` of the subtotals of orders that are not cancelled, by month."""
        revenue: dict[str, int] = {}
        for order in self._orders.values():
            if order.status == "cancelled":
                continue
            key = month_key(order.placed)
            revenue[key] = revenue.get(key, 0) + order_subtotal(order)
        return dict(sorted(revenue.items()))
