"""Stock transfers between locations: units travel until they arrive or are called back."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .errors import ParseError, StockError, StockroomError
from .inventory import Inventory
from .models import check_sku

NETWORK_HEADER = ("location", "sku", "on_hand", "reserved")


@dataclass(frozen=True)
class Transfer:
    transfer_id: str
    sku: str
    quantity: int
    source: str
    target: str


Event = Transfer | tuple[str, str]


class Network:
    def __init__(self, locations: Mapping[str, Inventory]) -> None:
        self._locations = dict(locations)
        self._transfers: dict[str, Transfer] = {}
        self._states: dict[str, str] = {}

    def locations(self) -> list[str]:
        return sorted(self._locations)

    def dispatch(self, transfer: Transfer) -> bool:
        known = self._transfers.get(transfer.transfer_id)
        if known is not None:
            if known == transfer:
                return False
            raise StockroomError(f"transfer {transfer.transfer_id} is another transfer")
        if transfer.quantity < 1:
            raise StockError("quantity must be positive")
        for name in (transfer.source, transfer.target):
            if name not in self._locations:
                raise StockroomError(f"unknown location {name!r}")
        if transfer.source == transfer.target:
            raise StockroomError(f"transfer {transfer.transfer_id} goes nowhere")
        check_sku(transfer.sku)
        self._locations[transfer.source].issue(transfer.sku, transfer.quantity)
        self._transfers[transfer.transfer_id] = transfer
        self._states[transfer.transfer_id] = "in_transit"
        return True

    def _known(self, transfer_id: str) -> Transfer:
        try:
            return self._transfers[transfer_id]
        except KeyError:
            raise StockroomError(f"no transfer {transfer_id}") from None

    def receive(self, transfer_id: str) -> bool:
        transfer = self._known(transfer_id)
        state = self._states[transfer_id]
        if state == "received":
            return False
        if state == "cancelled":
            raise StockroomError(f"transfer {transfer_id} was cancelled")
        self._locations[transfer.target].receive(transfer.sku, transfer.quantity)
        self._states[transfer_id] = "received"
        return True

    def cancel(self, transfer_id: str) -> bool:
        transfer = self._known(transfer_id)
        state = self._states[transfer_id]
        if state == "cancelled":
            return False
        if state == "received":
            raise StockroomError(f"transfer {transfer_id} has arrived")
        self._locations[transfer.source].receive(transfer.sku, transfer.quantity)
        self._states[transfer_id] = "cancelled"
        return True

    def state(self, transfer_id: str) -> str:
        self._known(transfer_id)
        return self._states[transfer_id]

    def transfer_ids(self) -> list[str]:
        return sorted(self._transfers)

    def in_transit(self, sku: str) -> int:
        return sum(
            t.quantity
            for tid, t in self._transfers.items()
            if t.sku == sku and self._states[tid] == "in_transit"
        )

    def total(self, sku: str) -> int:
        return sum(inv.on_hand(sku) for inv in self._locations.values()) + self.in_transit(sku)

    def apply(self, events: Iterable[Event]) -> list[bool]:
        saved = (
            dict(self._transfers),
            dict(self._states),
            {
                name: (dict(inv._on_hand), dict(inv._reserved))
                for name, inv in self._locations.items()
            },
        )
        try:
            return [self._one(event) for event in events]
        except Exception:
            self._transfers, self._states = saved[0], saved[1]
            for name, (on_hand, reserved) in saved[2].items():
                self._locations[name]._on_hand = on_hand
                self._locations[name]._reserved = reserved
            raise

    def _one(self, event: Event) -> bool:
        if isinstance(event, Transfer):
            return self.dispatch(event)
        kind, transfer_id = event
        if kind == "receive":
            return self.receive(transfer_id)
        if kind == "cancel":
            return self.cancel(transfer_id)
        raise ParseError(f"unknown event {kind!r}")


def parse_events(text: str) -> list[Event]:
    events: list[Event] = []
    for number, line in enumerate(text.split("\n"), start=1):
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        kind = fields[0]
        if kind == "dispatch" and len(fields) == 6:
            _, tid, sku, quantity, source, target = fields
            if not (quantity.isascii() and quantity.isdigit()):
                raise ParseError(f"line {number}: bad quantity {quantity!r}")
            events.append(Transfer(tid, sku, int(quantity), source, target))
        elif kind in ("receive", "cancel") and len(fields) == 2:
            events.append((kind, fields[1]))
        else:
            raise ParseError(f"line {number}: not an event: {line.strip()!r}")
    return events


def read_network(text: str) -> dict[str, Inventory]:
    rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    if not rows or tuple(cell.strip() for cell in rows[0]) != NETWORK_HEADER:
        raise ParseError(f"network header must be {','.join(NETWORK_HEADER)}")
    levels: dict[str, list[tuple[str, int, int]]] = {}
    for row in rows[1:]:
        if len(row) != len(NETWORK_HEADER):
            raise ParseError(f"a network row has {len(NETWORK_HEADER)} fields")
        location, sku, on_hand, reserved = (cell.strip() for cell in row)
        if not location:
            raise ParseError("empty location")
        try:
            numbers = int(on_hand), int(reserved)
        except ValueError as exc:
            raise ParseError(f"{location} {sku}: levels are whole numbers") from exc
        if any(row_[0] == sku for row_ in levels.get(location, [])):
            raise ParseError(f"{location} {sku} is listed twice")
        levels.setdefault(location, []).append((sku, *numbers))
    return {name: Inventory.from_levels(rows_) for name, rows_ in levels.items()}
