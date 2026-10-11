"""First-in, first-out stock costing: cost of goods sold and stock value per month."""

from __future__ import annotations

import csv
import io
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from .dates import month_key
from .errors import ParseError, StockError

COST_HEADER = ("day", "sku", "kind", "quantity", "unit_cost")
_SKU = re.compile(r"[A-Z]{2,4}-[0-9]{3,5}")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_QUANTITY = re.compile(r"[0-9]+")
_COST = re.compile(r"([0-9]+)(?:\.([0-9]{2}))?")


@dataclass(frozen=True)
class CostMovement:
    day: date
    sku: str
    kind: str
    quantity: int
    unit_cost_cents: int | None = None


@dataclass(frozen=True)
class CostRow:
    month: str
    sku: str
    opening_qty: int
    opening_value: int
    in_qty: int
    in_value: int
    out_qty: int
    out_value: int
    closing_qty: int
    closing_value: int


def _cell(value: str) -> str:
    return value.strip(" \t")


def read_cost_movements(text: str) -> list[CostMovement]:
    rows = [row for row in csv.reader(io.StringIO(text)) if any(_cell(c) for c in row)]
    if not rows or tuple(_cell(c) for c in rows[0]) != COST_HEADER:
        raise ParseError(f"cost movement header must be {','.join(COST_HEADER)}")
    movements = []
    for row in rows[1:]:
        if len(row) != len(COST_HEADER):
            raise ParseError(f"expected {len(COST_HEADER)} fields: {row!r}")
        day_text, sku, kind, quantity, cost = (_cell(c) for c in row)
        if not _DAY.fullmatch(day_text):
            raise ParseError(f"not a date: {day_text!r}")
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            raise ParseError(f"not a date: {day_text!r}") from None
        if not _SKU.fullmatch(sku):
            raise ParseError(f"bad sku: {sku!r}")
        if kind not in ("in", "out"):
            raise ParseError(f"kind must be in or out: {kind!r}")
        if not _QUANTITY.fullmatch(quantity) or int(quantity) == 0:
            raise ParseError(f"bad quantity: {quantity!r}")
        unit_cost = None
        if kind == "in":
            match = _COST.fullmatch(cost)
            if not match:
                raise ParseError(f"bad unit cost: {cost!r}")
            unit_cost = int(match.group(1)) * 100 + int(match.group(2) or "0")
        elif cost:
            raise ParseError("an out movement has no unit cost")
        movements.append(CostMovement(day, sku, kind, int(quantity), unit_cost))
    return movements


def _months(first: date, last: date) -> list[str]:
    keys = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        keys.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return keys


def fifo_report(movements: Iterable[CostMovement]) -> list[CostRow]:
    ordered = sorted(movements, key=lambda m: m.day)
    if not ordered:
        return []
    layers: dict[str, deque[list[int]]] = {}
    totals: dict[tuple[str, str], list[int]] = {}
    for movement in ordered:
        sku = movement.sku
        queue = layers.setdefault(sku, deque())
        bucket = totals.setdefault((month_key(movement.day), sku), [0, 0, 0, 0])
        if movement.kind == "in":
            cost = movement.unit_cost_cents or 0
            queue.append([movement.quantity, cost])
            bucket[0] += movement.quantity
            bucket[1] += movement.quantity * cost
            continue
        have = sum(layer[0] for layer in queue)
        if movement.quantity > have:
            short = movement.quantity - have
            raise StockError(f"{movement.day.isoformat()} {sku}: short by {short}")
        need, value = movement.quantity, 0
        while need:
            layer = queue[0]
            take = min(need, layer[0])
            value += take * layer[1]
            layer[0] -= take
            need -= take
            if layer[0] == 0:
                queue.popleft()
        bucket[2] += movement.quantity
        bucket[3] += value
    first_month = {}
    for movement in ordered:
        first_month.setdefault(movement.sku, month_key(movement.day))
    rows = []
    state = {sku: (0, 0) for sku in first_month}
    for month in _months(ordered[0].day, ordered[-1].day):
        for sku in sorted(first_month):
            if first_month[sku] > month:
                continue
            opening_qty, opening_value = state[sku]
            in_qty, in_value, out_qty, out_value = totals.get((month, sku), [0, 0, 0, 0])
            closing = (opening_qty + in_qty - out_qty, opening_value + in_value - out_value)
            rows.append(
                CostRow(
                    month,
                    sku,
                    opening_qty,
                    opening_value,
                    in_qty,
                    in_value,
                    out_qty,
                    out_value,
                    closing[0],
                    closing[1],
                )
            )
            state[sku] = closing
    return rows


def average_cost(quantity: int, value: int) -> int | None:
    """``value / quantity`` rounded half to even to a whole cent; ``None`` for no quantity."""
    if quantity == 0:
        return None
    whole, rest = divmod(value, quantity)
    if rest * 2 > quantity or (rest * 2 == quantity and whole % 2 == 1):
        whole += 1
    return whole
