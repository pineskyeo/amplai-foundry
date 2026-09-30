import os
from datetime import date
from fractions import Fraction
from math import floor
from pathlib import Path

import pytest

from stockroom.cli import main
from stockroom.models import Order, OrderLine
from stockroom.pricing import apply_discount, order_total


@pytest.fixture(autouse=True)
def _clean_stockroom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("STOCKROOM_"):
            monkeypatch.delenv(name)


PERCENTS = [0, 0.5, 1, 2.5, 5, 7.5, 10, 12.5, 15, 20, 25, 33, 37.5, 50, 62.5, 75, 99, 100]
ITEM_HEADER = "sku,name,price,tags,reorder_level\n"


def _expected(cents: int, percent: float) -> int:
    discount = floor(Fraction(cents) * Fraction(percent) / 100 + Fraction(1, 2))
    return cents - discount


def test_a_half_cent_discount_is_rounded_up() -> None:
    assert apply_discount(5, 10) == 4
    assert apply_discount(25, 10) == 22
    assert apply_discount(15, 10) == 13
    assert apply_discount(35, 10) == 31
    assert apply_discount(5, 50) == 2
    assert apply_discount(1, 50) == 0
    assert apply_discount(3, 50) == 1
    assert apply_discount(1, 10) == 1
    assert apply_discount(4, 10) == 4


def test_ordinary_discounts_round_to_the_nearest_cent() -> None:
    assert apply_discount(999, 15) == 849
    assert apply_discount(1000, 15) == 850
    assert apply_discount(1999, 15) == 1699
    assert apply_discount(101, 33) == 68
    assert apply_discount(199, 12.5) == 174
    assert apply_discount(1234, 12.5) == 1080
    assert apply_discount(1236, 12.5) == 1081
    assert apply_discount(999, 99.5) == 5


def test_every_small_amount_with_common_percentages_matches_exact_half_up_rounding() -> None:
    for percent in PERCENTS:
        for cents in range(0, 1300):
            assert apply_discount(cents, percent) == _expected(cents, percent), (cents, percent)
    for cents in (10**6 + 5, 123456789, 99999999995):
        for percent in PERCENTS:
            assert apply_discount(cents, percent) == _expected(cents, percent), (cents, percent)
    for cents in (10**17 + 5, 123456789012345675, 99999999999999995, 10**18 + 50):
        for percent in (10, 15, 50, 25, 12.5, 33, 7.5, 62.5, 99):
            assert apply_discount(cents, percent) == _expected(cents, percent), (cents, percent)


def test_no_discount_and_full_discount() -> None:
    for cents in (0, 1, 5, 999, 123456):
        assert apply_discount(cents, 0) == cents
        assert apply_discount(cents, 100) == 0


def test_a_percentage_outside_zero_to_one_hundred_is_refused() -> None:
    for percent in (-0.01, -5, 100.01, 120):
        with pytest.raises(ValueError):
            apply_discount(1000, percent)


def test_order_totals_use_the_rounded_discount() -> None:
    order = Order("A-1", "Hana Kim", date(2024, 1, 8), (OrderLine("MUG-001", 1, 999),))
    assert order_total(order, discount_percent=15) == 849
    assert order_total(order, discount_percent=15, tax_bp=825) == 849 + 70
    small = Order("A-2", "Hana Kim", date(2024, 1, 8), (OrderLine("MUG-001", 1, 5),))
    assert order_total(small, discount_percent=10) == 4
    three = Order("A-3", "Hana Kim", date(2024, 1, 8), (OrderLine("MUG-001", 3, 999),))
    assert order_total(three, discount_percent=15) == 2547


def test_the_price_command_uses_the_rounded_discount(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "items.csv"
    path.write_text(ITEM_HEADER + "MUG-001,Blue Mug,9.99,,0\nPEN-100,Pen,0.05,,0\n")
    base = ["price", "--items", str(path)]
    assert main([*base, "--sku", "MUG-001", "--quantity", "1", "--discount", "15"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "1 x MUG-001 Blue Mug: $8.49",
        "tax: $0.00",
        "total: $8.49",
    ]
    assert main([*base, "--sku", "MUG-001", "--quantity", "3", "--discount", "15"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "3 x MUG-001 Blue Mug: $25.47"
    assert main([*base, "--sku", "PEN-100", "--quantity", "1", "--discount", "10"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "1 x PEN-100 Pen: $0.04",
        "tax: $0.00",
        "total: $0.04",
    ]
