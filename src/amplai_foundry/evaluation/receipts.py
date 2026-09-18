"""Strict JSON and exact observation binding at the independent evidence boundary."""

from __future__ import annotations

import json
from typing import Any

from amplai_foundry.runtime.errors import Hold, RuntimeFault


def read_receipt(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for k, v in items:
            if k in result:
                raise ValueError("duplicate key")
            result[k] = v
        return result

    def invalid(_: str) -> Any:
        raise ValueError("nonfinite JSON constant")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
        if not isinstance(value, dict):
            raise ValueError("receipt is not an object")
        return value
    except (ValueError, UnicodeError, TypeError) as exc:
        raise RuntimeFault(
            "OBSERVATION_JSON", "Malformed, duplicate-key or nonfinite observation JSON"
        ) from exc


def check_observation(value: dict[str, Any], expected: dict[str, Any]) -> None:
    for key, required in expected.items():
        actual = value.get(key)
        if key not in value or type(actual) is not type(required) or actual != required:
            raise Hold("OBSERVATION_BINDING", "Independent evidence does not bind field: " + key)
