"""Summarize the JSON printed by `amplai ops version` for people."""

from __future__ import annotations

from typing import Any


def summarize(info: dict[str, Any]) -> str:
    """One line such as ``amplai 3.0.0.dev3 (DEV-03)``."""
    return f"amplai {info['package_version']} ({info['stage']})"
