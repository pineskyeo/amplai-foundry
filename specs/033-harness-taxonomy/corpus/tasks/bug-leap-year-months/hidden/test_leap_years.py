import calendar
from datetime import date

import pytest

from stockroom.dates import add_months, days_in_month, months_between

LEAP = (1600, 2000, 2004, 2020, 2024, 2028, 2400)
COMMON = (1700, 1800, 1900, 2021, 2023, 2025, 2100, 2200, 2300)


def test_february_follows_the_gregorian_leap_rule() -> None:
    for year in LEAP:
        assert days_in_month(year, 2) == 29, year
    for year in COMMON:
        assert days_in_month(year, 2) == 28, year


def test_other_months_and_invalid_months() -> None:
    for year in (1900, 2000, 2023, 2024):
        assert [days_in_month(year, m) for m in (1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)] == [
            31,
            31,
            30,
            31,
            30,
            31,
            31,
            30,
            31,
            30,
            31,
        ]
    for month in (0, 13, -1):
        with pytest.raises(ValueError):
            days_in_month(2024, month)


def test_days_in_month_matches_the_calendar_for_every_month_of_four_centuries() -> None:
    for year in range(1800, 2200):
        for month in range(1, 13):
            assert days_in_month(year, month) == calendar.monthrange(year, month)[1]


def test_month_end_dates_land_on_the_leap_day() -> None:
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2024, 1, 30), 1) == date(2024, 2, 29)
    assert add_months(date(2024, 1, 29), 1) == date(2024, 2, 29)
    assert add_months(date(2000, 1, 31), 1) == date(2000, 2, 29)
    assert add_months(date(2024, 3, 31), -1) == date(2024, 2, 29)
    assert add_months(date(2024, 12, 31), -10) == date(2024, 2, 29)
    assert add_months(date(2023, 12, 31), 2) == date(2024, 2, 29)
    assert add_months(date(2024, 2, 29), 48) == date(2028, 2, 29)
    assert add_months(date(2024, 2, 29), 1) == date(2024, 3, 29)
    assert add_months(date(2024, 2, 29), -1) == date(2024, 1, 29)


def test_common_years_and_century_years_clamp_to_the_28th() -> None:
    assert add_months(date(2023, 1, 29), 1) == date(2023, 2, 28)
    assert add_months(date(1900, 1, 31), 1) == date(1900, 2, 28)
    assert add_months(date(2100, 1, 30), 1) == date(2100, 2, 28)
    assert add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)
    assert add_months(date(2024, 2, 29), -12) == date(2023, 2, 28)
    assert add_months(date(2024, 2, 29), 24) == date(2026, 2, 28)
    assert add_months(date(2024, 3, 31), -13) == date(2023, 2, 28)
    assert add_months(date(2000, 2, 29), 1200) == date(2100, 2, 28)
    assert add_months(date(2000, 2, 29), 1188) == date(2099, 2, 28)
    assert add_months(date(2100, 3, 31), -1) == date(2100, 2, 28)


def test_add_months_matches_an_independent_calendar_model() -> None:
    def model(start: date, months: int) -> date:
        index = start.year * 12 + (start.month - 1) + months
        year, month = divmod(index, 12)
        last = calendar.monthrange(year, month + 1)[1]
        return date(year, month + 1, min(start.day, last))

    for ordinal in range(date(2019, 1, 1).toordinal(), date(2025, 12, 31).toordinal(), 3):
        start = date.fromordinal(ordinal)
        for months in range(-30, 31):
            assert add_months(start, months) == model(start, months), (start, months)
    for start in (date(1899, 12, 31), date(1999, 12, 31), date(2099, 12, 31), date(2000, 2, 29)):
        for months in (-25, -12, -1, 0, 1, 2, 12, 14, 25, 60, 1200):
            assert add_months(start, months) == model(start, months), (start, months)


def test_month_keys_between_dates_around_a_leap_february() -> None:
    assert months_between(date(2024, 1, 31), date(2024, 3, 1)) == ["2024-01", "2024-02", "2024-03"]
    assert months_between(date(2023, 12, 31), date(2024, 3, 31)) == [
        "2023-12",
        "2024-01",
        "2024-02",
        "2024-03",
    ]
    assert months_between(date(2024, 2, 29), date(2024, 2, 29)) == ["2024-02"]
    assert months_between(date(2100, 1, 31), date(2100, 4, 1)) == [
        "2100-01",
        "2100-02",
        "2100-03",
        "2100-04",
    ]
