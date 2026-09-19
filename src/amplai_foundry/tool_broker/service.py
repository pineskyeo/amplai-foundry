"""Typed server-owned tools and non-exportable secret handles."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from amplai_foundry.runtime.contracts.identity import canonical
from amplai_foundry.runtime.errors import Hold, RuntimeFault


@dataclass(frozen=True)
class ToolResult:
    outcome: str
    operation_id: str | None
    value: object


@dataclass(frozen=True)
class Tool:
    tool_id: str
    ref: dict[str, Any]
    action: str
    resource: str
    effect_class: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    invoke: Callable[..., Any]
    reconcile: Callable[..., Any] | None = None
    timeout_seconds: int = 30
    max_result_bytes: int = 1024 * 1024


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.tool_id in self.tools:
            raise RuntimeFault("TOOL_DUPLICATE", "Tool IDs are immutable within a runtime")
        if (
            not callable(tool.invoke)
            or not 0 < tool.timeout_seconds <= 3600
            or not 1 <= tool.max_result_bytes <= 16 * 1024 * 1024
        ):
            raise RuntimeFault("TOOL_LIMITS", "A bounded callable adapter is required")
        Draft202012Validator.check_schema(tool.input_schema)
        Draft202012Validator.check_schema(tool.output_schema)
        if tool.effect_class not in {
            "pure_read",
            "sandbox_write",
            "external_idempotent_write",
            "external_nonidempotent_write",
            "production_control",
        }:
            raise RuntimeFault("TOOL_EFFECT_CLASS", "Tool needs an explicit supported effect class")
        self.tools[tool.tool_id] = tool

    def get(self, ref: dict[str, Any]) -> Tool:
        tool = self.tools.get(ref["id"])
        if not tool or tool.ref != ref:
            raise Hold(
                "TOOL_NOT_PINNED", "No server-owned implementation matches the exact tool reference"
            )
        return tool

    def validate_input(self, tool: Tool, args: object) -> None:
        errors = list(Draft202012Validator(tool.input_schema).iter_errors(args))
        if errors:
            raise RuntimeFault("TOOL_INPUT_SCHEMA", "Tool argument schema violation")

    def validate_result(self, tool: Tool, result: ToolResult) -> None:
        if not isinstance(result, ToolResult) or result.outcome not in {"applied", "not_applied"}:
            raise Hold("TOOL_OUTCOME_UNKNOWN", "Tool adapter did not establish an external outcome")
        if len(canonical(result.value)) > tool.max_result_bytes:
            raise Hold("TOOL_RESULT_SIZE", "Tool result exceeds admission limit")
        if list(Draft202012Validator(tool.output_schema).iter_errors(result.value)):
            raise Hold(
                "TOOL_RESULT_SCHEMA", "Tool result cannot be interpreted under the pinned schema"
            )


class SecretBroker:
    def __init__(
        self,
        values: dict[str, str],
        *,
        allowed_hosts: dict[str, set[str]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._values = dict(values)
        self._hosts = allowed_hosts
        self._handles: dict[str, tuple[str, str, float, object]] = {}
        self.clock = clock

    def issue_handle(
        self, name: str, host: str, *, ttl_seconds: float = 300, scope: object = None
    ) -> str:
        if not 0 < ttl_seconds <= 3600:
            raise Hold("SECRET_TTL", "Secret handles need a bounded lifetime")
        if name not in self._values or host not in self._hosts.get(name, set()):
            raise Hold("SECRET_SCOPE", "Secret cannot be used for this endpoint")
        handle = "secret-handle-" + secrets.token_hex(16)
        self._handles[handle] = (name, host, self.clock() + ttl_seconds, scope)
        return handle

    def with_secret(
        self, handle: str, url: str, operation: Callable[[str], Any], *, scope: object = None
    ) -> Any:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
            raise RuntimeFault(
                "SECRET_ENDPOINT", "Secret delivery requires an exact HTTPS service endpoint"
            )
        binding = self._handles.get(handle)
        if (
            not binding
            or parsed.hostname != binding[1]
            or parsed.port not in {None, 443}
            or self.clock() >= binding[2]
            or scope != binding[3]
        ):
            raise Hold("SECRET_SCOPE", "Secret handle is not bound to this host")
        return operation(self._values[binding[0]])

    def revoke(self, handle: str) -> None:
        self._handles.pop(handle, None)

    def __repr__(self) -> str:
        return "<SecretBroker values=REDACTED>"
