"""Stock allocation: free stock handed to waiting orders by priority and held as reservations."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .errors import StockError, StockroomError
from .inventory import Inventory
from .models import Order

_STATUS_RANK = {"paid": 0, "open": 1}


@dataclass(frozen=True)
class Allocation:
    order_id: str
    line: int
    sku: str
    wanted: int
    held: int


def _distinct(orders: Iterable[Order]) -> list[Order]:
    found = list(orders)
    seen: set[str] = set()
    for order in found:
        if order.order_id in seen:
            raise StockroomError(f"duplicate order id {order.order_id}")
        seen.add(order.order_id)
    return found


def priority_queue(orders: Iterable[Order]) -> list[Order]:
    """Paid then open orders, oldest first, ties by order id (plain str order)."""
    waiting = [order for order in _distinct(orders) if order.status in _STATUS_RANK]
    return sorted(waiting, key=lambda o: (_STATUS_RANK[o.status], o.placed, o.order_id))


class Allocator:
    """Holds reserved stock for order lines, keyed by order id and 1-based line number."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory
        self._holdings: dict[str, dict[int, tuple[str, int]]] = {}

    def _units(self, order_id: str, number: int) -> int:
        return self._holdings.get(order_id, {}).get(number, ("", 0))[1]

    def _take(self, order_id: str, number: int, sku: str, units: int) -> None:
        self._inventory.reserve(sku, units)
        lines = self._holdings.setdefault(order_id, {})
        lines[number] = (sku, lines.get(number, (sku, 0))[1] + units)

    def run(self, orders: Iterable[Order], *, complete_only: bool = False) -> list[Allocation]:
        queue = priority_queue(orders)
        plan: list[Allocation] = []
        for order in queue:
            numbered = list(enumerate(order.lines, start=1))
            missing = {
                n: max(0, line.quantity - self._units(order.order_id, n)) for n, line in numbered
            }
            if complete_only:
                need: dict[str, int] = {}
                for n, line in numbered:
                    need[line.sku] = need.get(line.sku, 0) + missing[n]
                if all(units <= self._inventory.available(sku) for sku, units in need.items()):
                    for n, line in numbered:
                        if missing[n] > 0:
                            self._take(order.order_id, n, line.sku, missing[n])
            else:
                for n, line in numbered:
                    units = min(missing[n], self._inventory.available(line.sku))
                    if units > 0:
                        self._take(order.order_id, n, line.sku, units)
            plan += [
                Allocation(
                    order.order_id, n, line.sku, line.quantity, self._units(order.order_id, n)
                )
                for n, line in numbered
            ]
        return plan

    def held(self, order_id: str) -> dict[int, int]:
        lines = self._holdings.get(order_id, {})
        return {n: lines[n][1] for n in sorted(lines) if lines[n][1] > 0}

    def cancel(self, order_id: str) -> int:
        lines = self._holdings.pop(order_id, {})
        total = 0
        for n in sorted(lines):
            sku, units = lines[n]
            if units > 0:
                self._inventory.release(sku, units)
                total += units
        return total

    def ship(self, order: Order) -> None:
        for n, line in enumerate(order.lines, start=1):
            if self._units(order.order_id, n) != line.quantity:
                raise StockError(f"{order.order_id} line {n} is not fully held")
        for line in order.lines:
            self._inventory.ship(line.sku, line.quantity)
        self._holdings.pop(order.order_id, None)
