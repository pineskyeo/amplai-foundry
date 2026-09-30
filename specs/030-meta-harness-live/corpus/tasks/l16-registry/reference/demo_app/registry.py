"""A JSON-file backed registry of package versions."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Union

_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class RegistryError(ValueError):
    """The registry file is not valid registry data."""


def _key(version: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"not a MAJOR.MINOR.PATCH version: {version!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def _parse(text: str) -> dict[str, list[str]]:
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise RegistryError(f"not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise RegistryError("registry file must hold a JSON object")
    data: dict[str, list[str]] = {}
    for name, items in raw.items():
        if not isinstance(items, list) or not items:
            raise RegistryError(f"{name!r}: expected a non-empty list of versions")
        keys = []
        for item in items:
            if not isinstance(item, str):
                raise RegistryError(f"{name!r}: version {item!r} is not a string")
            try:
                keys.append(_key(item))
            except ValueError as exc:
                raise RegistryError(f"{name!r}: {exc}") from exc
        if any(a >= b for a, b in zip(keys, keys[1:])):
            raise RegistryError(f"{name!r}: versions are not strictly ascending")
        data[name] = list(items)
    return data


class Registry:
    def __init__(self, path: Union[Path, str]) -> None:
        self._path = Path(path)
        self._data: dict[str, list[str]] = {}
        if self._path.exists():
            try:
                text = self._path.read_bytes().decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RegistryError(f"not valid UTF-8: {exc}") from exc
            self._data = _parse(text)

    def _save(self) -> None:
        text = json.dumps(self._data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        self._path.write_bytes(text.encode("utf-8"))

    def add(self, name: str, version: str) -> None:
        _key(version)
        current = self._data.get(name, [])
        if version in current:
            raise ValueError(f"{name} {version} is already recorded")
        self._data[name] = sorted([*current, version], key=_key)
        self._save()

    def versions(self, name: str) -> list[str]:
        return list(self._data.get(name, []))

    def latest(self, name: str) -> str | None:
        found = self._data.get(name)
        return found[-1] if found else None

    def names(self) -> list[str]:
        return sorted(self._data)

    def remove(self, name: str, version: str) -> None:
        current = self._data.get(name)
        if current is None or version not in current:
            raise KeyError((name, version))
        current.remove(version)
        if not current:
            del self._data[name]
        self._save()
