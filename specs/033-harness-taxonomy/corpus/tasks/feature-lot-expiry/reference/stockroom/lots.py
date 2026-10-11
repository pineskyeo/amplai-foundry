"""Stock lots with expiry dates: picked earliest expiry first, written off once expired."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, replace
from datetime import date

from .dates import parse_date
from .errors import ParseError, StockError, StockroomError
from .inventory import Inventory

LOT_HEADER = ("lot_id", "sku", "expires", "quantity")


@dataclass(frozen=True)
class Lot:
    lot_id: str
    sku: str
    expires: date
    quantity: int


def read_lots(text: str) -> list[Lot]:
    """The lots of a lots file with the header ``LOT_HEADER``, in file order."""
    rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    if not rows or tuple(cell.strip() for cell in rows[0]) != LOT_HEADER:
        raise ParseError(f"lots header must be {','.join(LOT_HEADER)}")
    lots = []
    for row in rows[1:]:
        if len(row) != len(LOT_HEADER):
            raise ParseError(f"a lot row has {len(LOT_HEADER)} fields")
        lot_id, sku, expires, quantity = (cell.strip() for cell in row)
        if not lot_id:
            raise ParseError("empty lot id")
        try:
            units = int(quantity)
        except ValueError as exc:
            raise ParseError(f"{lot_id}: quantity is not a whole number") from exc
        lots.append(Lot(lot_id, sku, parse_date(expires), units))
    return lots


class LotStore:
    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory
        self._lots: dict[str, Lot] = {}  # insertion order is the receipt order

    def receive(self, lot: Lot) -> None:
        if lot.quantity < 1:
            raise StockError(f"{lot.lot_id}: quantity must be positive")
        known = self._lots.get(lot.lot_id)
        if known is not None and (known.sku, known.expires) != (lot.sku, lot.expires):
            raise StockroomError(f"lot {lot.lot_id} is already a lot of {known.sku}")
        self._inventory.receive(lot.sku, lot.quantity)
        if known is None:
            self._lots[lot.lot_id] = lot
        else:
            self._lots[lot.lot_id] = replace(known, quantity=known.quantity + lot.quantity)

    def _ordered(self, sku: str) -> list[Lot]:
        found = [(lot.expires, n, lot) for n, lot in enumerate(self._lots.values())]
        return [lot for _, _, lot in sorted(found, key=lambda t: t[:2]) if lot.sku == sku]

    def lots(self, sku: str) -> list[Lot]:
        return self._ordered(sku)

    def usable(self, sku: str, day: date) -> int:
        return sum(lot.quantity for lot in self._ordered(sku) if lot.expires >= day)

    def pick(self, sku: str, quantity: int, day: date) -> list[tuple[str, int]]:
        if quantity < 1:
            raise StockError("quantity must be positive")
        if quantity > self.usable(sku, day):
            raise StockError(f"{sku}: only {self.usable(sku, day)} usable on {day}")
        if quantity > self._inventory.available(sku):
            raise StockError(f"{sku}: only {self._inventory.available(sku)} available")
        self._inventory.remove(sku, quantity)
        taken: list[tuple[str, int]] = []
        for lot in self._ordered(sku):
            if quantity == 0:
                break
            if lot.expires < day:
                continue
            units = min(quantity, lot.quantity)
            quantity -= units
            taken.append((lot.lot_id, units))
            if units == lot.quantity:
                del self._lots[lot.lot_id]
            else:
                self._lots[lot.lot_id] = replace(lot, quantity=lot.quantity - units)
        return taken

    def expire(self, day: date) -> list[Lot]:
        order = {lot_id: n for n, lot_id in enumerate(self._lots)}
        gone = [lot for lot in self._lots.values() if lot.expires < day]
        expired_units: dict[str, int] = {}
        for lot in gone:
            expired_units[lot.sku] = expired_units.get(lot.sku, 0) + lot.quantity
        for sku, units in expired_units.items():
            if units > self._inventory.available(sku):
                raise StockError(
                    f"{sku}: {units} expired units, {self._inventory.available(sku)} available"
                )
        for sku, units in expired_units.items():
            self._inventory.remove(sku, units)
        for lot in gone:
            del self._lots[lot.lot_id]
        return sorted(gone, key=lambda lot: (lot.expires, lot.sku, order[lot.lot_id]))
