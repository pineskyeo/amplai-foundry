import os
import subprocess
import sys
from pathlib import Path

import pytest

from stockroom.errors import ParseError
from stockroom.vouchers import VOUCHERS, Redemption, redeem

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


def price(quantity: str, *extra: str, env: dict[str, str] | None = None) -> list[str]:
    done = run(
        "price", "--items", "data/items.csv", "--sku", "MUG-001", "--quantity", quantity, *extra,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    return done.stdout.splitlines()


def test_each_voucher_takes_its_fixed_amount_and_codes_ignore_case() -> None:
    assert VOUCHERS == {"FIVER": (500, 2000), "TENNER": (1000, 5000), "LASTCALL": (300, 0)}
    assert redeem(6000, ["FIVER"]) == Redemption((("FIVER", 500),), 5500)
    assert redeem(6000, ["TENNER"]) == Redemption((("TENNER", 1000),), 5000)
    assert redeem(6000, ["LASTCALL"]) == Redemption((("LASTCALL", 300),), 5700)
    assert redeem(6000, ["fiver"]) == Redemption((("FIVER", 500),), 5500)
    assert redeem(6000, ["LastCall"]) == Redemption((("LASTCALL", 300),), 5700)
    assert redeem(6000, []) == Redemption((), 6000)


def test_a_voucher_needs_its_minimum_spend_and_is_otherwise_skipped() -> None:
    assert redeem(1999, ["FIVER"]) == Redemption((), 1999)
    assert redeem(2000, ["FIVER"]) == Redemption((("FIVER", 500),), 1500)
    assert redeem(4999, ["TENNER"]) == Redemption((), 4999)
    assert redeem(5000, ["TENNER"]) == Redemption((("TENNER", 1000),), 4000)
    assert redeem(4999, ["TENNER", "FIVER"]) == Redemption((("FIVER", 500),), 4499)
    assert redeem(100, ["FIVER", "TENNER"]) == Redemption((), 100)


def test_vouchers_apply_in_table_order_and_fiver_gives_way_to_tenner() -> None:
    assert redeem(6000, ["LASTCALL", "TENNER"]) == Redemption(
        (("TENNER", 1000), ("LASTCALL", 300)), 4700
    )
    assert redeem(6000, ["FIVER", "TENNER", "LASTCALL"]) == Redemption(
        (("TENNER", 1000), ("LASTCALL", 300)), 4700
    )
    assert redeem(6000, ["TENNER", "FIVER"]) == Redemption((("TENNER", 1000),), 5000)
    assert redeem(2500, ["LASTCALL", "FIVER", "TENNER"]) == Redemption(
        (("FIVER", 500), ("LASTCALL", 300)), 1700
    )
    assert redeem(2500, ["fiver", "FIVER", "Fiver", "lastcall", "LASTCALL"]) == Redemption(
        (("FIVER", 500), ("LASTCALL", 300)), 1700
    )


def test_a_voucher_never_takes_more_than_is_left() -> None:
    assert redeem(200, ["LASTCALL"]) == Redemption((("LASTCALL", 200),), 0)
    assert redeem(300, ["LASTCALL"]) == Redemption((("LASTCALL", 300),), 0)
    assert redeem(0, ["LASTCALL"]) == Redemption((("LASTCALL", 0),), 0)
    assert redeem(0, ["FIVER", "TENNER"]) == Redemption((), 0)
    assert redeem(0, []) == Redemption((), 0)


def test_a_negative_subtotal_and_unknown_codes_are_refused() -> None:
    with pytest.raises(ValueError):
        redeem(-1, ["FIVER"])
    with pytest.raises(ValueError):
        redeem(-1, ["NOPE"])
    with pytest.raises(ParseError) as caught:
        redeem(6000, ["FIVER", "NOPE"])
    assert str(caught.value) == "unknown voucher 'NOPE'"
    with pytest.raises(ParseError) as caught:
        redeem(100, ["nope", "alsobad"])
    assert str(caught.value) == "unknown voucher 'nope'"


def test_price_prints_one_line_per_applied_voucher_and_taxes_what_is_left() -> None:
    assert price("8", "--voucher", "TENNER") == [
        "8 x MUG-001 Blue Mug: $100.00",
        "voucher TENNER: -$10.00",
        "tax: $0.00",
        "total: $90.00",
    ]
    assert price("8", "--voucher", "tenner", "--voucher", "fiver", env={"STOCKROOM_TAX_BP": "1000"}) == [
        "8 x MUG-001 Blue Mug: $100.00",
        "voucher TENNER: -$10.00",
        "tax: $9.00",
        "total: $99.00",
    ]
    assert price("2", "--voucher", "LASTCALL", "--voucher", "TENNER", "--voucher", "FIVER") == [
        "2 x MUG-001 Blue Mug: $25.00",
        "voucher FIVER: -$5.00",
        "voucher LASTCALL: -$3.00",
        "tax: $0.00",
        "total: $17.00",
    ]
    assert price("2") == ["2 x MUG-001 Blue Mug: $25.00", "tax: $0.00", "total: $25.00"]
    assert price("2", "--voucher", "TENNER") == [
        "2 x MUG-001 Blue Mug: $25.00",
        "tax: $0.00",
        "total: $25.00",
    ]
    assert price("2", "--voucher", "FIVER", env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "}) == [
        "2 x MUG-001 Blue Mug: EUR 25.00",
        "voucher FIVER: -EUR 5.00",
        "tax: EUR 0.00",
        "total: EUR 20.00",
    ]


def test_the_minimum_spend_is_checked_against_the_amount_after_the_discount() -> None:
    assert price("8", "--discount", "10", "--voucher", "TENNER") == [
        "8 x MUG-001 Blue Mug: $90.00",
        "voucher TENNER: -$10.00",
        "tax: $0.00",
        "total: $80.00",
    ]
    assert price("8", "--discount", "60", "--voucher", "TENNER", "--voucher", "FIVER") == [
        "8 x MUG-001 Blue Mug: $40.00",
        "voucher FIVER: -$5.00",
        "tax: $0.00",
        "total: $35.00",
    ]
    assert price("8", "--discount", "100", "--voucher", "LASTCALL") == [
        "8 x MUG-001 Blue Mug: $0.00",
        "voucher LASTCALL: -$0.00",
        "tax: $0.00",
        "total: $0.00",
    ]


def test_price_refuses_an_unknown_voucher_before_printing_anything() -> None:
    done = run(
        "price", "--items", "data/items.csv", "--sku", "MUG-001", "--quantity", "8",
        "--voucher", "TENNER", "--voucher", "Bogus",
    )
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr == "stockroom: error: unknown voucher 'Bogus'\n"
