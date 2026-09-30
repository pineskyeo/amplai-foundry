"""Summarize the JSON printed by `amplai ops version` for people."""

from __future__ import annotations

import re
from typing import Any

_VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:\.dev([0-9]+))?")


def summarize(info: dict[str, Any]) -> str:
    """One line such as ``amplai 3.0.0.dev3 (DEV-03)``."""
    return f"amplai {info['package_version']} ({info['stage']})"


def _version_key(text: str) -> tuple[int, int, int, int, int]:
    match = _VERSION.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise ValueError(f"unparsable version: {text!r}")
    major, minor, patch, dev = match.groups()
    if dev is None:
        return int(major), int(minor), int(patch), 1, 0
    return int(major), int(minor), int(patch), 0, int(dev)


def latest_per_stage(infos: list[dict]) -> dict[str, dict]:
    """Highest package_version per stage; later items win ties; keys sorted by stage."""
    best: dict[str, tuple[tuple[int, int, int, int, int], dict]] = {}
    for info in infos:
        if "stage" not in info:
            continue
        key = _version_key(info["package_version"])
        current = best.get(info["stage"])
        if current is None or key >= current[0]:
            best[info["stage"]] = (key, info)
    return {stage: best[stage][1] for stage in sorted(best)}
