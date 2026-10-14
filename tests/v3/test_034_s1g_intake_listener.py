"""Work 034 S1g — the intake-only listener (spec H-12, OD-8, plan.md §3.2 item 11).

The front agent's VM cannot reach the operator API on the host's loopback (OD-8). It enters
through a second listener (``intake_listener`` in ``local.json``) that serves only
``/api/v3/intake/<adapter_id>`` and refuses every other path with ``404 INTAKE_LISTENER_ROUTE``.
The operator token opens nothing on it. Until H-12's TLS or source-pinned token is decided,
``ops local-serve`` binds it only on a loopback address.

Real: `amplai ops local-init` output, LocalProductDeployment, both apps, uvicorn on real sockets.
Stand-in: the planner returns a fixed draft (as in test_034_s1a_intake_route.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.routing import Mount
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


def walk(routes: list[Any], prefix: str = "") -> Iterator[tuple[str, list[str]]]:
    """Every (path, methods) of an app, into included routers (FastAPI 0.143 keeps an
    ``include_router`` as one ``_IncludedRouter`` entry) and mounts."""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from walk(included.routes, prefix + route.include_context.prefix)
        elif isinstance(route, Mount):
            yield from walk(route.routes, prefix + route.path)
        else:
            yield prefix + route.path, sorted(getattr(route, "methods", None) or {"GET"})


def free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port: int = probe.getsockname()[1]
    probe.close()
    return port


def port_free(port: int) -> bool:
    """Nothing listens on the port any more (a closed connection may still be in TIME_WAIT, so
    this tries to connect rather than to bind)."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    except OSError:
        return True
    return False


def raw(port: int, request: bytes, timeout: float = 10.0) -> tuple[int, dict[str, str], bytes]:
    """One HTTP/1.1 exchange on a plain socket: the request target is sent exactly as given."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as conn:
        conn.sendall(request)
        data = b""
        while chunk := conn.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in lines[1:])}
    return int(lines[0].split()[1]), headers, body


def get(target: bytes) -> bytes:
    return b"GET " + target + b" HTTP/1.1\r\nHost: amplai.test\r\nConnection: close\r\n\r\n"


@contextlib.contextmanager
def serving(dep: Any) -> Iterator[tuple[int, int]]:
    """Both apps on real loopback sockets (``run_pair`` in a thread); yields the two ports."""
    op_sock = intake_listener.bind_socket("127.0.0.1", 0)
    side_sock = intake_listener.bind_socket("127.0.0.1", 0)
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
        yield op_sock.getsockname()[1], side_sock.getsockname()[1]
    finally:
        op_server.should_exit = True
        thread.join(timeout=20)
        op_sock.close()
        side_sock.close()
    assert not thread.is_alive()
    assert side_server.should_exit


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
    """Each route of the operator app (core V3, meta, evaluation, local product, health, OpenAPI),
    found by walking its route tree, with each of its methods and the operator token, is 404
    ``INTAKE_LISTENER_ROUTE`` on the listener."""
    dep, op, client, operator, _home = listening
    auth = {"Authorization": "Bearer " + operator, "Idempotency-Key": "k-1", "If-Match": "1"}
    routes = dict(walk(dep.app.router.routes))
    # the walk is complete: it has every path of the operator app's own OpenAPI document
    assert set(op.get("/openapi.json").json()["paths"]) <= set(routes)
    checked: set[str] = set()
    for path, methods in routes.items():
        if path == ROOT or path.startswith(ROOT + "/"):
            continue
        for method in methods:
            response = client.request(method, concrete(path), headers=auth, json={})
            if method == "HEAD":
                assert response.status_code == 404
            else:
                refused(response)
        checked.add(path)
    for surface in ("/healthz", "/openapi.json", "/api/v3/readyz", "/api/v3/snapshot",
                    "/api/v3/events", "/api/v3/meta/proposals", "/api/v3/runtime/kill",
                    "/api/v3/local/goals", "/api/v3/local/goals/{goal_id}/approve",
                    "/api/v3/local/publications/sync"):  # fmt: skip
        assert surface in checked, surface
    assert len(checked) > 50
    assert any(path.startswith(ROOT + "/") for path in routes)  # intake is in the same tree


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
        "/api/v3/intake/hermes/",  # no slash redirect
        "/api/v3/intake/hermes/goals/",
    ],
)
def test_paths_outside_the_intake_roots_are_refused(listening: Any, path: str) -> None:
    _dep, _op, client, operator, _home = listening
    for headers in ({}, HERMES, {"Authorization": "Bearer " + operator}):
        got = client.get(path, headers=headers, follow_redirects=False)
        refused(got)
        assert "location" not in got.headers
        refused(client.post(path, headers=headers, json={}, follow_redirects=False))


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


@pytest.mark.parametrize("who", ["none", "wrong", "operator"])
def test_an_unauthenticated_invalid_request_learns_no_field_names(listening: Any, who: str) -> None:
    _dep, _op, client, operator, _home = listening
    token = {"none": None, "wrong": "x" * 40, "operator": operator}[who]
    headers = {} if token is None else {"Authorization": "Bearer " + token}
    for response in (
        client.post(ROOT, headers=headers, json={"bogus": 1}),  # invalid body
        client.post(f"{ROOT}/goals/g-1/cancel", headers=headers, content=b"not json"),
        client.get(f"{ROOT}/goals", headers=headers),  # the query lacks provider and user_id
        client.get(f"{ROOT}/events", headers=headers, params={**SLACK_USER, "limit": "x"}),
    ):
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "UNAUTHENTICATED"
        assert "loc" not in response.text and "provider" not in response.text


def test_the_adapter_token_still_gets_the_validation_details(listening: Any) -> None:
    _dep, _op, client, _operator, _home = listening
    response = client.get(f"{ROOT}/goals", headers=HERMES)
    assert response.status_code == 422
    assert {e["loc"][-1] for e in response.json()["detail"]} == {"provider", "user_id"}


def test_the_operator_listener_validates_as_before(listening: Any) -> None:
    """The operator app is unchanged: FastAPI's 422 comes before the intake gate there."""
    _dep, op, _client, _operator, _home = listening
    assert op.get(f"{ROOT}/goals").status_code == 422


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


@contextlib.contextmanager
def watchdog(seconds: float) -> Iterator[None]:
    """A bound on a blocking call in the main thread: past ``seconds`` it sends SIGINT (a second
    one 3 s later), which ``serve_with_listener`` turns into a stop, and the test fails. A
    regression then fails the test instead of hanging the run."""
    done, fired = threading.Event(), threading.Event()

    def run() -> None:
        if done.wait(seconds):
            return
        fired.set()
        os.kill(os.getpid(), signal.SIGINT)
        if not done.wait(3):
            os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join(timeout=5)
    assert not fired.is_set(), f"did not end within {seconds} s"


def serve(home: Path, port: int) -> Any:
    with watchdog(30):
        return CliRunner().invoke(
            cli.app,
            ["ops", "local-serve", "--config", str(home / "local.json"), "--port", str(port)],
        )


@pytest.mark.parametrize("host", ["192.168.64.1", "10.0.2.2", "fe80::1"])
def test_an_address_off_loopback_needs_h12_protection(tmp_path: Path, host: str) -> None:
    home = init_home(tmp_path)
    with_intake(home)
    listen_on(home, {"host": host, "port": 5099})
    result = serve(home, free_port())
    assert result.exit_code == 3, result.output
    out = json.loads(result.output)
    assert out["code"] == "INTAKE_LISTENER_UNPROTECTED" and out["details"] == {"host": host}
    # only local-serve refuses: every other command that opens the deployment still works
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
    LocalProductDeployment(home / "local.json", start_loop=False).close()  # other commands work
    result = serve(home, free_port())
    assert result.exit_code == 3, result.output
    assert json.loads(result.output)["code"] == "INTAKE_LISTENER_NO_INTAKE"


def test_local_serve_refuses_the_operator_port(tmp_path: Path) -> None:
    home = init_home(tmp_path)
    with_intake(home)
    listen_on(home, {"host": "127.0.0.1", "port": 5098})
    result = serve(home, 5098)
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
    with serving(dep) as (op_port, side_port):
        op_url, side_url = f"http://127.0.0.1:{op_port}", f"http://127.0.0.1:{side_port}"
        auth = {"Authorization": "Bearer " + operator}
        assert httpx.get(op_url + "/healthz").status_code == 200
        assert httpx.get(op_url + "/api/v3/local/goals", headers=auth).status_code == 200
        refused(httpx.get(side_url + "/healthz"))
        refused(httpx.get(side_url + "/api/v3/local/goals", headers=auth))
        reached = httpx.get(side_url + f"{ROOT}/goals", headers=HERMES, params=SLACK_USER)
        assert reached.status_code == 200, reached.text
        assert reached.json() == {"items": []}
        assert "server" not in reached.headers


@pytest.mark.parametrize(
    ("target", "status"),
    [
        (b"/api/v3/intake/hermes%2F..%2F..%2Freadyz", 404),  # %2F decodes to a dot segment
        (b"/api/v3/intake/hermes/%2E%2E/%2E%2E/readyz", 404),
        (b"/api/v3/intake/hermes/%2e%2e/%2e%2e/healthz", 404),
        (b"/api/v3/intake/hermes/%252E%252E/readyz", 404),  # stays "%2E%2E": no such route
        (b"/%68ealthz", 404),
        (b"/api/v3/intake%2Fother/goals", 404),
        (b"/api/v3/intake%2Fhermes/goals", 401),  # decodes to the intake route: needs its token
        (b"http://amplai.test/healthz", 404),  # absolute form
        (b"http://amplai.test/api/v3/readyz", 404),
        (b"http://amplai.test/api/v3/intake/hermes/../../readyz", 404),
    ],
)
def test_encoded_and_absolute_targets_on_a_raw_socket(
    listening: Any, target: bytes, status: int
) -> None:
    dep, _op, _client, _operator, _home = listening
    with serving(dep) as (_op_port, side_port):
        code, headers, body = raw(side_port, get(target))
    assert code == status, body
    expected = "INTAKE_LISTENER_ROUTE" if status == 404 else "UNAUTHENTICATED"
    assert json.loads(body)["code"] == expected
    assert "location" not in headers


def test_forwarded_headers_do_not_change_the_peer() -> None:
    """The listener's server trusts no X-Forwarded-* (a source-pinned token would rely on it)."""

    async def probe(scope: Any, receive: Any, send: Any) -> None:
        body = json.dumps({"client": scope["client"][0], "scheme": scope["scheme"]}).encode()
        length = str(len(body)).encode()
        await send(
            {"type": "http.response.start", "status": 200, "headers": [(b"content-length", length)]}
        )
        await send({"type": "http.response.body", "body": body})

    sock = intake_listener.bind_socket("127.0.0.1", 0)
    server = intake_listener._side_server(probe)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[sock])), daemon=True)
    thread.start()
    try:
        deadline = time.time() + 20
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        forwarded = b"X-Forwarded-For: 203.0.113.9\r\nX-Forwarded-Proto: https\r\n"
        request = get(b"/").replace(b"\r\n\r\n", b"\r\n" + forwarded + b"\r\n")
        code, headers, body = raw(sock.getsockname()[1], request)
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()
    assert code == 200
    assert json.loads(body) == {"client": "127.0.0.1", "scheme": "http"}
    assert "server" not in headers
    assert server.config.proxy_headers is False


# -- serve_with_listener: signals, one ending stops the other, bounded shutdown ---------------


def _after_start(ports: tuple[int, int], then: Any, seen: dict[str, Any]) -> threading.Thread:
    """In a helper thread: wait until both ports answer, run ``then``, and always end with a
    SIGINT to this process (``serve_with_listener`` runs in the main thread)."""

    def run() -> None:
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    httpx.get(f"http://127.0.0.1:{ports[0]}/healthz", timeout=1)
                    httpx.get(f"http://127.0.0.1:{ports[1]}/healthz", timeout=1)
                    break
                except httpx.HTTPError:
                    time.sleep(0.05)
            then(seen)
        except BaseException as exc:  # reported by the test, never left running
            seen["error"] = exc
        finally:
            seen["signalled_at"] = time.monotonic()
            os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_ctrl_c_stops_both_servers(listening: Any) -> None:
    dep, _op, _client, operator, _home = listening
    op_port, side_port = free_port(), free_port()
    before = signal.getsignal(signal.SIGINT)

    def check(seen: dict[str, Any]) -> None:
        auth = {"Authorization": "Bearer " + operator}
        seen["op"] = httpx.get(f"http://127.0.0.1:{op_port}/api/v3/local/goals", headers=auth)
        seen["side"] = httpx.get(f"http://127.0.0.1:{side_port}/healthz")

    seen: dict[str, Any] = {}
    helper = _after_start((op_port, side_port), check, seen)
    with watchdog(45):
        intake_listener.serve_with_listener(
            dep.app, op_port, dep.intake_app, "127.0.0.1", side_port
        )
    helper.join(timeout=10)
    assert "error" not in seen, seen
    assert seen["op"].status_code == 200
    refused(seen["side"])
    assert port_free(op_port) and port_free(side_port)
    assert signal.getsignal(signal.SIGINT) is before  # the original handler is back


def test_a_slow_client_does_not_hold_the_shutdown(listening: Any) -> None:
    """A request whose body never arrives and a connection whose headers never end: shutdown
    still ends within the graceful timeout (uvicorn has no read timeout of its own)."""
    dep, _op, _client, _operator, _home = listening
    op_port, side_port = free_port(), free_port()
    slow: list[socket.socket] = []

    def stall(seen: dict[str, Any]) -> None:
        body = socket.create_connection(("127.0.0.1", side_port))
        body.sendall(
            b"POST " + ROOT.encode() + b" HTTP/1.1\r\nHost: x\r\nContent-Type: application/json"
            b'\r\nContent-Length: 1000\r\n\r\n{"text":'
        )
        headers = socket.create_connection(("127.0.0.1", side_port))
        headers.sendall(b"GET " + ROOT.encode() + b"/goals HTTP/1.1\r\nHost: x\r\n")
        slow.extend((body, headers))
        time.sleep(0.3)  # the POST is now waiting for its body inside the app

    seen: dict[str, Any] = {}
    helper = _after_start((op_port, side_port), stall, seen)
    with watchdog(45):
        intake_listener.serve_with_listener(
            dep.app, op_port, dep.intake_app, "127.0.0.1", side_port, graceful=1
        )
    took = time.monotonic() - seen["signalled_at"]
    helper.join(timeout=10)
    for conn in slow:
        conn.close()
    assert "error" not in seen, seen
    assert took < 8, took
    assert port_free(side_port)


def test_the_operator_failing_to_start_stops_the_listener(listening: Any) -> None:
    dep, _op, _client, _operator, _home = listening
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    side_port = free_port()
    began = time.monotonic()
    try:
        with watchdog(15), pytest.raises(SystemExit):  # uvicorn exits when it cannot bind
            intake_listener.serve_with_listener(
                dep.app, taken.getsockname()[1], dep.intake_app, "127.0.0.1", side_port
            )
    finally:
        taken.close()
    assert time.monotonic() - began < 15
    assert port_free(side_port)


def test_the_listener_ending_stops_the_operator(
    listening: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dep, _op, _client, _operator, _home = listening

    class Broken:
        should_exit = False
        force_exit = False

        async def serve(self, sockets: Any = None) -> None:
            await asyncio.sleep(0.5)
            raise RuntimeError("the listener ended")

    monkeypatch.setattr(intake_listener, "_side_server", lambda app, graceful=10: Broken())
    op_port, side_port = free_port(), free_port()
    began = time.monotonic()
    with watchdog(15), pytest.raises(RuntimeError, match="the listener ended"):
        intake_listener.serve_with_listener(
            dep.app, op_port, dep.intake_app, "127.0.0.1", side_port
        )
    assert time.monotonic() - began < 15
    assert port_free(op_port) and port_free(side_port)


def test_serve_with_listener_without_an_intake_app_holds() -> None:
    with watchdog(10), pytest.raises(Hold) as held:
        intake_listener.serve_with_listener(None, free_port(), None, "127.0.0.1", free_port())
    assert held.value.code == "INTAKE_LISTENER_NO_INTAKE"


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"])
def test_loopback_addresses_pass_the_protection_check(host: str) -> None:
    intake_listener.check_protected(host)


@pytest.mark.parametrize("host", ["192.168.64.1", "::ffff:192.168.64.1", "fe80::1", "::2"])
def test_other_addresses_fail_the_protection_check(host: str) -> None:
    with pytest.raises(Hold) as held:
        intake_listener.check_protected(host)
    assert held.value.code == "INTAKE_LISTENER_UNPROTECTED"


def test_a_mapped_any_address_is_every_interface() -> None:
    with pytest.raises(ValidationError):
        IntakeListenerEntry.model_validate({"host": "::ffff:0.0.0.0", "port": 5099})


def test_both_servers_wait_the_graceful_seconds(
    listening: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both uvicorn configs carry GRACEFUL_SECONDS; only the listener's turns proxy headers and
    the Server header off (the operator server is configured as before)."""
    dep, _op, _client, _operator, _home = listening
    configs: list[Any] = []

    class Recorded:
        should_exit = False
        force_exit = False

        def __init__(self, config: Any) -> None:
            configs.append(config)

        async def serve(self, sockets: Any = None) -> None:
            return None

    monkeypatch.setattr(intake_listener, "_quiet_server", Recorded)
    with watchdog(15):
        intake_listener.serve_with_listener(
            dep.app, free_port(), dep.intake_app, "127.0.0.1", free_port()
        )
    operator, side = configs
    assert operator.timeout_graceful_shutdown == intake_listener.GRACEFUL_SECONDS
    assert side.timeout_graceful_shutdown == intake_listener.GRACEFUL_SECONDS
    assert intake_listener.GRACEFUL_SECONDS == 10
    assert (operator.host, operator.app) == ("127.0.0.1", dep.app)
    assert side.app is dep.intake_app and side.lifespan == "off"
    assert side.proxy_headers is False and side.server_header is False


def test_an_oversized_body_to_a_foreign_path_is_refused_by_route(listening: Any) -> None:
    """The route guard runs before the body bound: a foreign path is 404, never 413."""
    _dep, _op, client, _operator, _home = listening
    big = b"x" * (4 * 1024 * 1024 + 1)
    refused(client.post("/healthz", content=big))
    refused(client.post("/api/v3/intents", content=big, headers=HERMES))
    on_route = client.post(ROOT, content=big, headers=HERMES)  # an intake route: the bound
    assert on_route.status_code == 413 and on_route.json()["code"] == "BODY_TOO_LARGE"


def test_a_second_ctrl_c_does_not_wait_for_a_stalled_request(listening: Any) -> None:
    """With a request stalled on its body and a 30 s graceful window, a second Ctrl-C stops both
    servers at once (uvicorn ``force_exit``)."""
    dep, _op, _client, _operator, _home = listening
    op_port, side_port = free_port(), free_port()
    slow: list[socket.socket] = []

    def stall_then_interrupt(seen: dict[str, Any]) -> None:
        body = socket.create_connection(("127.0.0.1", side_port))
        body.sendall(
            b"POST " + ROOT.encode() + b" HTTP/1.1\r\nHost: x\r\nContent-Type: application/json"
            b'\r\nContent-Length: 1000\r\n\r\n{"text":'
        )
        slow.append(body)
        time.sleep(0.3)  # the POST waits for its body inside the app
        seen["first_at"] = time.monotonic()
        os.kill(os.getpid(), signal.SIGINT)  # the second comes from _after_start
        time.sleep(0.5)

    seen: dict[str, Any] = {}
    helper = _after_start((op_port, side_port), stall_then_interrupt, seen)
    with watchdog(45):
        intake_listener.serve_with_listener(
            dep.app, op_port, dep.intake_app, "127.0.0.1", side_port, graceful=30
        )
    took = time.monotonic() - seen["first_at"]
    helper.join(timeout=10)
    for conn in slow:
        conn.close()
    assert "error" not in seen, seen
    assert took < 5, took
    assert port_free(op_port) and port_free(side_port)


def test_sigterm_stops_both_servers_and_ends_the_process_with_it(tmp_path: Path) -> None:
    """``ops local-serve`` with the listener, as a process: SIGTERM shuts both servers down, then
    the process ends with SIGTERM, as with ``uvicorn.run``."""
    home = init_home(tmp_path)
    with_intake(home)
    op_port, side_port = free_port(), free_port()
    listen_on(home, {"host": "127.0.0.1", "port": side_port})
    command = [
        sys.executable, "-c", "from amplai_foundry.runtime.cli import app; app()",
        "ops", "local-serve", "--config", str(home / "local.json"), "--port", str(op_port),
    ]  # fmt: skip
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                assert (
                    httpx.get(f"http://127.0.0.1:{op_port}/healthz", timeout=1).status_code == 200
                )
                refused(httpx.get(f"http://127.0.0.1:{side_port}/healthz", timeout=1))
                break
            except httpx.HTTPError:
                assert proc.poll() is None, proc.stdout.read() if proc.stdout else ""
                time.sleep(0.1)
        else:
            raise AssertionError("local-serve did not start")
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    log = out.decode()
    assert proc.returncode == -signal.SIGTERM, log
    assert log.count("Shutting down") == 2, log  # both servers shut down
    assert log.count("Finished server process") == 2, log
    assert "Application shutdown complete" in log  # the operator's lifespan (loop) ended
    assert port_free(op_port) and port_free(side_port)
