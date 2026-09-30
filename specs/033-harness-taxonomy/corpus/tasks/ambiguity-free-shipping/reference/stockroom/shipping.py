"""Shipping costs: a flat fee, free on big orders."""

from __future__ import annotations

from .models import Order
from .pricing import apply_discount, order_subtotal, order_total

SHIPPING_CENTS = 600
FREE_SHIPPING_CENTS = 5000


def shipping_fee(subtotal_cents: int, discounted_cents: int) -> int:
    """The shipping fee for an order of ``subtotal_cents`` that costs ``discounted_cents`` after
    its discount (before tax)."""
    worth = subtotal_cents
    return 0 if worth >= FREE_SHIPPING_CENTS else SHIPPING_CENTS


def total_with_shipping(order: Order, *, discount_percent: float = 0, tax_bp: int = 0) -> int:
    """What the customer pays: ``order_total`` plus shipping, which is not taxed or discounted."""
    subtotal = order_subtotal(order)
    fee = shipping_fee(subtotal, apply_discount(subtotal, discount_percent))
    return order_total(order, discount_percent=discount_percent, tax_bp=tax_bp) + fee
