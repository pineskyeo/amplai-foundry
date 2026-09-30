import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from stockroom.models import Order, OrderLine
from stockroom.shipping import FREE_SHIPPING_CENTS, SHIPPING_CENTS, total_with_shipping

ROOT = Path(__file__).resolve().parents[3]


def _order(*lines: tuple[int, int]) -> Order:
    return Order(
        "S-1",
        "Hana Kim",
        date(2024, 6, 3),
        tuple(OrderLine("MUG-001", quantity, price) for quantity, price in lines),
        "open",
    )


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def price(quantity: str, *extra: str, env: dict[str, str] | None = None) -> list[str]:
    done = run(
        "price", "--items", "data/items.csv", "--sku", "MUG-001", "--quantity", quantity, *extra,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


def test_shipping_is_a_flat_fee_that_is_free_from_fifty_dollars() -> None:
    assert (SHIPPING_CENTS, FREE_SHIPPING_CENTS) == (600, 5000)
    assert total_with_shipping(_order((1, 100))) == 700
    assert total_with_shipping(_order((1, 4999))) == 4999 + 600
    assert total_with_shipping(_order((1, 5000))) == 5000
    assert total_with_shipping(_order((1, 5001))) == 5001
    assert total_with_shipping(_order((2, 2000), (3, 500))) == 5500
    assert total_with_shipping(_order((2, 1000), (3, 500))) == 3500 + 600
    assert total_with_shipping(_order()) == 600


def test_shipping_is_neither_taxed_nor_discounted() -> None:
    assert total_with_shipping(_order((1, 4000)), tax_bp=1000) == 4000 + 400 + 600
    assert total_with_shipping(_order((1, 4000)), tax_bp=825) == 4000 + 330 + 600
    assert total_with_shipping(_order((1, 7000)), discount_percent=10, tax_bp=1000) == 6930
    assert total_with_shipping(_order((1, 3000)), discount_percent=10) == 2700 + 600
    assert total_with_shipping(_order((1, 3000)), discount_percent=100) == 600
    assert total_with_shipping(_order((1, 3000)), discount_percent=50, tax_bp=1000) == 1650 + 600


def test_a_discount_that_keeps_the_order_clearly_above_or_below_the_line() -> None:
    assert total_with_shipping(_order((1, 8000)), discount_percent=25) == 6000
    assert total_with_shipping(_order((1, 10000)), discount_percent=10) == 9000
    assert total_with_shipping(_order((1, 2000)), discount_percent=50) == 1000 + 600
    assert total_with_shipping(_order((2, 1500)), discount_percent=20) == 2400 + 600


def test_price_with_shipping_adds_a_line_and_keeps_the_rest() -> None:
    assert price("3", "--shipping") == [
        "3 x MUG-001 Blue Mug: $37.50",
        "tax: $0.00",
        "shipping: $6.00",
        "total: $43.50",
    ]
    assert price("4", "--shipping") == [
        "4 x MUG-001 Blue Mug: $50.00",
        "tax: $0.00",
        "shipping: $0.00",
        "total: $50.00",
    ]
    assert price("3", "--shipping", env={"STOCKROOM_TAX_BP": "1000"}) == [
        "3 x MUG-001 Blue Mug: $37.50",
        "tax: $3.75",
        "shipping: $6.00",
        "total: $47.25",
    ]
    assert price("8", "--shipping", "--discount", "10") == [
        "8 x MUG-001 Blue Mug: $90.00",
        "tax: $0.00",
        "shipping: $0.00",
        "total: $90.00",
    ]
    assert price("2", "--shipping", "--discount", "20")[2:] == ["shipping: $6.00", "total: $26.00"]
    assert price("3") == ["3 x MUG-001 Blue Mug: $37.50", "tax: $0.00", "total: $37.50"]


def test_the_order_is_worth_what_it_costs_before_its_discount() -> None:
    assert total_with_shipping(_order((1, 6000)), discount_percent=20) == 4800
    assert total_with_shipping(_order((1, 5000)), discount_percent=10) == 4500
    assert total_with_shipping(_order((2, 3000)), discount_percent=50, tax_bp=1000) == 3300
    assert price("8", "--shipping", "--discount", "60") == [
        "8 x MUG-001 Blue Mug: $40.00",
        "tax: $0.00",
        "shipping: $0.00",
        "total: $40.00",
    ]
