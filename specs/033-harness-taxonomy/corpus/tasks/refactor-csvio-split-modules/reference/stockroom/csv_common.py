"""Helpers the CSV readers share."""

from __future__ import annotations

from .errors import ParseError


def int_cell(value: str, what: str, number: int) -> int:
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ParseError(f"line {number}: {what} is not a whole number: {value!r}") from exc
