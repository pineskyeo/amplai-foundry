"""Money as integer cents. Amounts are text such as ``12.34``, ``$1,250.00`` or ``-3.50``."""

from __future__ import annotations

import re

from .errors import ParseError

_AMOUNT = re.compile(r"-?\d+(\.\d{1,2})?")


def parse_money(text: str) -> int:
    """The amount in cents: ``"12.34"`` -> 1234, ``"-3.5"`` -> -350.

    A leading ``$`` and ``,`` thousands separators are accepted. Anything else is a
    ``ParseError``.
    """
    cleaned = text.strip().replace(",", "")
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    if not _AMOUNT.fullmatch(cleaned):
        raise ParseError(f"not an amount: {text!r}")
    whole, _, frac = cleaned.partition(".")
    units = int(whole)
    cents = int(frac.ljust(2, "0")) if frac else 0
    if units >= 0:
        return units * 100 + cents
    return units * 100 - cents


def format_money(cents: int, symbol: str = "$") -> str:
    """``1234`` -> ``"$12.34"``; ``-123456`` -> ``"-$1,234.56"``."""
    sign = "-" if cents < 0 else ""
    units, rest = divmod(abs(cents), 100)
    return f"{sign}{symbol}{units:,}.{rest:02d}"


def split_evenly(total: int, parts: int) -> list[int]:
    """``total`` cents in ``parts`` shares that differ by at most one cent and sum to ``total``.

    The larger shares come first: ``split_evenly(100, 3)`` -> ``[34, 33, 33]``.
    """
    if parts <= 0:
        raise ValueError("parts must be positive")
    base, extra = divmod(total, parts)
    return [base + 1 if i < extra else base for i in range(parts)]
