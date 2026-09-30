"""Lint an order CSV file: report every problem instead of stopping at the first."""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from datetime import date

from .csvio import ORDER_HEADER
from .models import SKU, STATUSES, Item
from .money import format_money

_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_QUANTITY = re.compile(r"[0-9]+")
_PRICE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")


def _price_cents(text: str) -> int | None:
    found = _PRICE.fullmatch(text)
    if found is None:
        return None
    cents = int(found.group(2).ljust(2, "0")) if found.group(2) else 0
    return int(found.group(1)) * 100 + cents


def _real_date(text: str) -> bool:
    found = _DATE.fullmatch(text)
    if found is None:
        return False
    try:
        date(int(found.group(1)), int(found.group(2)), int(found.group(3)))
    except ValueError:
        return False
    return True


def lint_orders(text: str, items: Sequence[Item] | None = None) -> list[str]:
    """Every problem of the order file ``text``, as ``line N: ...`` messages (see the task text)."""
    if not text.strip():
        return ["line 1: empty file"]
    lines = text.splitlines()
    header = [cell.strip() for cell in next(csv.reader([lines[0]]), [])]
    if tuple(header) != ORDER_HEADER:
        return ["line 1: bad header"]
    catalogue = None if items is None else {item.sku: item.price_cents for item in items}
    problems: list[str] = []
    heads: dict[str, tuple[int, tuple[str, str, str]]] = {}
    seen: set[tuple[str, str]] = set()
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        fields = next(csv.reader([line]))
        if len(fields) != len(ORDER_HEADER):
            problems.append(f"line {number}: expected {len(ORDER_HEADER)} fields, found {len(fields)}")
            continue
        order_id, customer, placed, status, sku, quantity, price = (f.strip() for f in fields)
        if status not in STATUSES:
            problems.append(f"line {number}: unknown status {status!r}")
        if not _real_date(placed):
            problems.append(f"line {number}: bad date {placed!r}")
        sku_ok = SKU.fullmatch(sku) is not None
        if not sku_ok:
            problems.append(f"line {number}: bad sku {sku!r}")
        if not _QUANTITY.fullmatch(quantity) or int(quantity) < 1:
            problems.append(f"line {number}: bad quantity {quantity!r}")
        cents = _price_cents(price)
        if cents is None:
            problems.append(f"line {number}: bad price {price!r}")
        here = (customer, placed, status)
        if order_id not in heads:
            heads[order_id] = (number, here)
        else:
            first, there = heads[order_id]
            for name, a, b in zip(("customer", "placed", "status"), here, there, strict=True):
                if a != b:
                    problems.append(f"line {number}: {name} differs from line {first} for order {order_id!r}")
        if (order_id, sku) in seen:
            problems.append(f"line {number}: duplicate line for order {order_id!r} and sku {sku!r}")
        seen.add((order_id, sku))
        if catalogue is not None and sku_ok:
            if sku not in catalogue:
                problems.append(f"line {number}: unknown item {sku!r}")
            elif cents is not None and cents != catalogue[sku]:
                problems.append(
                    f"line {number}: price {format_money(cents, '')} differs from catalogue "
                    f"{format_money(catalogue[sku], '')}"
                )
    return problems
