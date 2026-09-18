"""Approved design schemas, pinned offline. Unknown schema and references fail closed."""
from __future__ import annotations
import json
from importlib.resources import files
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource, Unresolvable
from ..errors import RuntimeFault

BASE = "https://schemas.amplai.invalid/v3/"

def _offline(uri: str):
    raise NoSuchResource(ref=uri)

class Contracts:
    def __init__(self):
        root = files(__package__) / "data"
        self.definitions = {p.name.removesuffix(".schema.json"): json.loads(p.read_text())
                            for p in (root / "schemas").iterdir() if p.name.endswith(".schema.json")}
        self.registry = Registry(retrieve=_offline).with_resources(
            (s["$id"], Resource.from_contents(s)) for s in self.definitions.values())
        for schema in self.definitions.values():
            Draft202012Validator.check_schema(schema)
        self.states = json.loads((root / "state-machines.json").read_text())
        self.gates = json.loads((root / "gate-matrix.json").read_text())
        self.defaults = json.loads((root / "runtime-defaults.json").read_text())

    def validate(self, name: str, payload: dict) -> dict:
        if name not in self.definitions:
            raise RuntimeFault("UNKNOWN_SCHEMA", name)
        try:
            errors = sorted(Draft202012Validator(self.definitions[name], registry=self.registry,
                            format_checker=FormatChecker()).iter_errors(payload), key=lambda x: str(list(x.path)))
        except Unresolvable as exc:
            raise RuntimeFault('SCHEMA_REFERENCE', 'Only pinned offline schema references are allowed') from exc
        if errors:
            raise RuntimeFault("SCHEMA_INVALID", name, details=[{"path": list(e.path), "message": e.message} for e in errors])
        return payload


def strict_json_loads(raw: bytes | str):
    """Decode untrusted JSON without duplicate keys, NaN/Infinity or overflow."""
    import math
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise RuntimeFault("JSON_DUPLICATE_KEY", "Repeated JSON object key")
            result[key] = value
        return result
    def constant(_):
        raise RuntimeFault("JSON_NONFINITE", "Non-finite JSON number")
    def number(value):
        result = float(value)
        if not math.isfinite(result): constant(value)
        return result
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    from .identity import canonical
    canonical(value)
    return value
