from datetime import date

import pytest

from stockroom.dates import (
    add_months,
    days_between,
    format_date,
    month_key,
    months_between,
    parse_date,
)
from stockroom.errors import ParseError


def test_parse_and_format() -> None:
    assert parse_date(" 2024-03-09 ") == date(2024, 3, 9)
    assert format_date(date(2024, 3, 9)) == "2024-03-09"
    with pytest.raises(ParseError):
        parse_date("2024-13-01")
    with pytest.raises(ParseError):
        parse_date("next tuesday")


def test_month_key() -> None:
    assert month_key(date(2024, 3, 9)) == "2024-03"


def test_add_months() -> None:
    assert add_months(date(2024, 11, 15), 3) == date(2025, 2, 15)
    assert add_months(date(2023, 1, 31), 1) == date(2023, 2, 28)
    assert add_months(date(2024, 3, 31), 1) == date(2024, 4, 30)
    assert add_months(date(2024, 5, 10), -6) == date(2023, 11, 10)


def test_days_between() -> None:
    assert days_between(date(2024, 3, 1), date(2024, 3, 11)) == 10
    assert days_between(date(2024, 3, 11), date(2024, 3, 1)) == -10


def test_months_between() -> None:
    assert months_between(date(2023, 11, 20), date(2024, 2, 1)) == [
        "2023-11",
        "2023-12",
        "2024-01",
        "2024-02",
    ]
    assert months_between(date(2024, 2, 1), date(2024, 1, 1)) == []
