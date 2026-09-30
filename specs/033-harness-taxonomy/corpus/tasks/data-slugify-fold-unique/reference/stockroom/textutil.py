"""Small text helpers used by reports and the command line."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_APOSTROPHE = re.compile(r"(?<=[a-z0-9])['’](?=[a-z0-9])")
# Letters that Unicode does not decompose into a base letter and a mark.
_FOLD = {
    "ß": "ss",
    "æ": "ae",
    "œ": "oe",
    "ø": "o",
    "đ": "d",
    "ł": "l",
    "þ": "th",
    "ð": "d",
}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    bare = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return "".join(_FOLD.get(c, c) for c in bare.lower())


def _shorten(slug: str, max_length: int) -> str:
    """The longest prefix of whole words that fits; a first word that is too long is cut."""
    if len(slug) <= max_length:
        return slug
    words = slug.split("-")
    out = words[0]
    if len(out) > max_length:
        return out[:max_length]
    for word in words[1:]:
        if len(out) + 1 + len(word) > max_length:
            break
        out += "-" + word
    return out


def _check_length(max_length: int | None) -> None:
    if max_length is not None and max_length < 1:
        raise ValueError("max_length must be positive")


def slugify(text: str, *, max_length: int | None = None) -> str:
    """Lower-case words joined by ``-``: ``"Blue Mug, Large"`` -> ``"blue-mug-large"``.

    Accents are folded (``"Café"`` -> ``"cafe"``), ``&`` is the word ``and``, an apostrophe
    between two letters or digits is dropped (``"Bob's"`` -> ``"bobs"``), and ``max_length`` keeps
    the slug to whole words where it can.
    """
    _check_length(max_length)
    folded = _fold(text).replace("&", " and ")
    folded = _APOSTROPHE.sub("", folded)
    slug = _NON_SLUG.sub("-", folded).strip("-")
    return slug if max_length is None else _shorten(slug, max_length)


def unique_slugs(names: Iterable[str], *, max_length: int | None = None) -> list[str]:
    """One slug per name, in order, all different: repeats get ``-2``, ``-3``, ..."""
    _check_length(max_length)
    used: set[str] = set()
    result = []
    for name in names:
        base = slugify(name) or "n-a"
        candidate = base if max_length is None else _shorten(base, max_length)
        count = 1
        while candidate in used:
            count += 1
            suffix = f"-{count}"
            if max_length is None:
                candidate = base + suffix
            else:
                room = max_length - len(suffix)
                if room < 1:
                    raise ValueError("max_length leaves no room for the suffix")
                candidate = _shorten(base, room) + suffix
        used.add(candidate)
        result.append(candidate)
    return result


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
