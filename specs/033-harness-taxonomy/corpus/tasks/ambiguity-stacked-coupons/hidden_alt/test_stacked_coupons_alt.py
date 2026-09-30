import os
import subprocess
import sys
from pathlib import Path

import pytest

from stockroom.coupons import COUPONS, apply_coupons
from stockroom.errors import ParseError

ROOT = Path(__file__).resolve().parents[3]


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


def price(*extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return run(
        "price", "--items", "data/items.csv", "--sku", "MUG-001", "--quantity", "8", *extra, env=env
    )


def test_a_single_coupon_takes_its_percentage_off_alt() -> None:
    assert COUPONS == {"WELCOME": 10, "BULK": 5, "VIP": 20}
    assert apply_coupons(10000, ["WELCOME"]) == 9000
    assert apply_coupons(10000, ["BULK"]) == 9500
    assert apply_coupons(10000, ["VIP"]) == 8000
    assert apply_coupons(10000, []) == 10000
    assert apply_coupons(10000, ["welcome"]) == 9000
    assert apply_coupons(10000, ["Bulk"]) == 9500
    assert apply_coupons(10000, ["VIP", "vip", "Vip"]) == 8000


def test_the_amount_after_the_coupons_is_rounded_half_up_alt() -> None:
    assert apply_coupons(12345, ["WELCOME"]) == 11111
    assert apply_coupons(101, ["BULK"]) == 96
    assert apply_coupons(103, ["WELCOME"]) == 93
    assert apply_coupons(5, ["WELCOME"]) == 5
    assert apply_coupons(0, ["VIP"]) == 0


def test_an_unknown_code_is_refused_before_anything_is_applied_alt() -> None:
    with pytest.raises(ParseError) as caught:
        apply_coupons(10000, ["WELCOME", "NOPE"])
    assert str(caught.value) == "unknown coupon 'NOPE'"
    with pytest.raises(ParseError) as caught:
        apply_coupons(10000, ["nope"])
    assert str(caught.value) == "unknown coupon 'nope'"


def test_price_shows_the_amount_after_a_coupon_and_taxes_that_amount_alt() -> None:
    done = price("--coupon", "WELCOME")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "8 x MUG-001 Blue Mug: $90.00",
        "tax: $0.00",
        "total: $90.00",
    ]
    taxed = price("--coupon", "vip", env={"STOCKROOM_TAX_BP": "1000"})
    assert taxed.stdout.splitlines() == [
        "8 x MUG-001 Blue Mug: $80.00",
        "tax: $8.00",
        "total: $88.00",
    ]


def test_price_refuses_unknown_coupons_and_a_coupon_with_a_discount_alt() -> None:
    unknown = price("--coupon", "NOPE")
    assert unknown.returncode == 1 and unknown.stdout == ""
    assert unknown.stderr == "stockroom: error: unknown coupon 'NOPE'\n"
    both = price("--coupon", "WELCOME", "--discount", "10")
    assert both.returncode == 1 and both.stdout == ""
    assert both.stderr == "stockroom: error: --coupon cannot be combined with --discount\n"


def test_several_coupons_all_take_effect_whatever_their_order_alt() -> None:
    for codes in (["WELCOME", "BULK"], ["BULK", "VIP"], ["VIP", "WELCOME", "BULK"]):
        both = apply_coupons(10000, codes)
        assert both == apply_coupons(10000, list(reversed(codes)))
        assert both == apply_coupons(10000, [*codes, codes[0].lower()])
        for code in codes:
            assert both < apply_coupons(10000, [code])


def test_coupons_apply_one_after_the_other() -> None:
    assert apply_coupons(10000, ["WELCOME", "BULK"]) == 8550
    assert apply_coupons(10000, ["VIP", "WELCOME"]) == 7200
    assert apply_coupons(10000, ["WELCOME", "BULK", "VIP"]) == 6840
    assert apply_coupons(10000, ["BULK", "VIP", "WELCOME"]) == 6840
    done = price("--coupon", "WELCOME", "--coupon", "BULK")
    assert done.stdout.splitlines()[0] == "8 x MUG-001 Blue Mug: $85.50"
