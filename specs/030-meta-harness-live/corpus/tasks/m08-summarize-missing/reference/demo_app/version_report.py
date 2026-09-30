"""Summarize the JSON printed by `amplai ops version` for people."""

from __future__ import annotations

from typing import Any


class InvalidInfo(ValueError):
    """The version info has no usable package_version."""


def summarize(info: dict[str, Any]) -> str:
    """One line such as ``amplai 3.0.0.dev3 (DEV-03)``."""
    version = info.get("package_version")
    if not isinstance(version, str) or version == "":
        raise InvalidInfo(f"package_version must be a non-empty string: {version!r}")
    stage = info.get("stage")
    if stage is None or stage == "":
        stage = "unknown stage"
    return f"amplai {version} ({stage})"
