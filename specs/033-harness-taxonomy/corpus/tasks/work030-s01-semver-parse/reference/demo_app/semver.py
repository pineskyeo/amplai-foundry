"""Version text helpers."""

from __future__ import annotations


def parse_version(text: str) -> tuple[int, int, int]:
    value = text.strip()
    if value.startswith("v"):
        value = value[1:]
    parts = value.split(".")
    if len(parts) != 3 or not all(p.isascii() and p.isdigit() for p in parts):
        raise ValueError(f"not a MAJOR.MINOR.PATCH version: {text!r}")
    major, minor, patch = (int(p) for p in parts)
    return major, minor, patch
