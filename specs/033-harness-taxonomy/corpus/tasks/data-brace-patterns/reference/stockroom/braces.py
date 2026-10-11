"""Brace patterns: ``MUG-{001..003}``, ``{TEA,PEN}-{1..9..4}``, nested groups and escapes."""

from __future__ import annotations

import itertools
import re

from .errors import ParseError

LIMIT = 100_000
_ESCAPABLE = "\\{},"
_NUMBERS = re.compile(r"([0-9]+)\.\.([0-9]+)(?:\.\.([0-9]+))?")
_LETTERS = re.compile(r"([A-Za-z])\.\.([A-Za-z])(?:\.\.([0-9]+))?")

# A pattern is parsed into a sequence of parts: str (literal text), list of sequences
# (alternatives), or _Range.


def _error(index: int, reason: str) -> ParseError:
    return ParseError(f"column {index + 1}: {reason}")


def _check(pattern: str) -> dict[int, int]:
    """Escapes, then brace pairs; returns {open index: close index}."""
    i = 0
    while i < len(pattern):
        if pattern[i] == "\\":
            if i + 1 >= len(pattern) or pattern[i + 1] not in _ESCAPABLE:
                raise _error(i, "bad escape")
            i += 2
            continue
        i += 1
    pairs: dict[int, int] = {}
    stack: list[int] = []
    unmatched: list[int] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "\\":
            i += 2
            continue
        if char == "{":
            stack.append(i)
        elif char == "}":
            if stack:
                pairs[stack.pop()] = i
            else:
                unmatched.append(i)
        i += 1
    unmatched += stack
    if unmatched:
        raise _error(min(unmatched), "unmatched brace")
    return pairs


class _Range:
    """The values of a range, computed lazily (a range may be very long)."""

    def __init__(self, numbers: range, width: int, letters: bool) -> None:
        self.numbers = numbers
        self.width = width
        self.letters = letters

    def size(self) -> int:
        numbers = self.numbers
        last = numbers.stop - 1 if numbers.step > 0 else numbers.stop + 1
        return abs(last - numbers.start) // abs(numbers.step) + 1

    def values(self) -> list[str]:
        if self.letters:
            return [chr(v) for v in self.numbers]
        return [str(v).zfill(self.width) for v in self.numbers]


def _steps(start: int, stop: int, step: int) -> range:
    return range(start, stop + 1, step) if start <= stop else range(start, stop - 1, -step)


def _range(text: str) -> _Range | None:
    match = _NUMBERS.fullmatch(text)
    if match:
        first, last, step_text = match.groups()
        step = int(step_text) if step_text is not None else 1
        if step == 0:
            return None
        width = 0
        if (len(first) > 1 and first[0] == "0") or (len(last) > 1 and last[0] == "0"):
            width = max(len(first), len(last))
        return _Range(_steps(int(first), int(last), step), width, False)
    match = _LETTERS.fullmatch(text)
    if match:
        first, last, step_text = match.groups()
        step = int(step_text) if step_text is not None else 1
        if step == 0 or first.islower() != last.islower():
            return None
        return _Range(_steps(ord(first), ord(last), step), 0, True)
    return None


def _parse(pattern: str, start: int, end: int, pairs: dict[int, int]) -> list:
    parts: list = []
    literal: list[str] = []
    i = start
    while i < end:
        char = pattern[i]
        if char == "\\":
            literal.append(pattern[i + 1])
            i += 2
            continue
        if char != "{":
            literal.append(char)
            i += 1
            continue
        if literal:
            parts.append("".join(literal))
            literal = []
        close = pairs[i]
        commas = []
        j = i + 1
        while j < close:
            if pattern[j] == "\\":
                j += 2
                continue
            if pattern[j] == "{":
                j = pairs[j] + 1
                continue
            if pattern[j] == ",":
                commas.append(j)
            j += 1
        if commas:
            bounds = [i, *commas, close]
            parts.append([_parse(pattern, a + 1, b, pairs) for a, b in itertools.pairwise(bounds)])
        else:
            values = _range(pattern[i + 1 : close])
            if values is None:
                raise _error(i, "a group needs a comma or a range")
            parts.append(values)
        i = close + 1
    if literal:
        parts.append("".join(literal))
    return parts


def _tree(pattern: str) -> list:
    return _parse(pattern, 0, len(pattern), _check(pattern))


def _count(parts: list) -> int:
    total = 1
    for part in parts:
        if isinstance(part, str):
            continue
        if isinstance(part, _Range):
            total *= part.size()
        else:
            total *= sum(_count(alternative) for alternative in part)
    return total


def _expand(parts: list) -> list[str]:
    results = [""]
    for part in parts:
        if isinstance(part, str):
            options = [part]
        elif isinstance(part, _Range):
            options = part.values()
        else:
            options = [text for alternative in part for text in _expand(alternative)]
        results = [left + right for left in results for right in options]
    return results


def count_braces(pattern: str) -> int:
    return _count(_tree(pattern))


def expand_braces(pattern: str) -> list[str]:
    tree = _tree(pattern)
    if _count(tree) > LIMIT:
        raise ValueError(f"pattern expands to more than {LIMIT} texts")
    return _expand(tree)


def escape_braces(text: str) -> str:
    return "".join("\\" + c if c in _ESCAPABLE else c for c in text)
