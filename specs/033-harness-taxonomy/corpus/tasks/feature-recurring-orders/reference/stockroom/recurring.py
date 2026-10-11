"""Recurring orders: templates that place the same order on a schedule."""

from __future__ import annotations

import csv
import io
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from .dates import parse_date
from .errors import ParseError, StockroomError
from .models import Order, OrderLine
from .money import parse_money
from .orders import OrderBook

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TEMPLATE_HEADER = (
    "template_id",
    "customer",
    "start",
    "end",
    "every",
    "weekdays",
    "sku",
    "quantity",
    "unit_price",
)


@dataclass(frozen=True)
class Template:
    template_id: str
    customer: str
    start: date
    end: date | None
    every_days: int | None
    weekdays: tuple[int, ...]
    lines: tuple[OrderLine, ...]

    def __post_init__(self) -> None:
        if not self.template_id.strip():
            raise ParseError("empty template id")
        if (self.every_days is None) == (not self.weekdays):
            raise ParseError(f"{self.template_id}: give either every_days or weekdays")
        if self.every_days is not None and self.every_days < 1:
            raise ParseError(f"{self.template_id}: every_days must be at least 1")
        if any(not 0 <= d <= 6 for d in self.weekdays) or list(self.weekdays) != sorted(
            set(self.weekdays)
        ):
            raise ParseError(f"{self.template_id}: weekdays are distinct 0-6 in ascending order")
        if not self.lines:
            raise ParseError(f"{self.template_id}: no lines")
        if self.end is not None and self.end <= self.start:
            raise ParseError(f"{self.template_id}: end must be after start")


def occurrences(template: Template, until: date) -> list[tuple[int, date]]:
    last = until if template.end is None else min(until, template.end - timedelta(days=1))
    found: list[tuple[int, date]] = []
    if template.every_days is not None:
        day, number = template.start, 1
        while day <= last:
            found.append((number, day))
            day += timedelta(days=template.every_days)
            number += 1
        return found
    day, number = template.start, 0
    while day <= last:
        if day.weekday() in template.weekdays:
            number += 1
            found.append((number, day))
        day += timedelta(days=1)
    return found


def generate(
    templates: Sequence[Template],
    book: OrderBook,
    until: date,
    *,
    skip: Collection[date] = (),
) -> list[Order]:
    ids = [t.template_id for t in templates]
    if len(set(ids)) != len(ids):
        raise StockroomError("duplicate template id")
    known = {order.order_id for order in book}
    new: list[tuple[date, int, Order]] = []
    for position, template in enumerate(templates):
        for number, day in occurrences(template, until):
            order_id = f"{template.template_id}-{number}"
            if day in skip or order_id in known:
                continue
            order = Order(order_id, template.customer, day, template.lines, "open")
            new.append((day, position, order))
    new.sort(key=lambda entry: (entry[0], entry[1]))
    orders = [order for _, _, order in new]
    for order in orders:
        book.add(order)
    return orders


def read_templates(text: str) -> list[Template]:
    rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    if not rows or tuple(cell.strip() for cell in rows[0]) != TEMPLATE_HEADER:
        raise ParseError(f"template header must be {','.join(TEMPLATE_HEADER)}")
    heads: dict[str, tuple[str, date, date | None, int | None, tuple[int, ...]]] = {}
    lines: dict[str, list[OrderLine]] = {}
    for row in rows[1:]:
        if len(row) != len(TEMPLATE_HEADER):
            raise ParseError(f"a template row has {len(TEMPLATE_HEADER)} fields")
        tid, customer, start, end, every, weekdays, sku, quantity, price = (c.strip() for c in row)
        if every and not (every.isascii() and every.isdigit()):
            raise ParseError(f"{tid}: every is a whole number of days")
        names = weekdays.split()
        if any(name not in WEEKDAYS for name in names) or len(set(names)) != len(names):
            raise ParseError(f"{tid}: weekdays are distinct names among {' '.join(WEEKDAYS)}")
        head = (
            customer,
            parse_date(start),
            parse_date(end) if end else None,
            int(every) if every else None,
            tuple(sorted(WEEKDAYS.index(name) for name in names)),
        )
        if heads.setdefault(tid, head) != head:
            raise ParseError(f"{tid}: rows disagree")
        try:
            units = int(quantity)
        except ValueError as exc:
            raise ParseError(f"{tid}: quantity is not a whole number") from exc
        lines.setdefault(tid, []).append(OrderLine(sku, units, parse_money(price)))
    return [Template(tid, *heads[tid], tuple(lines[tid])) for tid in heads]
