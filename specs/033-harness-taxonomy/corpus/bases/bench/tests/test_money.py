import pytest

from stockroom.errors import ParseError
from stockroom.money import format_money, parse_money, split_evenly


def test_parse_money_plain_amounts() -> None:
    assert parse_money("12.34") == 1234
    assert parse_money("7") == 700
    assert parse_money("0.5") == 50


def test_parse_money_symbol_and_separators() -> None:
    assert parse_money("$1,250.00") == 125000
    assert parse_money("  $3.10 ") == 310


def test_parse_money_negative() -> None:
    assert parse_money("-3.50") == -350


@pytest.mark.parametrize("text", ["", "abc", "1.234", "$", "1.2.3"])
def test_parse_money_rejects(text: str) -> None:
    with pytest.raises(ParseError):
        parse_money(text)


def test_format_money() -> None:
    assert format_money(1234) == "$12.34"
    assert format_money(5) == "$0.05"
    assert format_money(-123456) == "-$1,234.56"
    assert format_money(990, symbol="EUR ") == "EUR 9.90"


def test_split_evenly() -> None:
    assert split_evenly(100, 3) == [34, 33, 33]
    assert split_evenly(90, 3) == [30, 30, 30]
    assert sum(split_evenly(1001, 7)) == 1001
    with pytest.raises(ValueError):
        split_evenly(10, 0)
