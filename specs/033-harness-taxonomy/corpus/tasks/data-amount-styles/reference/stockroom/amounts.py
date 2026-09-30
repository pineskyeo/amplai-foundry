"""Amounts as text in several regional styles, and back.

``format_amount`` and ``parse_amount`` work on integer cents. Styles:

=======  =================  ==========
style    ``1234567.89``     grouping
=======  =================  ==========
us       ``1,234,567.89``   thousands
eu       ``1.234.567,89``   thousands
ch       ``1'234'567.89``   thousands
in       ``12,34,567.89``   3, then 2
plain    ``1234567.89``     none
=======  =================  ==========
"""

from __future__ import annotations

import re

from .errors import ParseError

STYLES = ("us", "eu", "ch", "in", "plain")
NEGATIVES = ("minus", "paren", "trailing")
POSITIONS = ("prefix", "suffix")

_GROUP = {"us": ",", "eu": ".", "ch": "'", "in": ",", "plain": ""}
_DECIMAL = {"us": ".", "eu": ",", "ch": ".", "in": ".", "plain": "."}
_INTEGER = {
    "us": r"[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+",
    "eu": r"[0-9]{1,3}(?:\.[0-9]{3})+|[0-9]+",
    "ch": r"[0-9]{1,3}(?:['’][0-9]{3})+|[0-9]+",
    "in": r"[0-9]{1,2}(?:,[0-9]{2})*,[0-9]{3}|[0-9]+",
    "plain": r"[0-9]+",
}


def _group(digits: str, style: str) -> str:
    sep = _GROUP[style]
    if not sep:
        return digits
    if style == "in":
        if len(digits) <= 3:
            return digits
        head, parts = digits[:-3], [digits[-3:]]
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        return sep.join(parts)
    parts = []
    while len(digits) > 3:
        parts.insert(0, digits[-3:])
        digits = digits[:-3]
    parts.insert(0, digits)
    return sep.join(parts)


def format_amount(
    cents: int,
    *,
    style: str = "us",
    symbol: str = "",
    negative: str = "minus",
    symbol_position: str = "prefix",
) -> str:
    """``cents`` as text: ``format_amount(-123456, style="eu", symbol="EUR", negative="paren",
    symbol_position="suffix")`` -> ``"(1.234,56 EUR)"``."""
    if style not in STYLES:
        raise ValueError(f"unknown style: {style!r}")
    if negative not in NEGATIVES:
        raise ValueError(f"unknown negative form: {negative!r}")
    if symbol_position not in POSITIONS:
        raise ValueError(f"unknown symbol position: {symbol_position!r}")
    units, rest = divmod(abs(cents), 100)
    number = f"{_group(str(units), style)}{_DECIMAL[style]}{rest:02d}"
    if symbol:
        number = symbol + number if symbol_position == "prefix" else f"{number} {symbol}"
    if cents >= 0:
        return number
    if negative == "minus":
        return "-" + number
    if negative == "paren":
        return f"({number})"
    return number + "-"


def parse_amount(text: str, *, style: str = "us", symbol: str = "") -> int:
    """The cents of ``text`` in ``style``; anything that ``format_amount`` would not write (with
    any negative form and, when ``symbol`` is given, the symbol before or after) is a
    ``ParseError``."""
    if style not in STYLES:
        raise ValueError(f"unknown style: {style!r}")
    body = text.strip()
    negative = False
    if body.startswith("-"):
        negative, body = True, body[1:]
    elif body.startswith("(") and body.endswith(")") and len(body) >= 2:
        negative, body = True, body[1:-1]
    elif body.endswith("-"):
        negative, body = True, body[:-1]
    if symbol:
        if body.startswith(symbol):
            body = body[len(symbol) :].lstrip()
        elif body.endswith(symbol):
            body = body[: -len(symbol)].rstrip()
    pattern = rf"({_INTEGER[style]})(?:{re.escape(_DECIMAL[style])}([0-9]{{1,2}}))?"
    match = re.fullmatch(pattern, body)
    if match is None:
        raise ParseError(f"not an amount: {text!r}")
    digits = re.sub(r"[^0-9]", "", match.group(1))
    cents = int(digits) * 100 + int((match.group(2) or "").ljust(2, "0"))
    return -cents if negative else cents
