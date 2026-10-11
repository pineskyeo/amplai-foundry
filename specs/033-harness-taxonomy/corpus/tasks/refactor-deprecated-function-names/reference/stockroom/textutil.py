"""Small text helpers used by reports and the command line."""

from __future__ import annotations

import re
import warnings

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lower-case words joined by ``-``: ``"Blue Mug, Large"`` -> ``"blue-mug-large"``."""
    return _NON_SLUG.sub("-", text.lower()).strip("-")


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


def count_label(count: int, word: str) -> str:
    """``count_label(1, "order")`` -> ``"1 order"``; ``count_label(3, "box")`` -> ``"3 boxs"``."""
    return f"{count} {word if count == 1 else word + 's'}"


def plural(count: int, word: str) -> str:
    """Deprecated alias of ``count_label``."""
    warnings.warn("plural() is deprecated; use count_label()", DeprecationWarning, stacklevel=2)
    return count_label(count, word)


plural.__wrapped__ = count_label  # type: ignore[attr-defined]
