"""Strict JSON and exact observation binding at the independent evidence boundary."""
from __future__ import annotations

import json

from amplai_foundry.runtime.errors import RuntimeFault, Hold


def read_receipt(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise ValueError('duplicate key')
            result[k] = v
        return result

    def invalid(_):
        raise ValueError('nonfinite JSON constant')

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
        if not isinstance(value, dict):
            raise ValueError('receipt is not an object')
        return value
    except (ValueError, UnicodeError, TypeError) as exc:
        raise RuntimeFault('OBSERVATION_JSON', 'Malformed, duplicate-key or nonfinite observation JSON') from exc


def check_observation(value: dict, expected: dict) -> None:
    for key, required in expected.items():
        actual = value.get(key)
        if key not in value or type(actual) is not type(required) or actual != required:
            raise Hold('OBSERVATION_BINDING', 'Independent evidence does not bind field: ' + key)
