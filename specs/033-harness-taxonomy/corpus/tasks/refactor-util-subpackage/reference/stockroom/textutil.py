"""Kept so that ``stockroom.textutil`` still imports; the code lives in ``stockroom.util.textutil``."""

from __future__ import annotations

from .util.textutil import align, plural, slugify, truncate

__all__ = ["align", "plural", "slugify", "truncate"]
