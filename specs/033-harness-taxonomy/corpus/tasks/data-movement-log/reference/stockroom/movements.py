"""A stock movement log: one line per movement, read, written and replayed on an inventory.

Line format (fields separated by spaces or tabs)::

    2024-03-09T14:05:33Z RECEIVE MUG-001 12 "delivery, pallet 7"
    2024-03-09T16:05:33+02:00 SHIP MUG-001 3
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from .errors import ParseError
from .inventory import Inventory
from .models import check_sku

KINDS = ("RECEIVE", "RESERVE", "RELEASE", "SHIP")
_TIME = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(Z|([+-])([0-9]{2}):([0-9]{2}))"
)
_FIELD = re.compile(r"[ \t]*([^ \t]+)")
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t"}


@dataclass(frozen=True)
class Movement:
    at: datetime
    kind: str
    sku: str
    quantity: int
    note: str = ""


def _timestamp(text: str) -> datetime:
    match = _TIME.fullmatch(text)
    if match is None:
        raise ParseError(f"bad timestamp {text!r}")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    offset = timedelta()
    if match.group(7) != "Z":
        if int(match.group(9)) > 23 or int(match.group(10)) > 59:
            raise ParseError(f"bad time zone offset in {text!r}")
        offset = timedelta(hours=int(match.group(9)), minutes=int(match.group(10)))
        if match.group(8) == "-":
            offset = -offset
    try:
        local = datetime(year, month, day, hour, minute, second, tzinfo=timezone(offset))
    except ValueError as exc:
        raise ParseError(f"bad timestamp {text!r}") from exc
    return local.astimezone(UTC)


def _note(rest: str) -> str:
    """The text of one quoted string that fills ``rest`` (surrounding whitespace aside)."""
    rest = rest.strip(" \t")
    if not rest.startswith('"'):
        raise ParseError(f"the note must be a quoted string, not {rest!r}")
    out: list[str] = []
    i = 1
    while i < len(rest):
        char = rest[i]
        if char == "\\":
            if i + 1 >= len(rest) or rest[i + 1] not in _ESCAPES:
                raise ParseError(f"bad escape in note {rest!r}")
            out.append(_ESCAPES[rest[i + 1]])
            i += 2
        elif char == '"':
            if rest[i + 1 :].strip(" \t"):
                raise ParseError(f"text after the note {rest!r}")
            return "".join(out)
        else:
            out.append(char)
            i += 1
    raise ParseError(f"unterminated note {rest!r}")


def _parse_line(line: str) -> Movement:
    position = 0
    fields = []
    for _ in range(4):
        match = _FIELD.match(line, position)
        if match is None:
            raise ParseError("expected TIMESTAMP KIND SKU QUANTITY")
        fields.append(match.group(1))
        position = match.end()
    stamp, kind, sku, quantity = fields
    if kind not in KINDS:
        raise ParseError(f"unknown kind {kind!r}")
    check_sku(sku)
    digits = quantity[1:] if quantity.startswith("+") else quantity
    if not re.fullmatch(r"[0-9]+", digits) or int(digits) == 0:
        raise ParseError(f"quantity must be a positive whole number, not {quantity!r}")
    rest = line[position:]
    note = _note(rest) if rest.strip(" \t") else ""
    return Movement(_timestamp(stamp), kind, sku, int(digits), note)


def parse_movements(text: str) -> list[Movement]:
    """The movements of a log, in file order. Lines end at ``\\n`` only (a trailing ``\\r`` is
    dropped); blank lines and lines starting with ``#`` are skipped; an error says ``line N:``."""
    movements = []
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        stripped = line.strip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        try:
            movements.append(_parse_line(line))
        except ParseError as exc:
            raise ParseError(f"line {number}: {exc}") from exc
    return movements


def format_movement(movement: Movement) -> str:
    """The canonical line: the time in UTC with ``Z``, single spaces, a quoted note if any."""
    at = movement.at
    if at.tzinfo is None or at.utcoffset() is None or at.microsecond:
        raise ValueError("the time must be timezone-aware and in whole seconds")
    parts = [
        f"{at.astimezone(UTC):%Y-%m-%dT%H:%M:%S}Z",
        movement.kind,
        movement.sku,
        str(movement.quantity),
    ]
    if movement.note:
        escaped = (
            movement.note.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\t", "\\t")
        )
        parts.append(f'"{escaped}"')
    return " ".join(parts)


def apply_movements(inventory: Inventory, movements: Iterable[Movement]) -> None:
    """Apply the movements to ``inventory`` oldest first (ties keep their given order)."""
    for movement in sorted(movements, key=lambda m: m.at):
        if movement.kind == "RECEIVE":
            inventory.receive(movement.sku, movement.quantity)
        elif movement.kind == "RESERVE":
            inventory.reserve(movement.sku, movement.quantity)
        elif movement.kind == "RELEASE":
            inventory.release(movement.sku, movement.quantity)
        else:
            inventory.ship(movement.sku, movement.quantity)
