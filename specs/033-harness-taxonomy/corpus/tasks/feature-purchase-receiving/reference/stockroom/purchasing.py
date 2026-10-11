"""Purchase orders: receipts, over-delivery tolerance and moving average cost."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date

from .dates import parse_date
from .errors import ParseError, StockError, StockroomError
from .inventory import Inventory
from .models import check_sku
from .money import parse_money

PO_HEADER = ("po_id", "supplier", "expected", "sku", "ordered", "unit_cost")
RECEIPT_HEADER = ("po_id", "sku", "quantity")


@dataclass(frozen=True)
class POLine:
    sku: str
    ordered: int
    unit_cost_cents: int


@dataclass(frozen=True)
class PurchaseOrder:
    po_id: str
    supplier: str
    expected: date
    lines: tuple[POLine, ...]


def _rows(text: str, header: tuple[str, ...], what: str) -> list[list[str]]:
    rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    if not rows or tuple(cell.strip() for cell in rows[0]) != header:
        raise ParseError(f"{what} header must be {','.join(header)}")
    out = []
    for row in rows[1:]:
        if len(row) != len(header):
            raise ParseError(f"a {what} row has {len(header)} fields")
        out.append([cell.strip() for cell in row])
    return out


def _whole(text: str, what: str) -> int:
    try:
        return int(text)
    except ValueError as exc:
        raise ParseError(f"{what} is not a whole number: {text!r}") from exc


def read_purchase_orders(text: str) -> list[PurchaseOrder]:
    heads: dict[str, tuple[str, date]] = {}
    lines: dict[str, list[POLine]] = {}
    for po_id, supplier, expected, sku, ordered, cost in _rows(text, PO_HEADER, "purchase order"):
        if not po_id:
            raise ParseError("empty purchase order id")
        head = (supplier, parse_date(expected))
        if heads.setdefault(po_id, head) != head:
            raise ParseError(f"{po_id}: rows disagree on supplier or expected date")
        line = POLine(sku, _whole(ordered, "ordered"), parse_money(cost))
        lines.setdefault(po_id, []).append(line)
    return [PurchaseOrder(po_id, *heads[po_id], tuple(lines[po_id])) for po_id in heads]


def read_receipts(text: str) -> list[tuple[str, str, int]]:
    return [
        (po_id, sku, _whole(quantity, "quantity"))
        for po_id, sku, quantity in _rows(text, RECEIPT_HEADER, "receipt")
    ]


def _half_even(numerator: int, denominator: int) -> int:
    quotient, rest = divmod(numerator, denominator)
    if rest * 2 > denominator or (rest * 2 == denominator and quotient % 2 == 1):
        quotient += 1
    return quotient


class Receiving:
    def __init__(self, inventory: Inventory, *, tolerance_bp: int = 1000) -> None:
        if tolerance_bp < 0:
            raise ValueError("tolerance_bp must not be negative")
        self._inventory = inventory
        self._tolerance = tolerance_bp
        self._orders: dict[str, PurchaseOrder] = {}
        self._received: dict[tuple[str, str], int] = {}
        self._closed: set[str] = set()
        self._average: dict[str, int] = {}

    def add(self, po: PurchaseOrder) -> None:
        if po.po_id in self._orders:
            raise StockroomError(f"purchase order {po.po_id} exists")
        if not po.lines:
            raise ParseError(f"{po.po_id}: no lines")
        seen: set[str] = set()
        for line in po.lines:
            check_sku(line.sku)
            if line.ordered < 1 or line.unit_cost_cents < 0:
                raise ParseError(f"{po.po_id} {line.sku}: bad quantity or cost")
            if line.sku in seen:
                raise ParseError(f"{po.po_id}: {line.sku} on two lines")
            seen.add(line.sku)
        self._orders[po.po_id] = po

    def _po(self, po_id: str) -> PurchaseOrder:
        try:
            return self._orders[po_id]
        except KeyError:
            raise StockroomError(f"no purchase order {po_id}") from None

    def _line(self, po: PurchaseOrder, sku: str) -> POLine:
        for line in po.lines:
            if line.sku == sku:
                return line
        raise StockroomError(f"{po.po_id} has no {sku}")

    def receive(self, po_id: str, sku: str, quantity: int) -> int:
        po = self._po(po_id)
        if po_id in self._closed:
            raise StockroomError(f"{po_id} is closed")
        line = self._line(po, sku)
        if quantity < 1:
            raise StockError("quantity must be positive")
        done = self._received.get((po_id, sku), 0)
        limit = line.ordered + line.ordered * self._tolerance // 10000
        if done + quantity > limit:
            raise StockError(f"{po_id} {sku}: at most {limit - done} more units")
        before = self._inventory.on_hand(sku)
        if sku not in self._average:
            self._average[sku] = line.unit_cost_cents
        else:
            value = self._average[sku] * before + line.unit_cost_cents * quantity
            self._average[sku] = _half_even(value, before + quantity)
        self._inventory.receive(sku, quantity)
        self._received[(po_id, sku)] = done + quantity
        return max(0, line.ordered - done - quantity)

    def status(self, po_id: str) -> str:
        po = self._po(po_id)
        if po_id in self._closed:
            return "closed"
        got = [self._received.get((po_id, line.sku), 0) for line in po.lines]
        if all(g >= line.ordered for g, line in zip(got, po.lines, strict=True)):
            return "received"
        return "partial" if any(got) else "open"

    def close(self, po_id: str) -> None:
        state = self.status(po_id)
        if state == "received":
            raise StockroomError(f"{po_id} is fully received")
        self._closed.add(po_id)

    def received(self, po_id: str, sku: str) -> int:
        self._line(self._po(po_id), sku)
        return self._received.get((po_id, sku), 0)

    def outstanding(self, po_id: str) -> dict[str, int]:
        po = self._po(po_id)
        if po_id in self._closed:
            return {}
        out = {}
        for line in po.lines:
            missing = line.ordered - self._received.get((po_id, line.sku), 0)
            if missing > 0:
                out[line.sku] = missing
        return out

    def average_cost(self, sku: str) -> int | None:
        return self._average.get(sku)

    def overdue(self, as_of: date) -> list[str]:
        late = [
            po
            for po in self._orders.values()
            if po.expected < as_of and self.status(po.po_id) in ("open", "partial")
        ]
        return [po.po_id for po in sorted(late, key=lambda po: (po.expected, po.po_id))]
