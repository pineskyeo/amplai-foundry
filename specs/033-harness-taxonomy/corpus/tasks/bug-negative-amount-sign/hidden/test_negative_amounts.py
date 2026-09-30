import os
from pathlib import Path

import pytest

from stockroom.cli import main
from stockroom.csvio import read_items, read_orders
from stockroom.errors import ParseError
from stockroom.money import format_money, parse_money


@pytest.fixture(autouse=True)
def _clean_stockroom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("STOCKROOM_"):
            monkeypatch.delenv(name)


ITEM_HEADER = "sku,name,price,tags,reorder_level\n"
ORDER_HEADER = "order_id,customer,placed,status,sku,quantity,unit_price\n"


def test_negative_amounts_below_one_keep_their_sign() -> None:
    assert parse_money("-0.50") == -50
    assert parse_money("-0.05") == -5
    assert parse_money("-0.5") == -50
    assert parse_money("-0.99") == -99
    assert parse_money("-0.01") == -1
    assert parse_money("  -0.50 ") == -50
    assert parse_money("-0.10") == -10


def test_other_negative_amounts_are_unchanged() -> None:
    assert parse_money("-1.50") == -150
    assert parse_money("-3.5") == -350
    assert parse_money("-12") == -1200
    assert parse_money("-1,234.05") == -123405
    assert parse_money("-1000.99") == -100099
    assert parse_money("-3.50") == -350


def test_zero_and_positive_amounts_are_unchanged() -> None:
    assert parse_money("0.00") == 0
    assert parse_money("-0") == 0
    assert parse_money("-0.00") == 0
    assert parse_money("0.50") == 50
    assert parse_money("0.05") == 5
    assert parse_money("$0.75") == 75
    assert parse_money("12.34") == 1234


def test_formatted_amounts_read_back() -> None:
    for cents in range(-500, 501):
        assert parse_money(format_money(cents, symbol="")) == cents, cents
    for cents in (-123456, -100000, -99999, -100001, 123456):
        assert parse_money(format_money(cents, symbol="")) == cents, cents


@pytest.mark.parametrize("price", ["-0.50", "-0.05"])
def test_catalogue_with_negative_fraction_price_is_refused(price: str) -> None:
    with pytest.raises(ParseError):
        read_items(ITEM_HEADER + f"MUG-001,Mug,{price},,0\n")
    # a neighbouring valid row does not change the verdict
    with pytest.raises(ParseError):
        read_items(ITEM_HEADER + f"TEA-010,Tea,1.00,,0\nMUG-001,Mug,{price},,0\n")


def test_order_file_with_negative_fraction_unit_price_is_refused() -> None:
    row = "A-1,Hana Kim,2024-01-08,open,MUG-001,1,-0.25\n"
    with pytest.raises(ParseError):
        read_orders(ORDER_HEADER + row)
    ok = read_orders(ORDER_HEADER + "A-1,Hana Kim,2024-01-08,open,MUG-001,1,0.25\n")
    assert ok[0].lines[0].unit_price_cents == 25


def test_cli_refuses_a_catalogue_with_a_negative_price(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "items.csv"
    path.write_text(ITEM_HEADER + "MUG-001,Mug,-0.50,,0\n")
    assert main(["items", "--file", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("stockroom: error: ")
    assert captured.out == ""
