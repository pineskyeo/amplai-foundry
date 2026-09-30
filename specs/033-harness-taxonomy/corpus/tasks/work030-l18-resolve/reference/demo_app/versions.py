"""Version parsing and comparison helpers."""

from __future__ import annotations

import re

_VERSION = re.compile(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)")


def parse(text: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(text)
    if match is None:
        raise ValueError(f"not a MAJOR.MINOR.PATCH version: {text!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch
