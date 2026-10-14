"""Work 034 S1g — the intake-only listener (spec H-12, OD-8, plan.md §3.2 item 11).

The front agent's VM cannot reach the operator API on the host's loopback (OD-8). It enters
through a second listener (``intake_listener`` in ``local.json``) that serves only
``/api/v3/intake/<adapter_id>`` and refuses every other path with ``404 INTAKE_LISTENER_ROUTE``.
The operator token opens nothing on it. Until H-12's TLS or source-pinned token is decided, the
listener binds only a loopback address.

Real: `amplai ops local-init` output, LocalProductDeployment, both apps, uvicorn on real sockets.
Stand-in: the planner returns a fixed draft (as in test_034_s1a_intake_route.py).
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect
from test_034_s1a_intake_route import (
    HERMES,
    SLACK_USER,
    init_home,
    message,
    private,
    settle,
    with_intake,
)
from typer.testing import CliRunner

from amplai_foundry.control_plane.api_v3 import intake_listener
from amplai_foundry.runtime import cli
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.local_deployment import IntakeListenerEntry, LocalProductDeployment
from rc06_rig import FixedPlanner

ROOT = "/api/v3/intake/hermes"


def listen_on(home: Path, listener: dict[str, Any] | None) -> None:
    config = home / "local.json"
    value = json.loads(config.read_text())
    if listener is None:
        value.pop("intake_listener", None)
    else:
        value["intake_listener"] = listener
    private(config, json.dumps(value))


def start(home: Path) -> LocalProductDeployment:
    dep = LocalProductDeployment(home / "local.json", start_loop=False)
    for installed in dep.service.apps.values():
        installed.planners["codex-cli"] = FixedPlanner()
    return dep


@pytest.fixture
def listening(tmp_path: Path) -> Any:
    home = init_home(tmp_path)
    with_intake(home)
    listen_on(home, {"host": "127.0.0.1", "port": 5099})
    dep = start(home)
    operator = (home / "operator.token").read_text().strip()
    with TestClient(dep.app) as op_client, TestClient(dep.intake_app) as intake_client:
        yield dep, op_client, intake_client, operator, home
    settle(dep)
    dep.close()


def refused(response: httpx.Response) -> None:
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "INTAKE_LISTENER_ROUTE"


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


# -- intake works through the listener ---------------------------------------------------------


def test_the_intake_routes_work_through_the_listener(listening: Any) -> None:
    _dep, _op, client, _operator, _home = listening
    response = client.post(ROOT, headers=HERMES, json=message("m-1", text="make value return 2"))
    assert response.status_code == 202, response.text
    goal = response.json()["goal_id"]
    listed = client.get(f"{ROOT}/goals", headers=HERMES, params=SLACK_USER)
    assert listed.status_code == 200
    assert goal in [item["goal_id"] for item in listed.json()["items"]]
    one = client.get(f"{ROOT}/goals/{goal}", headers=HERMES, params=SLACK_USER)
    assert one.status_code == 200 and one.json()["goal_id"] == goal
    feed = client.get(f"{ROOT}/events", headers=HERMES, params=SLACK_USER)
    assert feed.status_code == 200 and feed.json()["snapshot_ref"] == f"{ROOT}/snapshot"
    snapshot = client.get(f"{ROOT}/snapshot", headers=HERMES, params=SLACK_USER)
    assert snapshot.status_code == 200 and "cursor" in snapshot.json()
    cancel = client.post(f"{ROOT}/goals/{goal}/cancel", headers=HERMES,
                         json=message("m-2", reason="not needed"))  # fmt: skip
    assert cancel.status_code == 200, cancel.text


def test_an_unknown_intake_route_under_the_adapter_root_is_coded(listening: Any) -> None:
    _dep, _op, client, _operator, _home = listening
    refused(client.get(f"{ROOT}/nothing-here", headers=HERMES))
    wrong = client.get(ROOT, headers=HERMES)  # the submit route is POST only
    assert wrong.status_code == 405 and wrong.json()["code"] == "METHOD_NOT_ALLOWED"


# -- everything else is unreachable ------------------------------------------------------------


def test_every_operator_route_is_unreachable_through_the_listener(listening: Any) -> None:
    """Each route of the operator app (core V3, meta, evaluation, local product, health, OpenAPI)
    with each of its methods and the operator token is 404 ``INTAKE_LISTENER_ROUTE`` on the
    listener. The routes come from the operator app's own OpenAPI document, which lists the
    included routers too (FastAPI keeps those out of ``app.routes``)."""
    _dep, op, client, operator, _home = listening
    auth = {"Authorization": "Bearer " + operator, "Idempotency-Key": "k-1", "If-Match": "1"}
    paths = op.get("/openapi.json").json()["paths"]
    surfaces = {"/openapi.json": ["get"]}
    surfaces.update({path: sorted(item) for path, item in paths.items()})
    checked: set[str] = set()
    for path, methods in surfaces.items():
        if path == ROOT or path.startswith(ROOT + "/"):
            continue
        for method in methods:
            refused(client.request(method.upper(), concrete(path), headers=auth, json={}))
        checked.add(path)
    for surface in ("/healthz", "/openapi.json", "/api/v3/readyz", "/api/v3/snapshot",
                    "/api/v3/events", "/api/v3/meta/proposals", "/api/v3/runtime/kill",
                    "/api/v3/local/goals", "/api/v3/local/goals/{goal_id}/approve",
                    "/api/v3/local/publications/sync"):  # fmt: skip
        assert surface in checked, surface
    assert len(checked) > 50
    # the intake routes are in the same document and are served by the listener
    assert any(path.startswith(ROOT + "/") for path in paths)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/healthz",
        "/openapi.json",
        "/docs",
        "/redoc",
        "/api/v3/readyz",
        "/api/v3/snapshot",
        "/api/v3/events",
        "/api/v3/local/goals",
        "/api/v3/runtime/kill",
        "/api/v3/intake",
        "/api/v3/intake/",
        "/api/v3/intake/other/goals",  # not a configured adapter
        "/api/v3/intake/hermesx/goals",  # a prefix of the adapter id is not its root
        "/api/v3/intakehermes/goals",
        "/api/v3/intake/hermes/../../readyz",
    ],
)
def test_paths_outside_the_intake_roots_are_refused(listening: Any, path: str) -> None:
    _dep, _op, client, operator, _home = listening
    for headers in ({}, HERMES, {"Authorization": "Bearer " + operator}):
        refused(client.get(path, headers=headers))
        refused(client.post(path, headers=headers, json={}))


def test_dot_segments_reach_the_guard_unnormalized() -> None:
    guard = intake_listener.IntakeOnly(None, ["hermes"])
    assert guard.allowed(ROOT) and guard.allowed(ROOT + "/goals")
    assert not guard.allowed(ROOT + "/../../readyz")
    assert not guard.allowed(ROOT + "/goals/..")
    assert not guard.allowed(ROOT + "x/goals")


def test_a_websocket_is_closed_before_it_is_accepted(listening: Any) -> None:
    _dep, _op, client, _operator, _home = listening
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(ROOT + "/events"):
        pass


def test_the_operator_token_opens_no_intake_route(listening: Any) -> None:
    _dep, _op, client, operator, _home = listening
    response = client.get(
        f"{ROOT}/goals", headers={"Authorization": "Bearer " + operator}, params=SLACK_USER
    )
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    posted = client.post(
        ROOT, headers={"Authorization": "Bearer " + operator}, json=message("m-op", text="x")
    )
    assert posted.status_code == 401


def test_both_listeners_share_one_rate_limit_window(tmp_path: Path) -> None:
    """One adapter keeps one window (H-7): the listener does not double its limit."""
    home = init_home(tmp_path)
    with_intake(home, limits={"submit": {"count": 1, "window_seconds": 600}})
    listen_on(home, {"host": "127.0.0.1", "port": 5099})
    dep = start(home)
    try:
        with TestClient(dep.app) as op, TestClient(dep.intake_app) as side:
            first = op.post(ROOT, headers=HERMES, json=message("m-1", text="x"))
            assert first.status_code == 202, first.text
            second = side.post(ROOT, headers=HERMES, json=message("m-2", text="y"))
            assert second.status_code == 429
            assert second.json()["code"] == "RATE_LIMITED"
    finally:
        settle(dep)
        dep.close()


# -- configuration ------------------------------------------------------------------------------


def test_without_a_listener_nothing_changes(tmp_path: Path) -> None:
    home = init_home(tmp_path)
    with_intake(home)
    dep = start(home)
    try:
        assert dep.intake_app is None
        with TestClient(dep.app) as op:  # the operator listener keeps the intake routes
            assert op.get(f"{ROOT}/goals", headers=HERMES, params=SLACK_USER).status_code == 200
    finally:
        dep.close()


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "localhost", "vm.local", "", "127.0.0.1:8080"],
)
def test_the_host_is_one_ip_address(host: str) -> None:
    with pytest.raises(ValidationError):
        IntakeListenerEntry.model_validate({"host": host, "port": 5099})


@pytest.mark.parametrize("value", [{"host": "127.0.0.1"}, {"port": 5099}, {}])
def test_host_and_port_have_no_default(value: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        IntakeListenerEntry.model_validate(value)


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_the_port_is_in_range(port: int) -> None:
    with pytest.raises(ValidationError):
        IntakeListenerEntry.model_validate({"host": "127.0.0.1", "port": port})


@pytest.mark.parametrize("host", ["192.168.64.1", "10.0.2.2", "fe80::1"])
def test_an_address_off_loopback_needs_h12_protection(tmp_path: Path, host: str) -> None:
    home = init_home(tmp_path)
    with_intake(home)
    listen_on(home, {"host": host, "port": 5099})
    with pytest.raises(Hold) as held:
        LocalProductDeployment(home / "local.json", start_loop=False)
    assert held.value.code == "INTAKE_LISTENER_UNPROTECTED"
    # the store's owner lock was released: the deployment opens again without the listener
    listen_on(home, None)
    LocalProductDeployment(home / "local.json", start_loop=False).close()


def test_ipv6_loopback_is_a_loopback_address(tmp_path: Path) -> None:
    home = init_home(tmp_path)
    with_intake(home)
    listen_on(home, {"host": "::1", "port": 5099})
    dep = start(home)
    try:
        assert dep.intake_app is not None
    finally:
        dep.close()


def test_a_listener_needs_intake_adapters(tmp_path: Path) -> None:
    home = init_home(tmp_path)
    listen_on(home, {"host": "127.0.0.1", "port": 5099})
    with pytest.raises(Hold) as held:
        LocalProductDeployment(home / "local.json", start_loop=False)
    assert held.value.code == "INTAKE_LISTENER_NO_INTAKE"


def test_local_serve_refuses_the_operator_port(tmp_path: Path) -> None:
    home = init_home(tmp_path)
    with_intake(home)
    listen_on(home, {"host": "127.0.0.1", "port": 5098})
    result = CliRunner().invoke(
        cli.app, ["ops", "local-serve", "--config", str(home / "local.json"), "--port", "5098"]
    )
    assert result.exit_code == 3, result.output
    assert json.loads(result.output)["code"] == "INTAKE_LISTENER_PORT"


def test_a_taken_port_is_a_coded_hold() -> None:
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    try:
        with pytest.raises(Hold) as held:
            intake_listener.bind_socket("127.0.0.1", taken.getsockname()[1])
        assert held.value.code == "INTAKE_LISTENER_BIND"
    finally:
        taken.close()


# -- real sockets ------------------------------------------------------------------------------


def test_two_sockets_serve_two_surfaces(listening: Any) -> None:
    """uvicorn on two real sockets in one event loop: the operator socket serves the operator
    API, the listener socket only the intake routes. Stopping the operator stops both."""
    dep, _op, _client, operator, _home = listening
    op_sock = intake_listener.bind_socket("127.0.0.1", 0)
    side_sock = intake_listener.bind_socket("127.0.0.1", 0)
    op_url = f"http://127.0.0.1:{op_sock.getsockname()[1]}"
    side_url = f"http://127.0.0.1:{side_sock.getsockname()[1]}"
    op_server = uvicorn.Server(uvicorn.Config(dep.app, workers=1, access_log=False))
    side_server = intake_listener._side_server(dep.intake_app)
    thread = threading.Thread(
        target=lambda: asyncio.run(
            intake_listener.run_pair(op_server, side_server, [side_sock], [op_sock])
        ),
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.time() + 20
        while not (op_server.started and side_server.started) and time.time() < deadline:
            time.sleep(0.05)
        assert op_server.started and side_server.started
        auth = {"Authorization": "Bearer " + operator}
        assert httpx.get(op_url + "/healthz").status_code == 200
        assert httpx.get(op_url + "/api/v3/local/goals", headers=auth).status_code == 200
        refused(httpx.get(side_url + "/healthz"))
        refused(httpx.get(side_url + "/api/v3/local/goals", headers=auth))
        reached = httpx.get(side_url + f"{ROOT}/goals", headers=HERMES, params=SLACK_USER)
        assert reached.status_code == 200, reached.text
        assert reached.json() == {"items": []}
    finally:
        op_server.should_exit = True
        thread.join(timeout=20)
        op_sock.close()
        side_sock.close()
    assert not thread.is_alive()
    assert side_server.should_exit
