"""Version text helpers."""

from __future__ import annotations

import re

_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)", re.ASCII)


def _parse(text: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"not a MAJOR.MINOR.PATCH version: {text!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def compare_versions(a: str, b: str) -> int:
    left, right = _parse(a), _parse(b)
    return (left > right) - (left < right)
