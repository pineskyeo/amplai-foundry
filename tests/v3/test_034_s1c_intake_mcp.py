"""Work 034 S1c — the intake MCP server and its versioned tool contract (spec F-1, F-4, AC-H9).

The contract (``integrations/intake_mcp/contract.json``) fixes tool names and input/output JSON
schemas; each tool is one ``/api/v3/intake/{adapter_id}`` route. A stub MCP client drives the
server without any front agent (AC-H9 style): message -> goal -> drafted contract -> operator
approval (operator route, not a tool) -> notifications -> cancel. The token is only in the MCP
server's settings (from its environment), never in a tool's arguments or results.

Real: the MCP SDK server and client (in process, and once over stdio in a subprocess),
LocalProductDeployment with an intake adapter, the intake routes. Stand-in: the planner returns a
fixed draft; the MCP server's HTTP client is wired to the in-process API.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from test_034_s1a_intake_route import (
    HERMES_TOKEN,
    SLACK_USER,
    init_home,
    settle,
    with_intake,
)

from amplai_foundry.control_plane.api_v3 import intake as intake_api
from amplai_foundry.integrations.intake_mcp.contract import CONTRACT_VERSION, load, raw
from amplai_foundry.integrations.intake_mcp.server import (
    IntakeCaller,
    Settings,
    build_server,
)
from amplai_foundry.runtime.local_deployment import LocalProductDeployment
from rc06_rig import FixedPlanner
from testclient_bridge import forwarding_transport

TOOLS = {
    "submit_work", "submit_design", "list_goals", "goal_status", "steer", "replan",
    "cancel_goal", "pending_events",
}  # fmt: skip
USER = {**SLACK_USER, "workspace_id": "T1"}


# -- the contract (F-4) --------------------------------------------------------------------------


def test_the_contract_is_version_1_with_the_v1_tools() -> None:
    document = raw()
    assert (document["contract"], document["version"]) == ("amplai-intake-tools", "1")
    assert set(load()) == TOOLS
    for tool in document["tools"]:
        assert tool["input_schema"]["additionalProperties"] is False
        assert tool["output_schema"]["additionalProperties"] is False
        for word in ("hermes", "slack", "telegram"):  # front-agent neutral (H-11)
            assert word not in json.dumps(tool).lower()


def test_no_tool_approves_grants_or_names_an_actor() -> None:
    for tool in load().values():
        assert "approve" not in tool.name and "grant" not in tool.name
        for field in ("actor", "subject_id", "role", "target_hints", "classification"):
            assert field not in tool.input_schema["properties"]


def _models() -> dict[str, Any]:
    return {
        "POST ": intake_api.SubmitBody,
        "POST /goals/{goal_id}/steer": intake_api.SteerBody,
        "POST /goals/{goal_id}/replan": intake_api.ReplanBody,
        "POST /goals/{goal_id}/cancel": intake_api.CancelBody,
    }


def test_each_tool_matches_its_intake_route() -> None:
    """Conformance of the contract to the server routes it calls."""
    models = _models()
    for tool in load().values():
        fields = set(tool.input_schema["properties"]) - {"goal_id"}
        if tool.method == "POST":
            model = models[f"POST {tool.path}"]
            assert fields | ({"mode"} if tool.mode else set()) <= set(model.model_fields), tool.name
            required = {n for n, f in model.model_fields.items() if f.is_required()}
            assert required <= fields, tool.name
        else:
            assert fields <= {"provider", "workspace_id", "user_id", "cursor", "limit"}
    source = inspect.getsource(intake_api.intake_router)
    for tool in load().values():
        decorator = f'@router.{tool.method.lower()}("{tool.path}"'
        assert decorator in source, tool.name


# -- a stub client drives the MCP server (AC-H9 style) -------------------------------------------


@pytest.fixture
def product(tmp_path: Path) -> Any:
    home = init_home(tmp_path)
    with_intake(home)
    dep = LocalProductDeployment(home / "local.json", start_loop=False)
    for installed in dep.service.apps.values():
        installed.planners["codex-cli"] = FixedPlanner()
    operator = (home / "operator.token").read_text().strip()
    with TestClient(dep.app) as client:
        yield dep, client, operator
    settle(dep)
    dep.close()


def caller(client: TestClient, token: str = HERMES_TOKEN) -> IntakeCaller:
    """The MCP server's real ``httpx.Client``, its requests answered by the in-process API."""
    http = httpx.Client(transport=forwarding_transport(client), base_url="http://testserver")
    return IntakeCaller(Settings("http://testserver", "hermes", token), http)


def run(steps: Any) -> Any:
    return anyio.run(steps)


def structured(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    value: dict[str, Any] = result.structured_content
    assert json.loads(result.content[0].text) == value
    return value


def error(result: Any) -> dict[str, Any]:
    assert result.is_error
    value: dict[str, Any] = json.loads(result.content[0].text)
    return value


def test_the_server_lists_exactly_the_contract(product: Any) -> None:
    _dep, client, _operator = product

    async def steps() -> Any:
        async with Client(build_server(caller(client))) as mcp:
            return (await mcp.list_tools()).tools

    tools = run(steps)
    contract = load()
    assert {t.name for t in tools} == set(contract)
    for tool in tools:
        assert tool.input_schema == contract[tool.name].input_schema
        assert tool.output_schema == contract[tool.name].output_schema


def test_a_stub_client_drives_the_intake_flow_without_a_front_agent(product: Any) -> None:
    dep, client, operator = product
    server = build_server(caller(client))
    message = {**USER, "channel_id": "C1", "message_id": "m-1"}
    seen: list[Any] = []

    async def submit() -> dict[str, Any]:
        async with Client(server) as mcp:
            first = await mcp.call_tool("submit_work", {**message, "text": "make value return 2"})
            again = await mcp.call_tool("submit_work", {**message, "text": "make value return 2"})
            seen.extend([first, again])
            assert structured(first)["goal_id"] == structured(again)["goal_id"]  # H-7
            return structured(first)

    goal = run(submit)["goal_id"]

    async def status() -> dict[str, Any]:
        async with Client(server) as mcp:
            result = await mcp.call_tool("goal_status", {**USER, "goal_id": goal})
            seen.append(result)
            return structured(result)

    deadline = time.time() + 20
    while run(status)["status"] == "planning" and time.time() < deadline:
        time.sleep(0.05)
    assert run(status)["status"] == "awaiting_approval"
    # approval is the operator's, on the operator route with the contract AMPLAI shows (H-13)
    plan = client.get(
        f"/api/v3/local/goals/{goal}", headers={"Authorization": "Bearer " + operator}
    )
    approved = client.post(
        f"/api/v3/local/goals/{goal}/approve",
        headers={"Authorization": "Bearer " + operator},
        json={"expected_contract_ref": plan.json()["contract_ref"]},
    )
    assert approved.json()["status"] == "approved"

    async def follow() -> None:
        async with Client(server) as mcp:
            start = structured(await mcp.call_tool("pending_events", USER))
            assert start["items"] == [] and start["snapshot"][0]["status"] == "approved"
            incarnation = start["cursor"].rsplit(":", 1)[0]
            feed = structured(
                await mcp.call_tool("pending_events", {**USER, "cursor": incarnation + ":0"})
            )
            assert goal in {item["goal_id"] for item in feed["items"]}
            listed = structured(await mcp.call_tool("list_goals", USER))
            assert [g["goal_id"] for g in listed["items"]] == [goal]
            stop = await mcp.call_tool(
                "cancel_goal",
                {**message, "message_id": "m-stop", "goal_id": goal, "reason": "operator asked"},
            )
            seen.append(stop)
            assert structured(stop)["goal_id"] == goal
            expired = structured(
                await mcp.call_tool("pending_events", {**USER, "cursor": "other-incarnation:1"})
            )
            assert expired["expired"] is True and expired["items"] == []

    run(follow)
    stops = [e for e in dep.store.events(dep.scope, limit=1000)
             if e["event_type"] == "intake.stop_requested"]  # fmt: skip
    assert stops[-1]["data"]["actor"]["authn_context_ref"] == "intake:hermes"
    # the token never appears in what the front agent's model sees
    for result in seen:
        assert HERMES_TOKEN not in result.model_dump_json()


def test_bad_arguments_never_reach_amplai(product: Any) -> None:
    _dep, client, _operator = product
    calls: list[str] = []
    real = caller(client)
    original = real.http.request

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(str(args[1]))
        return original(*args, **kwargs)

    real.http.request = counted  # type: ignore[method-assign]
    message = {**USER, "channel_id": "C1", "message_id": "m-1"}

    async def steps() -> list[dict[str, Any]]:
        async with Client(build_server(real)) as mcp:
            return [
                error(
                    await mcp.call_tool("submit_work", {**message, "text": "x", "actor": "admin"})
                ),
                error(await mcp.call_tool("submit_work", {**message})),
                error(await mcp.call_tool("goal_status", {**USER, "goal_id": "../../local/goals"})),
                error(await mcp.call_tool("approve", {**USER, "goal_id": "g-1"})),
            ]

    codes = [e["code"] for e in run(steps)]
    assert codes == ["INVALID_ARGUMENTS"] * 3 + ["UNKNOWN_TOOL"]
    assert calls == []


def test_amplai_refusals_come_back_as_tool_errors(product: Any) -> None:
    _dep, client, _operator = product
    message = {**USER, "channel_id": "C1", "message_id": "m-1", "text": "x"}

    async def steps() -> list[dict[str, Any]]:
        async with Client(build_server(caller(client, "wrong-token-" + "w" * 40))) as wrong:
            denied = error(await wrong.call_tool("submit_work", message))
        async with Client(build_server(caller(client))) as mcp:
            stranger = error(
                await mcp.call_tool("submit_work", {**message, "user_id": "U0STRANGER"})
            )
            missing = error(await mcp.call_tool("goal_status", {**USER, "goal_id": "goal-none"}))
        return [denied, stranger, missing]

    denied, stranger, missing = run(steps)
    assert denied["code"] == "UNAUTHENTICATED"
    assert stranger["code"] == "FORBIDDEN"
    assert missing["code"] == "NOT_FOUND"


# -- the separate process -------------------------------------------------------------------------


def test_the_server_runs_as_its_own_process_over_stdio() -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "AMPLAI_INTAKE_URL": "http://127.0.0.1:9",  # nothing listens: listing needs no AMPLAI
        "AMPLAI_INTAKE_ADAPTER": "hermes",
        "AMPLAI_INTAKE_TOKEN": HERMES_TOKEN,
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "amplai_foundry.integrations.intake_mcp"], env=env
    )

    async def steps() -> Any:
        async with Client(params) as mcp:
            tools = (await mcp.list_tools()).tools
            unreachable = await mcp.call_tool("list_goals", USER)
            return tools, unreachable

    tools, unreachable = run(steps)
    assert {t.name for t in tools} == set(load())
    assert error(unreachable)["code"] == "AMPLAI_UNREACHABLE"
    assert HERMES_TOKEN not in unreachable.model_dump_json()


@pytest.mark.parametrize(
    "env, says",
    [
        ({}, "AMPLAI_INTAKE_URL, AMPLAI_INTAKE_ADAPTER, AMPLAI_INTAKE_TOKEN"),
        (
            {"AMPLAI_INTAKE_URL": "http://127.0.0.1:9", "AMPLAI_INTAKE_ADAPTER": "hermes",
             "AMPLAI_INTAKE_TOKEN": "short"},
            "32+ characters",
        ),
        (
            {"AMPLAI_INTAKE_URL": "file:///etc", "AMPLAI_INTAKE_ADAPTER": "hermes",
             "AMPLAI_INTAKE_TOKEN": HERMES_TOKEN},
            "not an http(s) URL",
        ),
    ],
)  # fmt: skip
def test_the_server_refuses_to_start_without_its_settings(env: dict[str, str], says: str) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "amplai_foundry.integrations.intake_mcp"],
        env={"PATH": os.environ.get("PATH", ""), **env},
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert done.returncode != 0 and says in done.stderr
    assert HERMES_TOKEN not in done.stderr


def test_the_settings_hide_the_token() -> None:
    environ = {
        "AMPLAI_INTAKE_URL": "http://127.0.0.1:8765/",
        "AMPLAI_INTAKE_ADAPTER": "hermes",
        "AMPLAI_INTAKE_TOKEN": HERMES_TOKEN,
    }
    settings = Settings.from_environment(environ)
    assert "AMPLAI_INTAKE_TOKEN" not in environ  # removed once read
    assert HERMES_TOKEN not in repr(settings) and settings.url == "http://127.0.0.1:8765"
    assert CONTRACT_VERSION == "1"
