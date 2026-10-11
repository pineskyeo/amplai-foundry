"""Accounts receivable: payments applied to the oldest open orders, and the balance by age."""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from .errors import ParseError
from .models import Order
from .pricing import order_subtotal

PAYMENT_HEADER = ("customer", "paid", "amount")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AMOUNT = re.compile(r"([0-9]+)(?:\.([0-9]{2}))?")


@dataclass(frozen=True)
class Payment:
    customer: str
    day: date
    amount_cents: int


@dataclass(frozen=True)
class AgingRow:
    customer: str
    current: int
    days_1_30: int
    days_31_60: int
    days_61_90: int
    over_90: int
    total: int
    credit: int


def customer_key(name: str) -> str:
    """NFKC, whitespace runs to one space and trimmed, then case-folded."""
    return " ".join(unicodedata.normalize("NFKC", name).split()).casefold()


def read_payments(text: str) -> list[Payment]:
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows or tuple(c.strip() for c in rows[0]) != PAYMENT_HEADER:
        raise ParseError(f"payment header must be {','.join(PAYMENT_HEADER)}")
    payments = []
    for row in rows[1:]:
        if len(row) != 3:
            raise ParseError(f"expected 3 fields: {row!r}")
        customer, paid, amount = (c.strip() for c in row)
        if not customer:
            raise ParseError("empty customer")
        if not _DAY.fullmatch(paid):
            raise ParseError(f"not a date: {paid!r}")
        try:
            day = date.fromisoformat(paid)
        except ValueError:
            raise ParseError(f"not a date: {paid!r}") from None
        match = _AMOUNT.fullmatch(amount)
        if not match:
            raise ParseError(f"not an amount: {amount!r}")
        cents = int(match.group(1)) * 100 + int(match.group(2) or "0")
        if cents == 0:
            raise ParseError("a payment must be more than zero")
        payments.append(Payment(customer, day, cents))
    return payments


def _bucket(days: int) -> int:
    if days <= 0:
        return 0
    if days <= 30:
        return 1
    if days <= 60:
        return 2
    if days <= 90:
        return 3
    return 4


def age_receivables(
    orders: Iterable[Order],
    payments: Iterable[Payment],
    *,
    as_of: date,
    terms_days: int = 30,
) -> list[AgingRow]:
    if terms_days < 0:
        raise ValueError("terms must not be negative")
    names: dict[str, str] = {}
    # events: (day, kind 0 = invoice / 1 = payment, sequence, key, amount)
    events: list[tuple[date, int, int, str, int, date]] = []
    sequence = 0
    for order in orders:
        key = customer_key(order.customer)
        names.setdefault(key, order.customer)
        if order.status == "cancelled" or order.placed > as_of:
            continue
        events.append((order.placed, 0, sequence, key, order_subtotal(order), order.placed))
        sequence += 1
    for payment in payments:
        key = customer_key(payment.customer)
        if key not in names:
            names[key] = payment.customer
        if payment.day > as_of:
            continue
        events.append((payment.day, 1, sequence, key, payment.amount_cents, payment.day))
        sequence += 1
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    open_items: dict[str, list[list]] = {}
    credit: dict[str, int] = {}
    for _day, kind, _, key, amount, placed in events:
        invoices = open_items.setdefault(key, [])
        if kind == 0:
            used = min(credit.get(key, 0), amount)
            credit[key] = credit.get(key, 0) - used
            invoices.append([placed, amount - used])
            continue
        left = amount
        for invoice in invoices:
            if not left:
                break
            take = min(left, invoice[1])
            invoice[1] -= take
            left -= take
        credit[key] = credit.get(key, 0) + left
    rows = []
    for key in set(open_items) | set(credit):
        buckets = [0, 0, 0, 0, 0]
        for placed, balance in open_items.get(key, []):
            if balance:
                due = placed + timedelta(days=terms_days)
                buckets[_bucket((as_of - due).days)] += balance
        total = sum(buckets)
        if total or credit.get(key, 0):
            rows.append((key, AgingRow(names[key], *buckets, total, credit.get(key, 0))))
    rows.sort(key=lambda pair: (-pair[1].total, pair[0]))
    return [row for _, row in rows]
