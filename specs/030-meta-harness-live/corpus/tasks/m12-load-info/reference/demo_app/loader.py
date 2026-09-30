"""Load the version info JSON."""

from __future__ import annotations

import json


class InfoError(ValueError):
    """The text is not a usable version info document."""


def load_info(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError as error:
        raise InfoError(f"invalid info: not valid JSON ({error})") from error
    if not isinstance(data, dict):
        raise InfoError("invalid info: top level must be an object")
    version = data.get("package_version")
    if not isinstance(version, str) or version == "":
        raise InfoError("invalid info: package_version must be a non-empty string")
    if not isinstance(data.get("stage"), str):
        raise InfoError("invalid info: stage must be a string")
    return data
