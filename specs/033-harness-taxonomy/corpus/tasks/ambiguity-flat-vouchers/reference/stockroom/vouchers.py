"""Flat vouchers: a fixed amount off, with a minimum spend."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .errors import ParseError

# code -> (cents off, minimum spend in cents); the table order is the order they are applied in
VOUCHERS: dict[str, tuple[int, int]] = {
    "FIVER": (500, 2000),
    "TENNER": (1000, 5000),
    "LASTCALL": (300, 0),
}


@dataclass(frozen=True)
class Redemption:
    lines: tuple[tuple[str, int], ...]
    total: int


def redeem(subtotal_cents: int, codes: Sequence[str]) -> Redemption:
    """Apply the vouchers ``codes`` to ``subtotal_cents``."""
    if subtotal_cents < 0:
        raise ValueError("subtotal must not be negative")
    given: list[str] = []
    for code in codes:
        if code.upper() not in VOUCHERS:
            raise ParseError(f"unknown voucher {code!r}")
        if code.upper() not in given:
            given.append(code.upper())
    eligible = [c for c in VOUCHERS if c in given and subtotal_cents >= VOUCHERS[c][1]]
    if "TENNER" in eligible and "FIVER" in eligible:
        eligible.remove("FIVER")
    running = subtotal_cents
    lines: list[tuple[str, int]] = []
    for code in eligible:
        taken = min(VOUCHERS[code][0], running)
        running -= taken
        lines.append((code, taken))
    return Redemption(tuple(lines), running)
