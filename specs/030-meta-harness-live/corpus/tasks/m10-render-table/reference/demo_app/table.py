"""Plain-text tables."""

from __future__ import annotations


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    for row in rows:
        if len(row) != len(headers):
            raise ValueError(f"row has {len(row)} cells, expected {len(headers)}: {row!r}")
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def line(cells: list[str]) -> str:
        return " | ".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip(" ")

    separator = "-+-".join("-" * width for width in widths)
    return "\n".join([line(headers), separator] + [line(row) for row in rows])
