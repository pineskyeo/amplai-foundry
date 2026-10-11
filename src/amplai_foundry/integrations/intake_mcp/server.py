"""AMPLAI intake MCP server: the first ``intake_adapter`` (Work 034 S1c; spec F-1, F-4, M-5, H-1).

A separate process the front agent launches over stdio. It holds the adapter's token, which only
ever lives in this process's environment and never in a tool argument or result, so the front
agent's model never sees it (design/10 §6). Each tool of the versioned contract
(``contract.json``) is one call to ``/api/v3/intake/<adapter_id>``: this process has no other
route, no store, no key and no AMPLAI service object. It does not run inside the Control Plane
(M-5): it only speaks HTTP to it, and the server decides every permission (the intake actor,
identity map, permission ceiling and rate limits of S1a/S1c).

Environment (all required):

    AMPLAI_INTAKE_URL      base URL of the AMPLAI API, e.g. http://127.0.0.1:8765
    AMPLAI_INTAKE_ADAPTER  the adapter id configured in ``intake.adapters[]``
    AMPLAI_INTAKE_TOKEN    that adapter's bearer token (removed from the environment once read)

Run: ``python -m amplai_foundry.integrations.intake_mcp``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import anyio
import httpx
import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .contract import CONTRACT_VERSION, ContractViolation, ToolContract, load

ENV_URL = "AMPLAI_INTAKE_URL"
ENV_ADAPTER = "AMPLAI_INTAKE_ADAPTER"
ENV_TOKEN = "AMPLAI_INTAKE_TOKEN"
SERVER_NAME = "amplai-intake"
_ADAPTER_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class ToolFailure(Exception):
    """A tool call that did not succeed: an AMPLAI error code and its message, never a token."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message

    def wire(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class Settings:
    url: str
    adapter_id: str
    token: str

    def __repr__(self) -> str:  # the token never reaches a log or a traceback
        return f"Settings(url={self.url!r}, adapter_id={self.adapter_id!r}, token=<hidden>)"

    @classmethod
    def from_environment(cls, environ: MutableMapping[str, str]) -> Settings:
        missing = [k for k in (ENV_URL, ENV_ADAPTER, ENV_TOKEN) if not environ.get(k)]
        if missing:
            raise SystemExit("amplai-intake: set " + ", ".join(missing))
        url = environ[ENV_URL].rstrip("/")
        if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).hostname:
            raise SystemExit(f"amplai-intake: {ENV_URL} is not an http(s) URL")
        adapter_id = environ[ENV_ADAPTER]
        if not _ADAPTER_ID.fullmatch(adapter_id):
            raise SystemExit(f"amplai-intake: {ENV_ADAPTER} is not an adapter id")
        token = environ.pop(ENV_TOKEN).strip()
        if len(token) < 32:
            raise SystemExit(f"amplai-intake: {ENV_TOKEN} needs 32+ characters")
        return cls(url, adapter_id, token)


class IntakeCaller:
    """Maps one contract tool call to one intake route call."""

    def __init__(self, settings: Settings, http: httpx.Client) -> None:
        self.settings, self.http = settings, http
        self.tools = load()

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self.tools.get(name)
        if tool is None:
            raise ToolFailure(
                "UNKNOWN_TOOL", "No such tool in contract version " + CONTRACT_VERSION
            )
        try:
            tool.check_input(arguments)
        except ContractViolation as exc:
            raise ToolFailure("INVALID_ARGUMENTS", str(exc)) from None
        response = self._request(tool, dict(arguments))
        try:
            body = response.json()
        except ValueError:
            raise ToolFailure(
                "AMPLAI_RESPONSE", f"AMPLAI answered HTTP {response.status_code}"
            ) from None
        if not isinstance(body, dict):
            raise ToolFailure("AMPLAI_RESPONSE", f"AMPLAI answered HTTP {response.status_code}")
        if tool.name == "pending_events" and body.get("code") == "CURSOR_EXPIRED":
            body = {"items": [], "snapshot": body.get("snapshot"), "cursor": body.get("cursor")}
            body["expired"] = True
        elif response.status_code >= 400:
            raise _failure(response.status_code, body)
        try:
            tool.check_output(body)
        except ContractViolation:
            raise ToolFailure(
                "CONTRACT_OUTPUT", "AMPLAI's answer is outside the tool contract"
            ) from None
        return body

    def _request(self, tool: ToolContract, arguments: dict[str, Any]) -> httpx.Response:
        path = tool.path
        if "{goal_id}" in path:
            path = path.replace("{goal_id}", quote(arguments.pop("goal_id"), safe=""))
        url = f"/api/v3/intake/{self.settings.adapter_id}{path}"
        headers = {"Authorization": "Bearer " + self.settings.token}
        try:
            if tool.method == "GET":
                return self.http.get(url, params=arguments, headers=headers)
            if tool.mode is not None:
                arguments["mode"] = tool.mode
            return self.http.post(url, json=arguments, headers=headers)
        except httpx.HTTPError as exc:
            raise ToolFailure("AMPLAI_UNREACHABLE", type(exc).__name__) from None


def _failure(status: int, body: dict[str, Any]) -> ToolFailure:
    code = body.get("code")
    message = body.get("message")
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        code = f"HTTP_{status}"
    if not isinstance(message, str):
        message = f"AMPLAI answered HTTP {status}"
    return ToolFailure(code, message[:500])


def build_server(caller: IntakeCaller) -> Server[Any]:
    """The MCP server over ``caller``: the contract's tools, exactly."""
    listed = [
        types.Tool(
            name=t.name,
            description=t.description,
            input_schema=t.input_schema,
            output_schema=t.output_schema,
        )
        for t in caller.tools.values()
    ]

    async def list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=listed)

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        arguments = dict(params.arguments or {})
        try:
            result = await anyio.to_thread.run_sync(caller.call, params.name, arguments)
        except ToolFailure as failure:
            text = json.dumps(failure.wire(), ensure_ascii=False)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=text)], is_error=True
            )
        text = json.dumps(result, ensure_ascii=False)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], structured_content=result
        )

    def input_schema(name: str) -> dict[str, Any] | None:
        tool = caller.tools.get(name)
        return None if tool is None else tool.input_schema

    return Server(
        SERVER_NAME,
        version=CONTRACT_VERSION,
        get_tool_input_schema=input_schema,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def main() -> None:
    settings = Settings.from_environment(os.environ)
    http = httpx.Client(base_url=settings.url, timeout=30.0, follow_redirects=False)
    server = build_server(IntakeCaller(settings, http))

    async def serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    try:
        anyio.run(serve)
    finally:
        http.close()
