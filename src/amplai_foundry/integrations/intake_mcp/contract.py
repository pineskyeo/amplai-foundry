"""The versioned intake tool contract (Work 034 S1c, spec F-4).

``contract.json`` fixes each tool's name, input JSON Schema, output JSON Schema and the one
``/api/v3/intake/{adapter_id}`` route it maps to. It does not name a front agent: another agent
uses the same contract through the same MCP server with only its kit changed, and an agent that
cannot speak MCP gets another ``intake_adapter`` implementing this contract (F-4).

Versioning (``major.minor``, also written in ``contract.json``):

- ``1.0`` is the eight tools over the S1a intake routes.
- Adding a tool is an additive minor (``1.x``); each tool's ``since`` names the version that added
  it. A client built for ``1.y`` keeps working with ``1.x`` for any ``x >= y``.
- Renaming or removing a tool, or changing any input or output schema, is a new major.
- A front-agent kit pins the version it was checked against and includes only the tools with
  ``since`` at or below it.

The conformance test pins this version's tool names and schemas.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

CONTRACT_ID = "amplai-intake-tools"
CONTRACT_VERSION = "1.0"
CONTRACT_MAJOR = 1


def version_tuple(version: str) -> tuple[int, int]:
    """``"1.2"`` -> ``(1, 2)``; anything else is not a contract version."""
    major, _, minor = version.partition(".")
    if not (major.isascii() and major.isdigit() and minor.isascii() and minor.isdigit()):
        raise ValueError("A tool contract version is <major>.<minor>")
    return int(major), int(minor)


@dataclass(frozen=True)
class ToolContract:
    name: str
    since: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    method: str
    path: str
    mode: str | None

    def check_input(self, arguments: dict[str, Any]) -> None:
        _validate(self.input_schema, arguments)

    def check_output(self, value: Any) -> None:
        _validate(self.output_schema, value)


class ContractViolation(ValueError):
    """An input or output outside the tool's schema. Carries field paths, never values."""


def _validate(schema: dict[str, Any], value: Any) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
    if errors:
        fields = sorted({"/".join(str(p) for p in e.path) or "(root)" for e in errors})
        raise ContractViolation("Outside the tool schema at: " + ", ".join(fields))


def raw() -> dict[str, Any]:
    text = resources.files(__package__).joinpath("contract.json").read_text(encoding="utf-8")
    value: dict[str, Any] = json.loads(text)
    return value


def load() -> dict[str, ToolContract]:
    document = raw()
    if document.get("contract") != CONTRACT_ID or document.get("version") != CONTRACT_VERSION:
        raise ValueError("Unknown intake tool contract")
    current = version_tuple(CONTRACT_VERSION)
    tools: dict[str, ToolContract] = {}
    for item in document["tools"]:
        since = version_tuple(item["since"])
        if since[0] != current[0] or since > current:
            raise ValueError(f"{item['name']}: since is outside contract {CONTRACT_VERSION}")
        for schema in (item["input_schema"], item["output_schema"]):
            Draft202012Validator.check_schema(schema)
        route = item["route"]
        tools[item["name"]] = ToolContract(
            name=item["name"],
            since=item["since"],
            description=item["description"],
            input_schema=item["input_schema"],
            output_schema=item["output_schema"],
            method=route["method"],
            path=route["path"],
            mode=route.get("mode"),
        )
    return tools
