"""PerDispatchOpenCodePort and OpenCode local config (specs/031-opencode-driver), fixtures only."""

from __future__ import annotations

import base64
import json
import queue
import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from amplai_foundry.agent_drivers.opencode_port import (
    AUTH,
    CATALOG,
    PendingDockerLauncher,
    PerDispatchOpenCodePort,
    ScopedOpenCodeHome,
)
from amplai_foundry.agent_drivers.ports import AgentDriverPort
from amplai_foundry.agent_drivers.protocol import SessionJournal
from amplai_foundry.runtime import cli
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution.codex import AUTH as CODEX_AUTH
from amplai_foundry.runtime.execution.codex import install_driver_profile
from amplai_foundry.runtime.execution.product import ROUTER_ORDER
from amplai_foundry.runtime.local_deployment import LocalProductDeployment
from rc06_rig import codex_inputs, make_repo


def frame(event: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps({"id": uuid.uuid4().hex, **event}).encode() + b"\n\n"


class IterStream(httpx.SyncByteStream):
    def __init__(self, chunks: Iterator[bytes]) -> None:
        self.chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        return self.chunks


class FakeServer:
    """An in-process stand-in for one ``opencode serve`` reached by its exec transport."""

    base_url = "http://127.0.0.1:4096"
    password = "per-run-test-password"  # the server always runs with a password (D-091)

    def __init__(self, workspace: Path, home: Path) -> None:
        self.ws, self.home = workspace, home
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._busy, self.children, self.running = False, 0, True
        self.message: str | None = None
        self.sessions = 0
        self.saw_auth = (home / AUTH).is_file()
        self.transport = httpx.MockTransport(self.handle)

    @property
    def busy(self) -> bool:
        return self._busy

    @busy.setter
    def busy(self, value: bool) -> None:
        self._busy = value
        if not value and self.message:
            self.events.put({"type": "message.updated", "properties": {"info": self.reply()}})
        status = {"type": "busy" if value else "idle"}
        self.events.put({"type": "session.status",
                         "properties": {"sessionID": "ses_one", "status": status}})  # fmt: skip

    def reply(self) -> dict[str, Any]:
        return {"id": "msg_reply", "sessionID": "ses_one", "role": "assistant",
                "parentID": self.message, "time": {"completed": 1}}  # fmt: skip

    def stream(self) -> Iterator[bytes]:
        yield frame({"type": "server.connected", "properties": {}})
        while self.running:
            try:
                yield frame(self.events.get(timeout=0.02))
            except queue.Empty:
                continue

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert self.running, "request to a stopped server"
        expected = "Basic " + base64.b64encode(b"opencode:" + self.password.encode()).decode()
        assert request.headers.get("authorization") == expected  # password on (D-091)
        path = request.url.path
        if path == "/event":
            return httpx.Response(200, stream=IterStream(self.stream()))
        if path == "/global/health":
            return httpx.Response(200, json={"healthy": True, "version": "1.17.13"})
        if path == "/session" and request.method == "POST":
            self.sessions += 1
            return httpx.Response(200, json={"id": "ses_one"})
        if path.endswith("/prompt_async"):
            self.message = json.loads(request.content)["messageID"]
            self.busy = True
            return httpx.Response(204)
        if path == "/session/status":
            return httpx.Response(200, json={"ses_one": {"type": "busy" if self.busy else "idle"}})
        if path.endswith("/message"):
            return httpx.Response(200, json=[{"info": self.reply(), "parts": []}])
        if path.endswith("/abort"):
            self.busy = False
            return httpx.Response(200, json=True)
        raise AssertionError(path)

    def workspace(self) -> str:
        return str(self.ws)

    def boundary(self, session: str) -> bool:
        return not self.busy and self.children == 0

    def stop(self) -> None:
        self.running = False


class FakeLauncher:
    def __init__(self, *, wrong_workspace: bool = False) -> None:
        self.servers: dict[str, FakeServer] = {}
        self.wrong = wrong_workspace

    def launch(self, dispatch_id: str, workspace: Path, native_home: Path) -> FakeServer:
        server = FakeServer(workspace.parent if self.wrong else workspace, native_home)
        self.servers[dispatch_id] = server
        return server


def scoped_home(tmp_path: Path) -> Path:
    home = tmp_path / "opencode-home"
    for rel, text in ((AUTH, '{"opencode-go": {"key": "fixture"}}'), (CATALOG, "{}")):
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text(text)
    return home


def port(tmp_path: Path, launcher: Any) -> PerDispatchOpenCodePort:
    return PerDispatchOpenCodePort(
        version="1.17.13",
        launcher=launcher,
        credential=ScopedOpenCodeHome(scoped_home(tmp_path)),
        journal=SessionJournal(tmp_path / "journal"),
        native_root=tmp_path / "native",
        provider_id="opencode-go",
        model_id="glm-5.3-flash",
    )


def workspace(tmp_path: Path, name: str) -> Path:
    ws = tmp_path / "work" / name
    ws.mkdir(parents=True)
    return ws


def test_one_server_per_dispatch_on_its_own_workspace(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    p = port(tmp_path, launcher)
    assert isinstance(p, AgentDriverPort)
    for did in ("dispatch-a", "dispatch-b"):
        prepared = p.prepare({"dispatch_id": did}, "task", workspace(tmp_path, did))
        assert p.start(prepared) == did
    a, b = launcher.servers["dispatch-a"], launcher.servers["dispatch-b"]
    assert a is not b and a.ws != b.ws
    assert a.saw_auth and (tmp_path / "native" / "dispatch-a" / CATALOG).is_file()
    assert p.poll("dispatch-a")["state"] == "running"
    a.busy = False  # an idle event on the stream; the driver's event thread applies it
    deadline = time.monotonic() + 10
    while p.poll("dispatch-a")["state"] != "completed" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert p.poll("dispatch-a")["state"] == "completed"
    receipt = p.collect("dispatch-a")
    assert receipt["process_stopped"] is True and receipt["goal_verified"] is False
    assert not a.running and b.running  # only the collected dispatch's server stopped
    assert not (tmp_path / "native" / "dispatch-a" / AUTH).exists()  # no credential at rest
    p.destroy("dispatch-a")
    with pytest.raises(Hold) as held:
        p.poll("dispatch-a")
    assert held.value.code == "OPENCODE_SERVER_LOST"


def test_a_server_on_another_workspace_is_stopped_and_held(tmp_path: Path) -> None:
    launcher = FakeLauncher(wrong_workspace=True)
    p = port(tmp_path, launcher)
    with pytest.raises(Hold) as held:
        p.prepare({"dispatch_id": "dispatch-a"}, "task", workspace(tmp_path, "a"))
    assert held.value.code == "OPENCODE_WORKSPACE"
    assert not launcher.servers["dispatch-a"].running
    assert not (tmp_path / "native" / "dispatch-a" / AUTH).exists()


def test_cancel_keeps_the_server_until_the_boundary_is_confirmed(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    p = port(tmp_path, launcher)
    p.start(p.prepare({"dispatch_id": "dispatch-a"}, "task", workspace(tmp_path, "a")))
    server = launcher.servers["dispatch-a"]
    server.children = 1  # a tool process is still alive after the abort ack
    assert p.cancel("dispatch-a")["state"] == "cancelling"
    assert server.running
    server.children = 0
    # cancel is repeated until the boundary is confirmed: the event thread applies the idle state
    # asynchronously (driver contract, agent_drivers/http.py:526-544)
    deadline = time.monotonic() + 10
    while (state := p.cancel("dispatch-a")["state"]) != "cancelled" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert state == "cancelled"
    assert not server.running


def test_pause_stops_the_server_and_resume_relaunches_on_the_same_home(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    p = port(tmp_path, launcher)
    ws = workspace(tmp_path, "a")
    p.start(p.prepare({"dispatch_id": "dispatch-a"}, "task", ws))
    # pause is repeated until the stop is confirmed: the first call may still read "cancelling"
    # while the event thread applies the abort (driver contract, agent_drivers/http.py:526-544)
    deadline = time.monotonic() + 10
    while (state := p.pause("dispatch-a")["state"]) != "paused" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert state == "paused"
    assert not launcher.servers["dispatch-a"].running
    checkpoint = p.checkpoint("dispatch-a")  # journal only; the server is gone
    assert checkpoint["native_home"] == str(tmp_path / "native" / "dispatch-a")
    handle = p.resume({"dispatch_id": "dispatch-a2"}, "more", ws, checkpoint)
    resumed = launcher.servers["dispatch-a2"]
    assert handle == "dispatch-a2" and resumed.home == tmp_path / "native" / "dispatch-a"
    assert resumed.sessions == 0 and resumed.message  # same native session, new turn
    with pytest.raises(Hold) as held:
        p.resume({"dispatch_id": "x"}, "more", ws, {**checkpoint, "native_home": str(tmp_path)})
    assert held.value.code == "RESUME_HOME"


def test_the_container_launcher_is_a_stub_pending_the_image_decision(tmp_path: Path) -> None:
    p = port(tmp_path, PendingDockerLauncher())
    with pytest.raises(Hold) as held:
        p.prepare({"dispatch_id": "dispatch-a"}, "task", workspace(tmp_path, "a"))
    assert held.value.code == "OPENCODE_LAUNCHER_PENDING"
    assert not (tmp_path / "native" / "dispatch-a" / AUTH).exists()


def test_the_real_data_dir_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = scoped_home(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(home))
    with pytest.raises(Hold):
        ScopedOpenCodeHome(home)
    with pytest.raises(Hold):
        ScopedOpenCodeHome(tmp_path / "missing")


def run(*args: str) -> str:
    result = CliRunner().invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_opencode_needs_its_own_passing_report_and_can_be_switched(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    inputs = codex_inputs(tmp_path)
    codex_home = tmp_path / "codex-home"
    (codex_home / ".codex").mkdir(parents=True)
    (codex_home / CODEX_AUTH).write_text('{"tokens": "x"}')
    home = tmp_path / "amplai"
    run(
        "ops", "local-init", "--repo", str(repo), "--app", "app",
        "--codex-home", str(codex_home),
        "--container-profile", str(inputs.container_profile),
        "--qualification-report", str(inputs.qualification_report),
        "--egress-profile", str(inputs.egress_profile),
        "--egress-qualification", str(inputs.egress_qualification),
        "--verifier", "check=python3 -c 'import app' | app imports",
        "--home", str(home), "--operator", "pinesky",
    )  # fmt: skip
    config = home / "local.json"
    result = CliRunner().invoke(cli.app, ["ops", "local-driver", "opencode", "--config",
                                          str(config)])  # fmt: skip
    assert "DRIVER_UNKNOWN" in result.output  # not configured yet
    value = json.loads(config.read_text())
    value["opencode"] = {"credential_home": str(scoped_home(tmp_path))}
    value["apps"][0]["opencode_qualification_report"] = str(inputs.qualification_report)
    config.write_text(json.dumps(value))
    # the Codex report has no opencode-server entry: OpenCode is not admitted on it
    with pytest.raises(Hold) as held:
        LocalProductDeployment(config, start_loop=False)
    assert held.value.code == "DRIVER_UNQUALIFIED"
    # without a report for this app OpenCode is simply not a candidate; the switch still works
    value = json.loads(config.read_text())
    value["apps"][0].pop("opencode_qualification_report")
    config.write_text(json.dumps(value))
    run("ops", "local-driver", "opencode", "--disable", "--config", str(config))
    assert json.loads(config.read_text())["opencode"]["enabled"] is False
    dep = LocalProductDeployment(config, start_loop=False)
    try:
        assert dep.config.opencode is not None and dep.config.opencode.enabled is False
        assert set(dep.service.apps["app"].compositions) == {"codex-cli"}
    finally:
        dep.close()


def test_an_opencode_profile_records_http_sse_and_needs_its_own_report(
    tmp_path: Path, deployment: Any
) -> None:
    inputs = codex_inputs(tmp_path)
    container = json.loads(inputs.container_profile.read_text())
    container["tools"]["opencode"] = "1.17.13"  # app_image_up.sh records it (D3)
    inputs.container_profile.write_text(json.dumps(container))
    opencode = replace(inputs, provider="opencode", model="opencode-go/glm-5.3-flash")
    with pytest.raises(Hold) as held:  # no reports["opencode-server"] yet
        install_driver_profile(deployment.store, deployment.scope, opencode, [])
    assert held.value.code == "DRIVER_UNQUALIFIED"
    doc = json.loads(inputs.qualification_report.read_text())
    doc["reports"]["opencode-server"] = {
        "status": "pass", "driver_version": "1.17.13", "model": "opencode-go/glm-5.3-flash",
        "qualification_id": "qualification-opencode-rig",
        "checks": [{"name": "exact_version", "outcome": "pass"}],
        "tool_use": {"outcome": "pass", "long_command": {"outcome": "pass"}},
    }  # fmt: skip
    inputs.qualification_report.write_text(json.dumps(doc))
    refs = install_driver_profile(deployment.store, deployment.scope, opencode, [])
    driver = deployment.store.get(deployment.scope, "driver-capabilities", refs["driver"])
    model = deployment.store.get(deployment.scope, "model-profile", refs["model"])
    assert (driver["driver_id"], driver["transport"]) == ("opencode-server", "http_sse")
    assert model["provider"] == "opencode-go"
    assert ROUTER_ORDER == ("codex-cli", "claude-cli", "opencode-server")


def test_opencode_never_plans_the_first_available_planner_does() -> None:
    from types import SimpleNamespace

    from amplai_foundry.runtime.execution.product import LocalExecutionService

    def pick(installed: Any, driver_id: str, service: Any) -> Any:
        return LocalExecutionService._planner(service, installed, driver_id)

    empty = SimpleNamespace(planner=None, planners={})
    service = SimpleNamespace(planner="codex-planner", planners={})
    both = SimpleNamespace(planners={"codex-cli": "codex-planner", "claude-cli": "claude-planner"})
    assert pick(both, "opencode-server", service) == "codex-planner"
    claude_only = SimpleNamespace(planners={"claude-cli": "claude-planner"})
    assert pick(claude_only, "opencode-server", empty) == "claude-planner"
    assert pick(claude_only, "claude-cli", service) == "claude-planner"
    with pytest.raises(Hold) as held:  # no planner at all
        pick(SimpleNamespace(planners={}), "opencode-server", empty)
    assert held.value.code == "PLANNER_UNAVAILABLE"
    with pytest.raises(Hold):  # other unknown drivers stay strict
        pick(claude_only, "some-other-driver", service)
