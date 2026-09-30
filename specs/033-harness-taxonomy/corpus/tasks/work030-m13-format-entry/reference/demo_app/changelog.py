"""Changelog entry formatting."""

from __future__ import annotations

import re

_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def format_entry(version: str, date: str, changes: list[str]) -> str:
    if _DATE.fullmatch(date) is None:
        raise ValueError(f"date must look like YYYY-MM-DD: {date!r}")
    items = [change.strip() for change in changes]
    items = [item for item in items if item]
    body = [f"- {item}" for item in items] or ["- no changes"]
    return "\n".join([f"## {version} - {date}", ""] + body) + "\n"
