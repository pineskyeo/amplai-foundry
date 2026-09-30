"""Stock levels per SKU: on hand, reserved for open orders, and available (on hand - reserved)."""

from __future__ import annotations

from collections.abc import Iterable

from .errors import StockError
from .models import Item, check_sku


class Inventory:
    def __init__(self) -> None:
        self._on_hand: dict[str, int] = {}
        self._reserved: dict[str, int] = {}

    @classmethod
    def from_levels(cls, levels: Iterable[tuple[str, int, int]]) -> Inventory:
        """An inventory from ``(sku, on_hand, reserved)`` rows (see ``csvio.read_stock``)."""
        inventory = cls()
        for sku, on_hand, reserved in levels:
            if on_hand < 0 or reserved < 0 or reserved > on_hand:
                raise StockError(f"{sku}: inconsistent levels {on_hand}/{reserved}")
            inventory._on_hand[check_sku(sku)] = on_hand
            inventory._reserved[sku] = reserved
        return inventory

    def copy(self) -> Inventory:
        other = Inventory()
        other._on_hand = dict(self._on_hand)
        other._reserved = dict(self._reserved)
        return other

    def on_hand(self, sku: str) -> int:
        return self._on_hand.get(sku, 0)

    def reserved(self, sku: str) -> int:
        return self._reserved.get(sku, 0)

    def available(self, sku: str) -> int:
        return self.on_hand(sku) - self.reserved(sku)

    @staticmethod
    def _positive(quantity: int) -> None:
        if quantity <= 0:
            raise StockError("quantity must be positive")

    def receive(self, sku: str, quantity: int) -> None:
        """``quantity`` units arrive."""
        self._positive(quantity)
        self._on_hand[check_sku(sku)] = self.on_hand(sku) + quantity

    def reserve(self, sku: str, quantity: int) -> None:
        """Hold ``quantity`` units for an order; they must be available."""
        self._positive(quantity)
        if quantity > self.on_hand(sku):
            raise StockError(f"{sku}: only {self.available(sku)} available")
        self._reserved[sku] = self.reserved(sku) + quantity

    def release(self, sku: str, quantity: int) -> None:
        """Give back ``quantity`` reserved units (an order was cancelled)."""
        self._positive(quantity)
        if quantity > self.reserved(sku):
            raise StockError(f"{sku}: only {self.reserved(sku)} reserved")
        self._reserved[sku] = self.reserved(sku) - quantity

    def ship(self, sku: str, quantity: int) -> None:
        """Ship ``quantity`` reserved units: they leave both reserved and on-hand stock."""
        self._positive(quantity)
        if quantity > self.reserved(sku):
            raise StockError(f"{sku}: only {self.reserved(sku)} reserved")
        self._reserved[sku] = self.reserved(sku) - quantity
        self._on_hand[sku] = self.on_hand(sku) - quantity

    def low_stock(self, items: Iterable[Item]) -> list[Item]:
        """The items whose available stock is below their reorder level, in the given order."""
        return [item for item in items if self.available(item.sku) < item.reorder_level]

    def snapshot(self) -> dict[str, tuple[int, int]]:
        """``{sku: (on_hand, reserved)}`` sorted by SKU."""
        return {sku: (self.on_hand(sku), self.reserved(sku)) for sku in sorted(self._on_hand)}
