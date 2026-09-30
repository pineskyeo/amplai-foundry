from datetime import date

import pytest

from stockroom.models import Order, OrderLine
from stockroom.pricing import (
    Tier,
    apply_discount,
    line_total,
    order_subtotal,
    order_total,
    tax,
    unit_price,
)

TIERS = [Tier(10, 900), Tier(50, 800)]


def test_unit_price_tiers() -> None:
    assert unit_price(1000, 1, TIERS) == 1000
    assert unit_price(1000, 20, TIERS) == 900
    assert unit_price(1000, 100, TIERS) == 800
    assert unit_price(1000, 5) == 1000


def test_apply_discount() -> None:
    assert apply_discount(1000, 10) == 900
    assert apply_discount(2000, 25) == 1500
    assert apply_discount(1000, 0) == 1000
    assert apply_discount(1000, 100) == 0
    with pytest.raises(ValueError):
        apply_discount(1000, 120)


def test_tax() -> None:
    assert tax(10000, 825) == 825
    assert tax(0, 825) == 0
    with pytest.raises(ValueError):
        tax(100, -1)


def test_order_totals() -> None:
    order = Order(
        "A-1",
        "Hana Kim",
        date(2024, 1, 8),
        (OrderLine("MUG-001", 2, 1250), OrderLine("TEA-010", 4, 500)),
    )
    assert line_total(order.lines[0]) == 2500
    assert order_subtotal(order) == 4500
    assert order_total(order) == 4500
    assert order_total(order, discount_percent=20, tax_bp=1000) == 3960
