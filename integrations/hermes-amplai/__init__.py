"""Hermes plugin for the narrow AMPLAI orchestration boundary.

The module is deliberately standard-library only.  Hermes owns conversation;
AMPLAI owns authority and durable Work state.  No handler can execute a shell,
write a repository, or activate a Work.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TOOLS: dict[str, dict[str, Any]] = {
    "amplai_submit_request": {
        "permission": "orchestration.request.submit",
        "method": "POST",
        "path": "/orchestration-requests",
        "required": ("controller", "goal", "reply_route"),
    },
    "amplai_get_request": {
        "permission": "orchestration.request.read",
        "method": "GET",
        "path": "/orchestration-requests/{request_id}",
        "required": ("request_id",),
    },
    "amplai_get_work": {
        "permission": "orchestration.work.read",
        "method": "GET",
        "path": "/work/{work_id}",
        "required": ("work_id",),
    },
    "amplai_list_pending_actions": {
        "permission": "orchestration.work.read",
        "method": "GET",
        "path": "/pending-actions",
        "required": (),
    },
    "amplai_request_knowledge_intake": {
        "permission": "orchestration.request.submit",
        "method": "POST",
        "path": "/knowledge-intake-requests",
        "required": ("source_text", "reply_route"),
    },
}

_SCHEMAS: dict[str, dict[str, Any]] = {
    "amplai_submit_request": {
        "type": "object",
        "properties": {
            "controller": {"type": "string", "enum": ["status", "design", "work"]},
            "goal": {"type": "string", "minLength": 1},
            "reply_route": {
                "type": "object",
                "properties": {
                    "provider": {"const": "slack"},
                    "workspace_id": {"type": "string"},
                    "channel_id": {"type": "string"},
                    "thread_id": {"type": ["string", "null"]},
                },
                "required": ["provider", "workspace_id", "channel_id"],
                "additionalProperties": False,
            },
            "runner_hint": {"type": ["string", "null"], "enum": ["claude-code", "codex", None]},
            "target_app_hint": {"type": ["string", "null"]},
            "project_hint": {"type": ["string", "null"]},
            "artifact_refs": {"type": "array", "items": {"type": "string"}},
            "request_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["controller", "goal", "reply_route"],
        "additionalProperties": False,
    },
    "amplai_get_request": {
        "type": "object",
        "properties": {"request_id": {"type": "string"}},
        "required": ["request_id"],
        "additionalProperties": False,
    },
    "amplai_get_work": {
        "type": "object",
        "properties": {"work_id": {"type": "string"}},
        "required": ["work_id"],
        "additionalProperties": False,
    },
    "amplai_list_pending_actions": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "amplai_request_knowledge_intake": {
        "type": "object",
        "properties": {
            "source_text": {"type": "string", "minLength": 1},
            "reply_route": {"type": "object"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["source_text", "reply_route"],
        "additionalProperties": False,
    },
}


class AmplaiClient:
    """Fixed-endpoint loopback HTTP client; values never become shell arguments."""

    def __init__(self, base_url: str, bearer_token: str, project_id: str) -> None:
        if not base_url.startswith("http://127.0.0.1:") and not base_url.startswith(
            "https://127.0.0.1:"
        ):
            raise ValueError("AMPLAI_BASE_URL must be loopback")
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.project_id = project_id

    def call(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        tool = TOOLS[tool_name]
        for field in tool["required"]:
            if field not in payload:
                return {"ok": False, "error": f"{field}_REQUIRED"}
        path = str(tool["path"])
        for field in ("request_id", "work_id"):
            if "{" + field + "}" in path:
                value = payload.get(field)
                if not isinstance(value, str) or not value:
                    return {"ok": False, "error": f"{field}_REQUIRED"}
                path = path.replace("{" + field + "}", value)
        url = self.base_url + "/v1/projects/" + self.project_id + path
        body = None
        headers = {"Authorization": "Bearer " + self.bearer_token, "Accept": "application/json"}
        if tool["method"] == "POST":
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Idempotency-Key"] = str(payload.get("idempotency_key") or "")
        request = Request(url, data=body, headers=headers, method=str(tool["method"]))
        try:
            with urlopen(request, timeout=10) as response:  # nosec B310 -- loopback asserted above
                return {"ok": True, "status": response.status, "data": json.loads(response.read())}
        except HTTPError as error:
            return {"ok": False, "status": error.code, "error": error.read().decode("utf-8")}
        except URLError:
            return {"ok": False, "error": "AMPLAI_UNAVAILABLE"}


def verify_profile(path: Path) -> None:
    """Validate the shipped JSON-compatible YAML without a PyYAML dependency."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("feature_flags", {}).get("enabled") is not False:
        raise ValueError("Hermes AMPLAI profile must ship disabled")
    toolsets = value.get("toolsets") or {}
    if toolsets.get("enabled") != ["amplai"]:
        raise ValueError("only the amplai toolset may be enabled")
    required_disabled = {"terminal", "file", "code_execution", "delegation", "mcp"}
    if not required_disabled.issubset(set(toolsets.get("disabled") or [])):
        raise ValueError("execution-capable Hermes toolsets must be disabled")


def register(ctx: Any) -> None:
    """Register fixed-schema tool handlers with Hermes' plugin context."""
    client = AmplaiClient(
        ctx.env("AMPLAI_BASE_URL"),
        ctx.env("AMPLAI_BEARER_TOKEN"),
        ctx.env("AMPLAI_PROJECT_ID"),
    )
    for name in TOOLS:
        schema = _SCHEMAS[name]
        ctx.register_tool(
            name,
            "amplai",
            schema,
            lambda payload, tool_name=name: json.dumps(client.call(tool_name, payload)),
        )
