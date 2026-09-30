"""Release stage helpers."""

from __future__ import annotations

import re

_DEV = re.compile(r"DEV-(0[1-9]|[1-9][0-9])", re.ASCII)
_RC = re.compile(r"RC-([1-9][0-9]?)", re.ASCII)


def stage_rank(stage: str) -> int:
    if stage == "FINAL":
        return 1000
    match = _DEV.fullmatch(stage)
    if match is not None:
        return int(match.group(1))
    match = _RC.fullmatch(stage)
    if match is not None:
        return 100 + int(match.group(1))
    raise ValueError(f"unknown stage: {stage!r}")
