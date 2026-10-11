"""Quote requests: one ``SKU QUANTITY`` request per line, priced against the catalogue."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .models import Item

_QUANTITY = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class QuoteLine:
    number: int
    item: Item
    quantity: int

    @property
    def amount_cents(self) -> int:
        return self.item.price_cents * self.quantity


def request_lines(text: str) -> list[tuple[int, str]]:
    """``(line number, line)`` of the request lines: split at ``\\n`` (a ``\\r`` before it is
    dropped); blank lines and lines starting with ``#`` are skipped."""
    pieces = text.split("\n")
    if pieces and pieces[-1] == "":
        pieces.pop()
    found = []
    for number, line in enumerate(pieces, start=1):
        if line.endswith("\r"):
            line = line[:-1]
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            found.append((number, line))
    return found


def price_requests(
    text: str, items: Iterable[Item]
) -> tuple[list[QuoteLine], list[tuple[int, str]]]:
    """The priced lines and the ``(line number, reason)`` of the lines that fail."""
    catalogue = {item.sku: item for item in items}
    priced: list[QuoteLine] = []
    failed: list[tuple[int, str]] = []
    for number, line in request_lines(text):
        fields = line.split()
        if len(fields) != 2:
            failed.append((number, f"expected SKU and QUANTITY, got {len(fields)} fields"))
            continue
        sku, quantity = fields
        if not _QUANTITY.fullmatch(quantity) or int(quantity) < 1:
            failed.append((number, f"bad quantity {quantity!r}"))
            continue
        if sku not in catalogue:
            failed.append((number, f"no item {sku}"))
            continue
        priced.append(QuoteLine(number, catalogue[sku], int(quantity)))
    return priced, failed
