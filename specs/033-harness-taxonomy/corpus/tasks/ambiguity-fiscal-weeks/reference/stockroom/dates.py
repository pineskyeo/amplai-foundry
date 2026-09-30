"""ISO dates and month arithmetic. Nothing here reads the clock; callers pass dates."""

from __future__ import annotations

import re
from datetime import date, timedelta

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


def days_in_month(year: int, month: int) -> int:
    if not 1 <= month <= 12:
        raise ValueError(f"no month {month}")
    return _DAYS_IN_MONTH[month - 1]


def add_months(value: date, months: int) -> date:
    """The same day ``months`` later (or earlier when negative), clamped to the month's last day:
    ``add_months(date(2023, 1, 31), 1)`` -> ``date(2023, 2, 28)``."""
    index = value.month - 1 + months
    year, month0 = value.year + index // 12, index % 12
    day = min(value.day, days_in_month(year, month0 + 1))
    return date(year, month0 + 1, day)


_WEEK_LABEL = re.compile(r"([0-9]{4})-W([0-9]{2})")


def _saturday(value: date) -> date:
    return value + timedelta(days=(5 - value.weekday()) % 7)


def fiscal_week(value: date) -> str:
    """The fiscal week label ``YYYY-Www`` of ``value``.

    Weeks run Sunday to Saturday and belong to the year of their Saturday; week 01 is the week
    of the year's first Saturday.
    """
    saturday = _saturday(value)
    week = (saturday.timetuple().tm_yday - 1) // 7 + 1
    return f"{saturday.year:04d}-W{week:02d}"


def week_bounds(label: str) -> tuple[date, date]:
    """The Sunday and the Saturday of the fiscal week ``label`` (see ``fiscal_week``)."""
    found = _WEEK_LABEL.fullmatch(label)
    if found is None:
        raise ParseError(f"not a week: {label!r}")
    year, week = int(found.group(1)), int(found.group(2))
    try:
        january_first = date(year, 1, 1)
        saturday = _saturday(january_first) + timedelta(weeks=week - 1)
    except (ValueError, OverflowError):
        raise ParseError(f"not a week: {label!r}") from None
    if week < 1 or saturday.year != year:
        raise ParseError(f"not a week: {label!r}")
    return saturday - timedelta(days=6), saturday


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
