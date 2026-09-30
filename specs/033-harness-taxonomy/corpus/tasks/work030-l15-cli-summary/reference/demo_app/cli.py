"""Command-line entry point that prints the one-line version summary."""

from __future__ import annotations

import json
from typing import Any, TextIO

from demo_app.version_report import summarize

_OPTIONS = ("--upper", "--json")


class _InputError(Exception):
    pass


def _parse_options(argv: list[str]) -> set[str]:
    chosen: set[str] = set()
    for arg in argv:
        if arg not in _OPTIONS:
            raise _InputError("usage")
        chosen.add(arg)
    return chosen


def _load_info(text: str) -> dict[str, Any]:
    try:
        info = json.loads(text)
    except ValueError as exc:
        raise _InputError(f"invalid JSON: {' '.join(str(exc).split())}") from exc
    if not isinstance(info, dict):
        raise _InputError("input must be a JSON object")
    version = info.get("package_version")
    if not isinstance(version, str) or not version:
        raise _InputError("package_version must be a non-empty string")
    if not isinstance(info.get("stage"), str):
        raise _InputError("stage must be a string")
    return info


def _render(info: dict[str, Any], options: set[str]) -> str:
    line = summarize(info)
    if "--upper" in options:
        line = line.upper()
    if "--json" in options:
        line = json.dumps({"summary": line}, separators=(",", ":"))
    return line + "\n"


def main(argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    try:
        options = _parse_options(argv)
        info = _load_info(stdin.read())
    except _InputError as exc:
        stderr.write(f"error: {exc}\n")
        return 2
    stdout.write(_render(info, options))
    return 0
