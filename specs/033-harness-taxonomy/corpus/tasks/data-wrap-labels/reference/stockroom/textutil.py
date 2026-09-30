"""Small text helpers used by reports and the command line."""

from __future__ import annotations

import re
import unicodedata

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lower-case words joined by ``-``: ``"Blue Mug, Large"`` -> ``"blue-mug-large"``."""
    return _NON_SLUG.sub("-", text.lower()).strip("-")


def _char_width(char: str) -> int:
    if unicodedata.category(char) in ("Mn", "Me", "Cf", "Cc"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    """Terminal columns of ``text``: 0 for marks, format and control characters, 2 for wide and
    full-width characters, else 1."""
    return sum(_char_width(char) for char in text)


def _break_word(word: str, width: int) -> list[str]:
    chunks: list[str] = []
    current, used = "", 0
    for char in word:
        columns = _char_width(char)
        if current and columns > 0 and used + columns > width:
            chunks.append(current)
            current, used = "", 0
        current += char
        used += columns
    chunks.append(current)
    return chunks


def wrap_text(text: str, width: int) -> list[str]:
    """``text`` as lines of at most ``width`` columns (see ``display_width``)."""
    if width < 1:
        raise ValueError("width must be positive")
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current, used = "", 0
        for word in words:
            columns = display_width(word)
            if current and used + 1 + columns <= width:
                current += " " + word
                used += 1 + columns
                continue
            if current:
                lines.append(current)
                current, used = "", 0
            if columns <= width:
                current, used = word, columns
                continue
            chunks = _break_word(word, width)
            lines.extend(chunks[:-1])
            current, used = chunks[-1], display_width(chunks[-1])
        lines.append(current)
    return lines


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """``text`` shortened to at most ``width`` characters, ending in ``ellipsis`` when cut."""
    if width < 1:
        raise ValueError("width must be positive")
    if len(text) <= width:
        return text
    return text[:width] + ellipsis


def align(text: str, width: int, how: str = "left") -> str:
    """``text`` padded with spaces to ``width``: ``left``, ``right`` or ``center``."""
    if how == "left":
        return text.ljust(width)
    if how == "right":
        return text.rjust(width)
    if how == "center":
        return text.center(width)
    raise ValueError(f"unknown alignment: {how!r}")


def plural(count: int, word: str) -> str:
    """``plural(1, "order")`` -> ``"1 order"``; ``plural(3, "order")`` -> ``"3 orders"``."""
    return f"{count} {word if count == 1 else word + 's'}"
