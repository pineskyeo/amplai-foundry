"""Exact integer rounding, rates in basis points, and invoices with per-line discount and tax."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ParseError
from .models import Order

MODES = ("floor", "ceiling", "down", "up", "half_up", "half_down", "half_even")
_PERCENT = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?%")
_POINTS = re.compile(r"([0-9]+)bp")


def round_div(numerator: int, denominator: int, mode: str) -> int:
    """``numerator / denominator`` rounded to an integer, computed exactly."""
    if mode not in MODES:
        raise ValueError(f"unknown rounding mode: {mode!r}")
    if denominator == 0:
        raise ZeroDivisionError("denominator is zero")
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    floor, rest = divmod(numerator, denominator)
    if rest == 0:
        return floor
    ceiling = floor + 1
    negative = numerator < 0
    if mode == "floor":
        return floor
    if mode == "ceiling":
        return ceiling
    if mode == "down":
        return ceiling if negative else floor
    if mode == "up":
        return floor if negative else ceiling
    twice = 2 * rest
    if twice < denominator:
        return floor
    if twice > denominator:
        return ceiling
    if mode == "half_up":
        return floor if negative else ceiling
    if mode == "half_down":
        return ceiling if negative else floor
    return floor if floor % 2 == 0 else ceiling


def parse_rate(text: str) -> int:
    """``'8.25%'`` or ``'825bp'`` -> 825 basis points (0 to 10000)."""
    cleaned = text.strip()
    match = _PERCENT.fullmatch(cleaned)
    if match:
        value = int(match.group(1)) * 100 + int((match.group(2) or "").ljust(2, "0"))
    else:
        match = _POINTS.fullmatch(cleaned)
        if not match:
            raise ParseError(f"not a rate: {text!r}")
        value = int(match.group(1))
    if value > 10000:
        raise ParseError(f"rate above 100%: {text!r}")
    return value


def format_rate(bp: int) -> str:
    if not 0 <= bp <= 10000:
        raise ValueError(f"rate out of range: {bp}")
    return f"{bp // 100}.{bp % 100:02d}%"


@dataclass(frozen=True)
class InvoiceLine:
    sku: str
    quantity: int
    amount: int
    discount: int
    net: int
    tax: int


@dataclass(frozen=True)
class Invoice:
    lines: tuple[InvoiceLine, ...]
    amount: int
    discount: int
    net: int
    tax: int
    total: int
    tax_on_total: int


def invoice(
    order: Order,
    *,
    tax_bp: int,
    discount_bp: int = 0,
    mode: str = "half_even",
    credit: bool = False,
) -> Invoice:
    if mode not in MODES:
        raise ValueError(f"unknown rounding mode: {mode!r}")
    if not 0 <= tax_bp <= 10000 or not 0 <= discount_bp <= 10000:
        raise ValueError("rates must be between 0 and 10000 basis points")
    lines = []
    for line in order.lines:
        amount = line.quantity * line.unit_price_cents
        if credit:
            amount = -amount
        discount = round_div(amount * discount_bp, 10000, mode)
        net = amount - discount
        lines.append(
            InvoiceLine(
                line.sku, line.quantity, amount, discount, net, round_div(net * tax_bp, 10000, mode)
            )
        )
    net = sum(line.net for line in lines)
    tax = sum(line.tax for line in lines)
    return Invoice(
        lines=tuple(lines),
        amount=sum(line.amount for line in lines),
        discount=sum(line.discount for line in lines),
        net=net,
        tax=tax,
        total=net + tax,
        tax_on_total=round_div(net * tax_bp, 10000, mode),
    )
