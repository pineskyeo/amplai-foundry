"""Stock adjustments as fixed-width records (a legacy warehouse export).

Columns (1-based, inclusive): 1-10 SKU, 11-18 signed quantity (zoned decimal with an
overpunched last digit), 19-26 date ``YYYYMMDD``, 27-46 reason.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from .errors import ParseError, StockError

RECORD_LENGTH = 46
_SKU = re.compile(r"[A-Z]{2,4}-[0-9]{3,5}")
_ASCII_DIGITS = "0123456789"
_POSITIVE = "{ABCDEFGHI"
_NEGATIVE = "}JKLMNOPQR"
_MAX_DELTA = 99_999_999


@dataclass(frozen=True)
class Adjustment:
    sku: str
    delta: int
    day: date
    reason: str = ""


def _lines(text: str) -> list[str]:
    lines: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\r" or char == "\n":
            lines.append("".join(current))
            current = []
            if char == "\r" and i + 1 < len(text) and text[i + 1] == "\n":
                i += 1
        else:
            current.append(char)
        i += 1
    if current:
        lines.append("".join(current))
    return lines


def _fail(number: int, column: int, message: str) -> ParseError:
    return ParseError(f"line {number}, column {column}: {message}")


def _record(line: str, number: int) -> Adjustment:
    if len(line) > RECORD_LENGTH:
        raise _fail(number, RECORD_LENGTH + 1, f"record longer than {RECORD_LENGTH} characters")
    line = line.ljust(RECORD_LENGTH)
    sku = line[0:10].rstrip(" ")
    if not _SKU.fullmatch(sku):
        raise _fail(number, 1, f"bad sku {sku!r}")
    for offset in range(10, 17):
        if line[offset] not in _ASCII_DIGITS:
            raise _fail(number, offset + 1, "quantity digit expected")
    last = line[17]
    if last in _ASCII_DIGITS:
        sign, digit = 1, _ASCII_DIGITS.index(last)
    elif last in _POSITIVE:
        sign, digit = 1, _POSITIVE.index(last)
    elif last in _NEGATIVE:
        sign, digit = -1, _NEGATIVE.index(last)
    else:
        raise _fail(number, 18, f"bad sign digit {last!r}")
    delta = sign * (int(line[10:17]) * 10 + digit)
    if delta == 0:
        raise _fail(number, 11, "zero quantity")
    for offset in range(18, 26):
        if line[offset] not in _ASCII_DIGITS:
            raise _fail(number, offset + 1, "date digit expected")
    try:
        day = date(int(line[18:22]), int(line[22:24]), int(line[24:26]))
    except ValueError:
        raise _fail(number, 19, f"no such date {line[18:26]!r}") from None
    reason = line[26:].rstrip(" ")
    for offset, char in enumerate(reason, start=27):
        if unicodedata.category(char) == "Cc":
            raise _fail(number, offset, "control character in reason")
    return Adjustment(sku=sku, delta=delta, day=day, reason=reason)


def parse_adjustments(text: str) -> list[Adjustment]:
    """The adjustments of a fixed-width file, in file order."""
    if text.startswith("\ufeff"):
        text = text[1:]
    adjustments = []
    for number, line in enumerate(_lines(text), start=1):
        if line.strip(" ") == "":
            continue
        adjustments.append(_record(line, number))
    return adjustments


def format_adjustment(adjustment: Adjustment) -> str:
    """The canonical 46-character record (no line end)."""
    if not _SKU.fullmatch(adjustment.sku):
        raise ValueError(f"bad sku {adjustment.sku!r}")
    delta = adjustment.delta
    if delta == 0 or abs(delta) > _MAX_DELTA:
        raise ValueError(f"quantity out of range: {delta}")
    reason = adjustment.reason
    if len(reason) > 20 or reason.endswith(" "):
        raise ValueError(f"reason cannot be written: {reason!r}")
    if any(unicodedata.category(char) == "Cc" for char in reason):
        raise ValueError("control character in reason")
    digits = f"{abs(delta):08d}"
    signs = _POSITIVE if delta > 0 else _NEGATIVE
    day = adjustment.day
    return (
        adjustment.sku.ljust(10)
        + digits[:7]
        + signs[int(digits[7])]
        + f"{day.year:04d}{day.month:02d}{day.day:02d}"
        + reason.ljust(20)
    )


def format_adjustments(adjustments: Iterable[Adjustment]) -> str:
    return "".join(format_adjustment(a) + "\r\n" for a in adjustments)


def apply_adjustments(
    levels: Iterable[tuple[str, int, int]], adjustments: Iterable[Adjustment]
) -> list[tuple[str, int, int]]:
    """Stock levels after the adjustments, applied by date (equal dates in the given order)."""
    on_hand: dict[str, int] = {}
    reserved: dict[str, int] = {}
    for sku, have, held in levels:
        on_hand[sku] = have
        reserved[sku] = held
    for adjustment in sorted(adjustments, key=lambda a: a.day):
        sku = adjustment.sku
        have = on_hand.get(sku, 0)
        held = reserved.get(sku, 0)
        if have + adjustment.delta < held:
            raise StockError(
                f"{sku}: cannot remove {-adjustment.delta}, only {have - held} available"
            )
        on_hand[sku] = have + adjustment.delta
        reserved[sku] = held
    return [(sku, on_hand[sku], reserved[sku]) for sku in sorted(on_hand)]
