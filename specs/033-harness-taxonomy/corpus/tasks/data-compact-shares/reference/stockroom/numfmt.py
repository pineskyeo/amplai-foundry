"""Compact numbers (``1.5K``) and percentage shares that add up to exactly 100 %."""

from __future__ import annotations

import re
from collections.abc import Sequence
from fractions import Fraction

from .errors import ParseError

_UNITS = (("K", 10**3), ("M", 10**6), ("B", 10**9), ("T", 10**12))
_COMPACT = re.compile(r"(-?)([0-9]+)(?:\.([0-9]+))?([kKmMbBtT]?)")


def _tenths(amount: int, unit: int) -> int:
    """``amount / unit`` in tenths, rounded half up (``amount`` is not negative)."""
    return (amount * 20 // unit + 1) // 2


def format_compact(number: int) -> str:
    """``1500`` -> ``"1.5K"``, ``999950`` -> ``"1M"``, ``-2000000`` -> ``"-2M"``."""
    amount = abs(number)
    if amount < 1000:
        return str(number)
    index = max(i for i, (_, unit) in enumerate(_UNITS) if amount >= unit)
    tenths = _tenths(amount, _UNITS[index][1])
    if tenths >= 10000 and index + 1 < len(_UNITS):
        index += 1
        tenths = _tenths(amount, _UNITS[index][1])
    whole, frac = divmod(tenths, 10)
    text = f"{whole}.{frac}" if frac else str(whole)
    return ("-" if number < 0 else "") + text + _UNITS[index][0]


def parse_compact(text: str) -> int:
    """``"1.5K"`` -> 1500; the value must be a whole number."""
    match = _COMPACT.fullmatch(text.strip())
    if match is None:
        raise ParseError(f"not a number: {text!r}")
    sign, whole, frac, suffix = match.groups()
    unit = dict(_UNITS).get(suffix.upper(), 1)
    value = Fraction(int(whole + (frac or "")), 10 ** len(frac or "")) * unit
    if value.denominator != 1:
        raise ParseError(f"not a whole number: {text!r}")
    return -int(value) if sign else int(value)


def percent_shares(values: Sequence[int], digits: int = 1) -> list[str]:
    """Each value's share of the total as text such as ``"45.3%"``, rounded by the largest
    remainder method so that the shares add up to exactly 100 (ties go to the earlier value).
    All values zero: every share is 0."""
    if isinstance(digits, bool) or not 0 <= digits <= 4:
        raise ValueError("digits must be between 0 and 4")
    if any(value < 0 for value in values):
        raise ValueError("values must not be negative")
    total = sum(values)
    scale = 100 * 10**digits
    if total == 0:
        scaled = [0] * len(values)
    else:
        parts = [divmod(value * scale, total) for value in values]
        scaled = [quotient for quotient, _ in parts]
        missing = scale - sum(scaled)
        order = sorted(range(len(values)), key=lambda i: (-parts[i][1], i))
        for i in order[:missing]:
            scaled[i] += 1
    unit = 10**digits
    if digits == 0:
        return [f"{share}%" for share in scaled]
    return [f"{share // unit}.{share % unit:0{digits}d}%" for share in scaled]
