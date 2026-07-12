"""Deterministic source identifiers and safe filenames."""

import re
import unicodedata
from datetime import date


def source_id(for_date: date, exact_hash: str) -> str:
    """Build a date and content-hash source ID."""
    return f"SRC-{for_date:%Y%m%d}-{exact_hash[:8].upper()}"


def safe_slug(value: str, *, fallback: str = "source") -> str:
    """Build a path-safe lowercase slug while retaining Korean letters."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    slug = re.sub(r"[^\w]+", "-", normalized, flags=re.UNICODE)
    slug = re.sub(r"_+", "-", slug).strip("-.")
    return slug[:80].rstrip("-") or fallback
