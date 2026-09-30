"""Compare two version-info dicts and render the difference."""

from __future__ import annotations

from typing import Any


def _check_strings(info: dict[str, Any], label: str) -> None:
    for key, value in info.items():
        if not isinstance(value, str):
            raise TypeError(f"{label}[{key!r}] must be a str, got {type(value).__name__}")


def _change(key: str, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any] | None:
    if key not in old:
        return {"key": key, "kind": "added", "old": None, "new": new[key]}
    if key not in new:
        return {"key": key, "kind": "removed", "old": old[key], "new": None}
    if old[key] != new[key]:
        return {"key": key, "kind": "changed", "old": old[key], "new": new[key]}
    return None


def diff_infos(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    _check_strings(old, "old")
    _check_strings(new, "new")
    changes = []
    for key in sorted(set(old) | set(new)):
        change = _change(key, old, new)
        if change is not None:
            changes.append(change)
    return changes


def _line(change: dict[str, Any]) -> str:
    kind = change["kind"]
    if kind == "added":
        return f"+ {change['key']}: {change['new']}"
    if kind == "removed":
        return f"- {change['key']}: {change['old']}"
    return f"~ {change['key']}: {change['old']} -> {change['new']}"


def format_diff(changes: list[dict[str, Any]]) -> str:
    return "\n".join(_line(change) for change in changes)
