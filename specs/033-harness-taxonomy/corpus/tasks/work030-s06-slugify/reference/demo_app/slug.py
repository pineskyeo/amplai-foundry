"""Slug helpers."""

from __future__ import annotations

import re

_SEPARATORS = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    lowered = "".join(c.lower() if c.isascii() else " " for c in text)
    slug = _SEPARATORS.sub("-", lowered).strip("-")
    return slug or "n-a"
