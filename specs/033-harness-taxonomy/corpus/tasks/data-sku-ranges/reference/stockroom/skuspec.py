"""SKU lists with ranges: ``MUG-001..MUG-003, TEA-010`` and back."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .errors import ParseError

_SKU = re.compile(r"([A-Z]{2,4}-)([0-9]{3,5})")
Key = tuple[str, int, int]  # (text before the digits, digit count, number)


def _key(sku: str) -> Key:
    match = _SKU.fullmatch(sku)
    if match is None:
        raise ParseError(f"bad sku: {sku!r}")
    prefix, digits = match.groups()
    return prefix, len(digits), int(digits)


def _text(key: Key) -> str:
    prefix, width, number = key
    return f"{prefix}{number:0{width}d}"


def expand_skus(spec: str) -> list[str]:
    """The SKUs of ``spec``, sorted by (text before the digits, digit count, number), no repeats."""
    if not spec.strip():
        return []
    found: set[Key] = set()
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            raise ParseError(f"empty item in {spec!r}")
        if ".." not in item:
            found.add(_key(item))
            continue
        start, _, end = item.partition("..")
        if not start or not end or ".." in end:
            raise ParseError(f"bad range: {item!r}")
        first = _key(start)
        if re.fullmatch(r"[0-9]+", end):
            last = (first[0], len(end), int(end))
        else:
            last = _key(end)
        if first[:2] != last[:2] or first[2] > last[2]:
            raise ParseError(f"bad range: {item!r}")
        found.update((first[0], first[1], n) for n in range(first[2], last[2] + 1))
    return [_text(key) for key in sorted(found)]


def compress_skus(skus: Iterable[str]) -> str:
    """The shortest list for ``skus``: runs of three or more consecutive numbers of one prefix and
    digit count become ``FIRST..LAST``; the pieces are joined by ``", "``."""
    keys = sorted({_key(sku) for sku in skus})
    pieces: list[str] = []
    run: list[Key] = []

    def flush() -> None:
        if len(run) >= 3:
            pieces.append(f"{_text(run[0])}..{_text(run[-1])}")
        else:
            pieces.extend(_text(key) for key in run)
        run.clear()

    for key in keys:
        if run and (run[-1][:2] != key[:2] or run[-1][2] + 1 != key[2]):
            flush()
        run.append(key)
    flush()
    return ", ".join(pieces)
