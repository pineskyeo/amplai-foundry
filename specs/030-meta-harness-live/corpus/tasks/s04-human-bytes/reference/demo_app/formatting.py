"""Text formatting helpers."""

from __future__ import annotations

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def human_bytes(n: int) -> str:
    if n < 0:
        raise ValueError(f"byte count must not be negative: {n}")
    if n < 1024:
        return f"{n} B"
    index = 1
    while index < len(_UNITS) - 1 and n >= 1024 ** (index + 1):
        index += 1
    return f"{n / 1024 ** index:.1f} {_UNITS[index]}"
