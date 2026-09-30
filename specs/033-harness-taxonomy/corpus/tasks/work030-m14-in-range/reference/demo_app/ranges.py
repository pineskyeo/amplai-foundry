"""Version range checks."""

from __future__ import annotations

import operator
import re
from typing import Callable

_VERSION = re.compile(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)")
_OPERATORS: list[tuple[str, Callable[[object, object], bool]]] = [
    (">=", operator.ge),
    ("<=", operator.le),
    ("==", operator.eq),
    ("!=", operator.ne),
    (">", operator.gt),
    ("<", operator.lt),
]


def _parse(text: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"not a MAJOR.MINOR.PATCH version: {text!r}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _comparison(piece: str) -> tuple[Callable[[object, object], bool], tuple[int, int, int]]:
    text = piece.strip()
    for symbol, compare in _OPERATORS:
        if text.startswith(symbol):
            return compare, _parse(text[len(symbol):])
    raise ValueError(f"malformed comparison: {piece!r}")


def in_range(version: str, spec: str) -> bool:
    value = _parse(version)
    if spec.strip() == "":
        return True
    comparisons = [_comparison(piece) for piece in spec.split(",")]
    return all(compare(value, bound) for compare, bound in comparisons)
