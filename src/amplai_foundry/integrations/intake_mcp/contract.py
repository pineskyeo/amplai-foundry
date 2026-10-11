"""The versioned intake tool contract (Work 034 S1c, spec F-4).

``contract.json`` fixes each tool's name, input JSON Schema, output JSON Schema and the one
``/api/v3/intake/{adapter_id}`` route it maps to. It does not name a front agent: another agent
uses the same contract through the same MCP server with only its kit changed, and an agent that
cannot speak MCP gets another ``intake_adapter`` implementing this contract (F-4).

A change to a tool name or schema is a new ``version``; the conformance test pins this one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

CONTRACT_ID = "amplai-intake-tools"
CONTRACT_VERSION = "1"


@dataclass(frozen=True)
class ToolContract:
    name: str
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
    tools: dict[str, ToolContract] = {}
    for item in document["tools"]:
        for schema in (item["input_schema"], item["output_schema"]):
            Draft202012Validator.check_schema(schema)
        route = item["route"]
        tools[item["name"]] = ToolContract(
            name=item["name"],
            description=item["description"],
            input_schema=item["input_schema"],
            output_schema=item["output_schema"],
            method=route["method"],
            path=route["path"],
            mode=route.get("mode"),
        )
    return tools
