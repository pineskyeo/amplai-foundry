"""ISO dates and month arithmetic. Nothing here reads the clock; callers pass dates."""

from __future__ import annotations

import re
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


MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
DATE_STYLES = ("iso", "slash", "dotted", "compact", "short", "long", "us", "ordinal")
_ISO = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_SLASH = re.compile(r"([0-9]{4})/([0-9]{1,2})/([0-9]{1,2})")
_DOTTED = re.compile(r"([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{4})")
_COMPACT = re.compile(r"([0-9]{4})([0-9]{2})([0-9]{2})")
_DAY_FIRST = re.compile(r"([0-9]{1,2})([A-Za-z]{2})? +([A-Za-z]+) +([0-9]{4})")
_MONTH_FIRST = re.compile(r"([A-Za-z]+) +([0-9]{1,2})([A-Za-z]{2})?, +([0-9]{4})")


def _ordinal(day: int) -> str:
    if 11 <= day % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _month(name: str) -> int:
    lowered = name.lower()
    for number, full in enumerate(MONTH_NAMES, start=1):
        if lowered in (full.lower(), full.lower()[:3]):
            return number
    raise ParseError(f"not a month: {name!r}")


def _checked_suffix(day: int, suffix: str | None) -> None:
    if suffix is not None and suffix.lower() != _ordinal(day):
        raise ParseError(f"wrong ordinal suffix {suffix!r} for day {day}")


def parse_loose_date(text: str) -> date:
    """A date in one of the written forms ``2024-03-09``, ``2024/3/9``, ``9.3.2024``,
    ``20240309``, ``9th Mar 2024``, ``9 March 2024`` or ``March 9, 2024``; anything else,
    including ``03/04/2024``, is a ``ParseError``."""
    body = text.strip()
    try:
        if match := _ISO.fullmatch(body) or _SLASH.fullmatch(body) or _COMPACT.fullmatch(body):
            year, month, day = (int(part) for part in match.groups())
        elif match := _DOTTED.fullmatch(body):
            day, month, year = (int(part) for part in match.groups())
        elif match := _DAY_FIRST.fullmatch(body):
            day, suffix, name, year_text = match.groups()
            _checked_suffix(int(day), suffix)
            day, month, year = int(day), _month(name), int(year_text)
        elif match := _MONTH_FIRST.fullmatch(body):
            name, day, suffix, year_text = match.groups()
            _checked_suffix(int(day), suffix)
            day, month, year = int(day), _month(name), int(year_text)
        else:
            raise ParseError(f"not a date: {text!r}")
        return date(year, month, day)
    except ValueError as exc:
        raise ParseError(f"not a date: {text!r}") from exc


def format_date_style(value: date, style: str) -> str:
    """``value`` in a style of ``DATE_STYLES``; ``parse_loose_date`` reads every one back."""
    year, month, day = f"{value.year:04d}", value.month, value.day
    name = MONTH_NAMES[month - 1]
    if style == "iso":
        return f"{year}-{month:02d}-{day:02d}"
    if style == "slash":
        return f"{year}/{month}/{day}"
    if style == "dotted":
        return f"{day}.{month}.{year}"
    if style == "compact":
        return f"{year}{month:02d}{day:02d}"
    if style == "short":
        return f"{day} {name[:3]} {year}"
    if style == "long":
        return f"{day} {name} {year}"
    if style == "us":
        return f"{name} {day}, {year}"
    if style == "ordinal":
        return f"{day}{_ordinal(day)} {name} {year}"
    raise ValueError(f"unknown date style: {style!r}")


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
