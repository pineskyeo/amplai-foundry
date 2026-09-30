"""ISO dates and month arithmetic. Nothing here reads the clock; callers pass dates."""

from __future__ import annotations

from datetime import date

from .errors import ParseError

_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def parse_date(text: str) -> date:
    """``"2024-03-09"`` -> ``date(2024, 3, 9)``; anything else is a ``ParseError``."""
    try:
        return date.fromisoformat(text.strip())
    except ValueError as exc:
        raise ParseError(f"not a date: {text!r}") from exc


def format_date(value: date) -> str:
    return value.isoformat()


def month_key(value: date) -> str:
    """``date(2024, 3, 9)`` -> ``"2024-03"``."""
    return f"{value.year:04d}-{value.month:02d}"


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_in_month(year: int, month: int) -> int:
    if not 1 <= month <= 12:
        raise ValueError(f"no month {month}")
    if month == 2 and _is_leap(year):
        return 29
    return _DAYS_IN_MONTH[month - 1]


def add_months(value: date, months: int) -> date:
    """The same day ``months`` later (or earlier when negative), clamped to the month's last day:
    ``add_months(date(2023, 1, 31), 1)`` -> ``date(2023, 2, 28)``."""
    index = value.month - 1 + months
    year, month0 = value.year + index // 12, index % 12
    day = min(value.day, days_in_month(year, month0 + 1))
    return date(year, month0 + 1, day)


def days_between(start: date, end: date) -> int:
    """Whole days from ``start`` to ``end`` (negative when ``end`` is earlier)."""
    return (end - start).days


def months_between(start: date, end: date) -> list[str]:
    """The month keys from ``start``'s month to ``end``'s month, both included."""
    if end < start:
        return []
    keys = []
    current = date(start.year, start.month, 1)
    while current <= end:
        keys.append(month_key(current))
        current = add_months(current, 1)
    return keys
