"""Stock movements: a plain-text ledger that can be replayed against an inventory.

One movement per line: ``DAY KIND SKU QUANTITY [NOTE]``, for example
``2024-03-02 reserve MUG-001 3 web order 77``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from .dates import month_key, parse_date
from .errors import ParseError, StockError
from .inventory import Inventory
from .models import check_sku

KINDS = ("receive", "reserve", "release", "ship", "count")
_DIGITS = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class Movement:
    day: date
    kind: str
    sku: str
    quantity: int
    note: str = ""


def parse_ledger(text: str) -> list[Movement]:
    movements: list[Movement] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 4)
        if len(parts) < 4:
            raise ParseError(f"line {number}: expected day, kind, sku and quantity")
        day_text, kind, sku, quantity_text = parts[:4]
        note = parts[4].strip() if len(parts) == 5 else ""
        try:
            day = parse_date(day_text)
            if kind not in KINDS:
                raise ParseError(f"unknown kind {kind!r}")
            check_sku(sku)
            if not _DIGITS.fullmatch(quantity_text):
                raise ParseError(f"bad quantity {quantity_text!r}")
            quantity = int(quantity_text)
            if quantity == 0 and kind != "count":
                raise ParseError("quantity must be positive")
        except ParseError as exc:
            raise ParseError(f"line {number}: {exc}") from exc
        if movements and day < movements[-1].day:
            raise ParseError(f"line {number}: dated before the previous movement")
        movements.append(Movement(day, kind, sku, quantity, note))
    return movements


def _apply_one(inventory: Inventory, movement: Movement) -> None:
    try:
        if movement.kind == "count":
            inventory.count(movement.sku, movement.quantity)
        else:
            getattr(inventory, movement.kind)(movement.sku, movement.quantity)
    except StockError as exc:
        raise StockError(f"{movement.day.isoformat()} {movement.kind} {movement.sku}: {exc}") from exc


def apply_movements(inventory: Inventory, movements: Iterable[Movement]) -> None:
    """Apply ``movements`` in order; on any failure ``inventory`` is left exactly as it was."""
    items = list(movements)
    trial = inventory.copy()
    for movement in items:
        _apply_one(trial, movement)
    for movement in items:
        _apply_one(inventory, movement)


def month_end_snapshots(
    inventory: Inventory, movements: Iterable[Movement]
) -> dict[str, dict[str, tuple[int, int]]]:
    """``{"YYYY-MM": inventory.snapshot()}`` after the last movement (in list order) of each month
    that has movements, sorted by month. ``inventory`` itself is not changed."""
    work = inventory.copy()
    result: dict[str, dict[str, tuple[int, int]]] = {}
    for movement in movements:
        _apply_one(work, movement)
        result[month_key(movement.day)] = work.snapshot()
    return dict(sorted(result.items()))
