"""Price lists: dated unit prices per SKU, general or per customer; repricing open orders."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from .dates import parse_date
from .errors import ParseError
from .models import Item, Order, check_sku
from .money import parse_money

PRICE_HEADER = ("sku", "customer", "start", "end", "price")


@dataclass(frozen=True)
class PriceRule:
    sku: str
    customer: str
    start: date
    end: date | None
    unit_price_cents: int


@dataclass(frozen=True)
class Repricing:
    order_id: str
    line: int
    sku: str
    quantity: int
    old_cents: int
    new_cents: int


def _folded(name: str) -> str:
    return " ".join(name.split()).casefold()


def read_price_rules(text: str) -> list[PriceRule]:
    rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    if not rows or tuple(cell.strip() for cell in rows[0]) != PRICE_HEADER:
        raise ParseError(f"price list header must be {','.join(PRICE_HEADER)}")
    rules = []
    for row in rows[1:]:
        if len(row) != len(PRICE_HEADER):
            raise ParseError(f"a price rule has {len(PRICE_HEADER)} fields")
        sku, customer, start, end, price = (cell.strip() for cell in row)
        first = parse_date(start)
        last = parse_date(end) if end else None
        if last is not None and last <= first:
            raise ParseError(f"{sku}: the end {end} is not after the start {start}")
        cents = parse_money(price)
        if cents < 0:
            raise ParseError(f"{sku}: negative price")
        rules.append(PriceRule(check_sku(sku), customer, first, last, cents))
    return rules


def best_rule(rules: Sequence[PriceRule], sku: str, customer: str, day: date) -> PriceRule | None:
    who = _folded(customer)
    found = []
    for index, rule in enumerate(rules):
        general = not rule.customer.strip()
        if rule.sku != sku or not (general or _folded(rule.customer) == who):
            continue
        if not (rule.start <= day and (rule.end is None or day < rule.end)):
            continue
        key = (
            general,
            -rule.start.toordinal(),
            rule.end is None,
            rule.end.toordinal() if rule.end is not None else 0,
            rule.unit_price_cents,
            index,
        )
        found.append((key, rule))
    return min(found, key=lambda pair: pair[0])[1] if found else None


def price_for(
    rules: Sequence[PriceRule], items: Iterable[Item], sku: str, customer: str, day: date
) -> int:
    rule = best_rule(rules, sku, customer, day)
    if rule is not None:
        return rule.unit_price_cents
    for item in items:
        if item.sku == sku:
            return item.price_cents
    raise ParseError(f"no price for {sku}")


def reprice(
    orders: Iterable[Order], rules: Sequence[PriceRule], items: Iterable[Item]
) -> list[Repricing]:
    catalogue = list(items)
    changes = []
    for order in orders:
        if order.status != "open":
            continue
        for number, line in enumerate(order.lines, start=1):
            new = price_for(rules, catalogue, line.sku, order.customer, order.placed)
            if new != line.unit_price_cents:
                changes.append(
                    Repricing(
                        order.order_id, number, line.sku, line.quantity, line.unit_price_cents, new
                    )
                )
    return changes
