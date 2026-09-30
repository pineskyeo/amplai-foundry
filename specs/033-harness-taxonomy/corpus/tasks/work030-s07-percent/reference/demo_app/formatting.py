"""Text formatting helpers."""

from __future__ import annotations


def percent(part: int, total: int) -> str:
    if part < 0 or total < 0:
        raise ValueError(f"part and total must not be negative: {part}, {total}")
    if total == 0:
        return "n/a"
    return f"{part * 100 / total:.1f}%"
