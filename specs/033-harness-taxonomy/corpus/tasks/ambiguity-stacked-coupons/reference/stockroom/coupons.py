"""Percentage coupons."""

from __future__ import annotations

from collections.abc import Sequence

from .errors import ParseError

COUPONS: dict[str, int] = {"WELCOME": 10, "BULK": 5, "VIP": 20}


def _codes(codes: Sequence[str]) -> list[str]:
    """The distinct known codes in upper case, in the order first given."""
    out: list[str] = []
    for code in codes:
        if code.upper() not in COUPONS:
            raise ParseError(f"unknown coupon {code!r}")
        if code.upper() not in out:
            out.append(code.upper())
    return out


def apply_coupons(cents: int, codes: Sequence[str]) -> int:
    """``cents`` after the coupons, rounded half up to a whole cent."""
    distinct = _codes(codes)
    percent = sum(COUPONS[code] for code in distinct)
    return (cents * (100 - percent) + 50) // 100
