"""Transport contract tests. Mock HTTP is NOT live-account qualification.

FakeContainer runs trusted fixture Python, so these tests assert protocol/process
handling, NOT operating-system containment or network egress isolation.
"""

import json
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from amplai_foundry.agent_drivers.cli import CliDriver
from amplai_foundry.agent_drivers.http import BoundHttp, OpenCodeDriver, ResponsesDriver, native_id
from amplai_foundry.agent_drivers.protocol import SessionJournal
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault
from amplai_foundry.sandbox.container import ContainerProfile, ContainerSandbox


class FakeContainer:
    """Known test subprocess. Never represented as a real security sandbox."""

    def __init__(self, script):
        self.script = script
        self.driver = None
        self.confirm = True
        self.commands = []

    def command(self, argv, workspace, run_name, **kwargs):
        self.commands.append((argv, str(workspace), run_name, kwargs))
        return [sys.executable, "-c", self.script]

    def stop(self, name):
        if not self.confirm:
            raise Hold("TEST_UNCONFIRMED", "Injected stop failure")
        p = self.driver.processes.get(name) if self.driver else None
        if p and p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=3)

    def stopped(self, name):
        p = self.driver.processes.get(name) if self.driver else None
        return self.confirm and p is not None and p.poll() is not None

    def destroy(self, name):
        assert self.stopped(name)


def fixture_driver(tmp_path, script, **kwargs):
    sandbox = FakeContainer(script)
    driver = CliDriver(
        "codex",
        "codex",
        "pinned-fixture",
        sandbox,
        SessionJournal(tmp_path / "journal"),
        model="test-model",
        qualified=True,
        **kwargs,
    )
    sandbox.driver = driver
    workspace = tmp_path / "work"
    workspace.mkdir()
    return driver, sandbox, workspace


SUCCESS = (
    'import sys;sys.stdout.write(\'{"type":"thread.started","thread_id":"native_exact"}\\n'
    '{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":2}}\');'
    "sys.stdout.flush()"
)


def wait(driver, handle):
    driver.threads[handle].join(8)
    assert not driver.threads[handle].is_alive()
    return driver.poll(handle)


def test_dev02_cli_real_subprocess_jsonl_tail_and_native_home(tmp_path):
    d, _box, ws = fixture_driver(tmp_path, SUCCESS)
    prepared = d.prepare({"dispatch_id": "dispatch-one"}, "task", ws)
    handle = d.start(prepared)
    record = wait(d, handle)
    assert record["state"] == "completed" and record["session_handle"] == "native_exact"
    assert record["cursor"] == 2 and d.collect(handle)["process_stopped"] is True
    assert record["usage"]["input_tokens"] == 3 and record["usage"]["cost_microunits"] is None
    cp = d.checkpoint(handle)
    assert Path(cp["native_home"]).is_dir() and Path(cp["native_home"]).parent == d.native_root
    assert d.start(prepared) == handle
    d.destroy(handle)


def test_dev02_cli_exact_resume_retains_native_home(tmp_path):
    d, box, ws = fixture_driver(tmp_path, SUCCESS)
    h = d.start(d.prepare({"dispatch_id": "dispatch-one"}, "task", ws))
    wait(d, h)
    cp = d.checkpoint(h)
    h2 = d.resume({"dispatch_id": "dispatch-two"}, "continue same contract", ws, cp)
    wait(d, h2)
    assert d.checkpoint(h2)["native_home"] == cp["native_home"]
    assert box.commands[-1][0][4:6] == ["resume", "native_exact"]
    cp["model"] = "different"
    with pytest.raises(Hold):
        d.resume({"dispatch_id": "dispatch-three"}, "same", ws, cp)


@pytest.mark.parametrize("field", ["command", "workspace", "session", "native_home"])
def test_dev02_cli_prepared_arguments_cannot_be_mutated(tmp_path, field):
    d, _box, ws = fixture_driver(tmp_path, SUCCESS)
    p = d.prepare({"dispatch_id": "dispatch-one"}, "task", ws)
    p[field] = ["sh", "-c", "false"] if field == "command" else "/changed"
    with pytest.raises(Conflict):
        d.start(p)
    assert not d.processes


def test_dev02_cli_concurrent_start_never_spawns_twice(tmp_path):
    d, _box, ws = fixture_driver(tmp_path, SUCCESS)
    p = d.prepare({"dispatch_id": "dispatch-one"}, "task", ws)

    def start(_):
        try:
            return d.start(p)
        except (Conflict, Hold):
            return "blocked"

    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(start, range(4)))
    wait(d, "dispatch-one")
    assert len(d.processes) == 1 and "dispatch-one" in values


@pytest.mark.parametrize(
    "script",
    [
        # zero exit without completion
        'print(\'{"type":"thread.started","thread_id":"native_exact"}\')',
        'print(\'{"type":"new.event"}\')',
        'print(\'{"type":"thread.started","thread_id":"a"}\');print(\'{"type":"thread.started","thread_id":"b"}\')',
        'print(\'{"type":"turn.completed"}\')',  # missing exact session
        'print(\'{"type":"turn.completed","type":"turn.failed"}\')',
    ],
)
def test_dev02_cli_bad_stream_or_missing_receipt_does_not_succeed(tmp_path, script):
    d, _box, ws = fixture_driver(tmp_path, script)
    h = d.start(d.prepare({"dispatch_id": "dispatch-one"}, "task", ws))
    assert wait(d, h)["state"] != "completed"
    with pytest.raises(Hold):
        d.collect(h)


def test_dev02_cli_cancel_confirmed_and_late_collector_does_not_overwrite(tmp_path):
    script = (
        "import time; "
        'print(\'{"type":"thread.started","thread_id":"native_exact"}\',flush=True);'
        "time.sleep(30)"
    )
    d, _box, ws = fixture_driver(tmp_path, script)
    h = d.start(d.prepare({"dispatch_id": "dispatch-one"}, "task", ws))
    for _ in range(100):
        if d.journal.read(h)["session_handle"]:
            break
        time.sleep(0.01)
    assert d.cancel(h)["process_stopped"] is True
    assert wait(d, h)["state"] == "cancelled"
    assert d.checkpoint(h)["session_handle"] == "native_exact"


def test_dev02_cli_unknown_stop_cannot_checkpoint(tmp_path):
    d, box, ws = fixture_driver(tmp_path, "import time;time.sleep(30)")
    h = d.start(d.prepare({"dispatch_id": "dispatch-one"}, "task", ws))
    box.confirm = False
    try:
        with pytest.raises(Hold):
            d.cancel(h)
        with pytest.raises(Hold):
            d.checkpoint(h)
        assert d.journal.read(h)["state"] == "unknown"
    finally:
        box.confirm = True
        d.cancel(h)
        wait(d, h)


def test_dev02_cli_deadline_stops_fixture_process(tmp_path):
    d, _box, ws = fixture_driver(tmp_path, "import time;time.sleep(30)", max_seconds=0.05)
    h = d.start(d.prepare({"dispatch_id": "dispatch-one"}, "task", ws))
    wait(d, h)
    for _ in range(100):
        if d.journal.read(h)["state"] == "cancelled":
            break
        time.sleep(0.01)
    assert d.journal.read(h)["state"] == "cancelled"
    assert d.processes[h].poll() is not None


def test_dev02_cli_orphan_start_requires_reconciliation(tmp_path):
    d, _box, ws = fixture_driver(tmp_path, SUCCESS)
    p = d.prepare({"dispatch_id": "dispatch-one"}, "task", ws)
    d.journal.transition("dispatch-one", {"prepared"}, "starting")
    with pytest.raises(Hold):
        d.start(p)
    assert not d.processes


@pytest.mark.parametrize("key", ["HOME", "PATH", "DOCKER_HOST", "LD_PRELOAD", "NODE_OPTIONS"])
def test_dev02_credential_injection_does_not_reconfigure_launcher(tmp_path, key):
    with pytest.raises(Hold):
        fixture_driver(tmp_path, SUCCESS, environment={key: "malicious"})


@pytest.mark.parametrize("session", ["latest", "continue", "--continue", "", True])
def test_dev02_cli_rejects_unpinned_native_resume(tmp_path, session):
    d, _box, ws = fixture_driver(tmp_path, SUCCESS)
    with pytest.raises(Hold):
        d.prepare({"dispatch_id": "dispatch-one"}, "task", ws, session=session)


def test_dev02_container_command_has_no_host_escape_mount(tmp_path):
    box = ContainerSandbox(ContainerProfile("registry/worker@sha256:" + "a" * 64))
    ws = tmp_path / "work"
    ws.mkdir()
    home = tmp_path / "native"
    home.mkdir()
    cmd = box.command(
        ["codex", "--version"], ws, "run-one", native_home=home, env_names=["OPENAI_API_KEY"]
    )
    assert "--read-only" in cmd and "--cap-drop=ALL" in cmd and "--network" in cmd
    assert cmd[cmd.index("--network") + 1] == "none"
    assert "--privileged" not in cmd and "/var/run/docker.sock" not in str(cmd)
    assert cmd[cmd.index("--user") + 1] == "65534:65534"
    with pytest.raises(Hold):
        box.command(["true"], ws, "run-one", env_names=["HOME"])
    with pytest.raises(Hold):
        ContainerSandbox(ContainerProfile("registry/worker:latest"))
    with pytest.raises(Hold):
        ContainerSandbox(ContainerProfile("registry/worker@sha256:" + "a" * 64, network="bridge"))


class OpenCodeFixture:
    def __init__(self):
        self.requests = []
        self.busy = True
        self.stopped = False
        self.cross = False
        self.created = 0
        self.message = None

    def __call__(self, request):
        self.requests.append((request.method, request.url.path))
        if request.url.path == "/global/health":
            return httpx.Response(200, json={"healthy": True, "version": "fixture-1"})
        if request.url.path == "/session" and request.method == "POST":
            self.created += 1
            return httpx.Response(200, json={"id": "ses_exact"})
        if request.url.path.endswith("/prompt_async"):
            self.message = json.loads(request.content)["messageID"]
            return httpx.Response(204)
        if request.url.path == "/session/status":
            return httpx.Response(
                200, json={"ses_exact": {"type": "busy" if self.busy else "idle"}}
            )
        if request.url.path.endswith("/message"):
            return httpx.Response(
                200,
                json=[
                    {
                        "info": {
                            "id": "msg_reply",
                            "sessionID": "ses_other" if self.cross else "ses_exact",
                            "role": "assistant",
                            "parentID": self.message,
                            "time": {"completed": 1},
                        },
                        "parts": [],
                    }
                ],
            )
        if request.url.path.endswith("/abort"):
            return httpx.Response(200, json=True)
        raise AssertionError(str(request.url))


def open_code(tmp_path):
    f = OpenCodeFixture()
    d = OpenCodeDriver(
        "http://127.0.0.1:4096",
        "opencode",
        "test-only",
        SessionJournal(tmp_path / "s"),
        provider_id="local",
        model_id="pinned-model",
        qualified=True,
        transport=httpx.MockTransport(f),
        allow_local=True,
        expected_version="fixture-1",
        boundary_probe=lambda _: f.stopped,
    )
    return d, f


def test_dev02_opencode_204_is_acceptance_not_done(tmp_path):
    d, f = open_code(tmp_path)
    x = {"dispatch_id": "dispatch-one"}
    result = d.start(x, "task")
    assert result["completed"] is False and d.poll("dispatch-one")["state"] == "running"
    f.busy = False
    assert (
        d.poll("dispatch-one")["state"] == "running"
    )  # idle and completed message still need containment stop
    f.stopped = True
    assert d.poll("dispatch-one")["state"] == "completed"
    assert d.collect("dispatch-one")["goal_verified"] is False
    d.start(x, "task")
    assert f.created == 1
    assert (
        d.http.client.headers.get("Authorization") is None
    )  # BasicAuth injected only into requests, not persisted journal


def test_dev02_opencode_abort_ack_is_not_termination(tmp_path):
    d, f = open_code(tmp_path)
    d.start({"dispatch_id": "dispatch-one"}, "task")
    assert d.cancel("dispatch-one")["state"] == "cancelling"
    f.busy = False
    assert d.poll("dispatch-one")["state"] == "cancelling"
    f.stopped = True
    assert d.poll("dispatch-one")["state"] == "cancelled"
    assert d.poll("dispatch-one")["state"] == "cancelled"  # late message cannot win
    assert d.checkpoint("dispatch-one")["session_handle"] == "ses_exact"


def test_dev02_opencode_cross_session_or_profile_fails(tmp_path):
    d, f = open_code(tmp_path)
    d.start({"dispatch_id": "dispatch-one"}, "task")
    f.busy = False
    f.cross = True
    with pytest.raises(Hold):
        d.poll("dispatch-one")
    with pytest.raises(Conflict):
        d.start({"dispatch_id": "dispatch-one"}, "different task")
    d.expected_version = "another-version"
    with pytest.raises(Hold):
        d.probe()


@pytest.mark.parametrize(
    "identifier", ["../escape", "/bad", "latest", "a?query", "a%2Fbad", "a\\b"]
)
def test_dev02_http_native_id_cannot_change_endpoint(identifier):
    with pytest.raises(Hold):
        native_id(identifier)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://a:b@example.com",
        "https://example.com/#fragment",
        "https://example.com/?query=1",
    ],
)
def test_dev02_http_endpoint_no_ambient_auth_or_insecure_transport(url):
    with pytest.raises(RuntimeFault):
        BoundHttp(url)


def test_dev02_http_no_redirect_or_path_escape():
    h = BoundHttp(
        "https://trusted.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"location": "https://attacker.invalid"})
        ),
    )
    with pytest.raises(Hold):
        h.request("GET", "data")
    for path in ["//attacker.invalid", "../outside", "a%2f..", "a\\b", "http://attacker.invalid"]:
        with pytest.raises(RuntimeFault):
            h.request("GET", path)


def responses(tmp_path, output):
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=output)

    return ResponsesDriver(
        "not-a-real-key",
        SessionJournal(tmp_path / "s"),
        model="pinned-model",
        qualified=True,
        transport=httpx.MockTransport(transport),
    ), requests


def test_dev02_responses_privacy_and_exact_tool_pairing(tmp_path):
    d, requests = responses(
        tmp_path,
        {
            "id": "resp_exact",
            "status": "completed",
            "output": [
                {"type": "function_call", "call_id": "call_a", "name": "write", "arguments": "{}"}
            ],
        },
    )
    result = d.start(
        {"dispatch_id": "dispatch-one"},
        "task",
        max_output_tokens=20,
        tools=[{"type": "function", "name": "write", "parameters": {"type": "object"}}],
    )
    assert result["state"] == "awaiting_tools" and result["goal_verified"] is False
    assert requests[0]["store"] is False and requests[0]["parallel_tool_calls"] is False
    assert (
        d.tool_outputs("dispatch-one", [{"provider_call_id": "call_a", "output": "ok"}])[0][
            "call_id"
        ]
        == "call_a"
    )
    for values in [
        [],
        [{"provider_call_id": "call_b", "output": "ok"}],
        [
            {"provider_call_id": "call_a", "output": "1"},
            {"provider_call_id": "call_a", "output": "2"},
        ],
    ]:
        with pytest.raises(Hold):
            d.tool_outputs("dispatch-one", values)
    with pytest.raises(Conflict):
        d.start({"dispatch_id": "dispatch-one"}, "task", max_output_tokens=20, tools=[])


def test_dev02_responses_duplicate_native_call_rejected(tmp_path):
    call = {"type": "function_call", "call_id": "call_a", "name": "write", "arguments": "{}"}
    d, requests = responses(
        tmp_path, {"id": "resp_exact", "status": "completed", "output": [call, call]}
    )
    with pytest.raises(Hold):
        d.start({"dispatch_id": "dispatch-one"}, "task", max_output_tokens=20)
    d.start({"dispatch_id": "dispatch-one"}, "task", max_output_tokens=20)
    assert len(requests) == 1  # Interrupted turn is not sent twice.


def test_dev02_opencode_resumes_only_existing_exact_native_session(tmp_path):
    d, f = open_code(tmp_path)
    d.start({"dispatch_id": "dispatch-one"}, "first")
    f.busy = False
    f.stopped = True
    cp = d.checkpoint("dispatch-one")
    result = d.resume({"dispatch_id": "dispatch-two"}, "continue same goal", cp)
    assert (
        result["state"] == "running" and result["session_handle"] == "ses_exact" and f.created == 1
    )
    assert f.requests.count(("POST", "/session")) == 1
    changed = deepcopy(cp)
    changed["session_handle"] = "ses_other"
    with pytest.raises(Hold):
        d.resume({"dispatch_id": "dispatch-three"}, "same", changed)
