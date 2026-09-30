"""Pick the highest available version that satisfies each constraint."""

from __future__ import annotations

import operator
from typing import Callable

from demo_app.versions import parse


class ResolveError(LookupError):
    """A package cannot be resolved."""


Check = Callable[[tuple, tuple], bool]

# Longest operators first so that ">=" is not read as ">".
_OPERATORS: list[tuple[str, Check]] = [
    (">=", operator.ge),
    ("<=", operator.le),
    ("==", operator.eq),
    (">", operator.gt),
    ("<", operator.lt),
]


def _parse_comparison(text: str) -> tuple[Check, tuple[int, int, int]]:
    body = text.strip()
    for symbol, check in _OPERATORS:
        if body.startswith(symbol):
            return check, parse(body[len(symbol) :].strip())
    raise ValueError(f"comparison has no valid operator: {text!r}")


def parse_constraint(constraint: str) -> list[tuple[Check, tuple[int, int, int]]]:
    if not constraint.strip():
        return []
    return [_parse_comparison(part) for part in constraint.split(",")]


def _pick(name: str, constraint: str, candidates: list[str]) -> str:
    rules = parse_constraint(constraint)
    keyed = [(parse(text), text) for text in candidates]
    best = None
    for key, text in keyed:
        if all(check(key, bound) for check, bound in rules):
            if best is None or key > best[0]:
                best = (key, text)
    if best is None:
        raise ResolveError(f"no version of {name} satisfies {constraint}")
    return best[1]


def resolve(constraints: dict[str, str], available: dict[str, list[str]]) -> dict[str, str]:
    chosen: dict[str, str] = {}
    for name, constraint in constraints.items():
        if name not in available:
            raise ResolveError(f"package {name} is not available")
        chosen[name] = _pick(name, constraint, available[name])
    return chosen
