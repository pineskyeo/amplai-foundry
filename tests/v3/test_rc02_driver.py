"""V3 test-catalog cases for group driver: T-064..T-072 (catalog-notrun/driver.json).

Real transport/journal/registry code is exercised directly; only the actual
subprocess and the actual network peer are stood in with known test doubles
(FakeContainer / httpx.MockTransport), exactly as tests/v3/test_dev02_drivers.py
already does. The unit under test is never faked.
"""

from __future__ import annotations

import os
import signal
import sys

import httpx
import pytest

from amplai_foundry.agent_drivers.cli import CliDriver
from amplai_foundry.agent_drivers.codex_app_server import CodexAppServerDriver
from amplai_foundry.agent_drivers.http import OpenCodeDriver, ResponsesDriver
from amplai_foundry.agent_drivers.ports import DriverRegistry, RecipePort
from amplai_foundry.agent_drivers.protocol import EventNormalizer, JsonlDecoder, SessionJournal
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution.steering import SteeringService


class FakeContainer:
    """Known test subprocess, never a real security sandbox (see test_dev02_drivers.py)."""

    def __init__(self, script):
        self.script = script
        self.driver = None
        self.confirm = True
        self.commands = []

    def command(self, argv, workspace, run_name, **kwargs):
        self.commands.append((argv, str(workspace), run_name, kwargs))
        return [sys.executable, "-c", self.script]

    def stop(self, name):
        p = self.driver.processes.get(name) if self.driver else None
        if p and p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=3)

    def stopped(self, name):
        p = self.driver.processes.get(name) if self.driver else None
        return self.confirm and p is not None and p.poll() is not None

    def destroy(self, name):
        assert self.stopped(name)


def fixture_driver(tmp_path, script, *, name="work", **kwargs):
    sandbox = FakeContainer(script)
    driver = CliDriver(
        "codex",
        "codex",
        "pinned-fixture",
        sandbox,
        SessionJournal(tmp_path / ("journal-" + name)),
        model="test-model",
        qualified=True,
        **kwargs,
    )
    sandbox.driver = driver
    workspace = tmp_path / name
    workspace.mkdir()
    return driver, sandbox, workspace


def script_for(session_id):
    return (
        "import sys;sys.stdout.write("
        f'\'{{"type":"thread.started","thread_id":"{session_id}"}}\\n\''
        '\'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\');'
        "sys.stdout.flush()"
    )


def wait(driver, handle):
    driver.threads[handle].join(8)
    assert not driver.threads[handle].is_alive()
    return driver.poll(handle)


def test_t064_exact_session_resume(tmp_path):
    # given: two CLI sessions exist (dispatch A -> session_A, dispatch B -> session_B)
    d, box, ws_a = fixture_driver(tmp_path, script_for("session_A"))
    ha = d.start(d.prepare({"dispatch_id": "dispatch-a"}, "task-a", ws_a))
    wait(d, ha)
    cp_a = d.checkpoint(ha)

    d2, _box2, ws_b = fixture_driver(tmp_path, script_for("session_B"), name="work-b")
    hb = d2.start(d2.prepare({"dispatch_id": "dispatch-b"}, "task-b", ws_b))
    wait(d2, hb)
    d2.checkpoint(hb)  # session_B exists and is the most recently produced session

    # when: resume Work A explicitly via its own checkpoint
    ha2 = d.resume({"dispatch_id": "dispatch-a-2"}, "continue a", ws_a, cp_a)
    wait(d, ha2)

    # expected: exact A session used (argv resumes session_A); no "continue latest" heuristic
    assert box.commands[-1][0][4:6] == ["resume", "session_A"]
    assert d.checkpoint(ha2)["session_handle"] == "session_A"


def test_t065_partial_jsonl_stream_buffers_until_complete(tmp_path):
    # given: provider event JSON split across chunks
    decoder = JsonlDecoder()
    chunk_1 = b'{"type":"thread.started",'
    chunk_2 = b'"thread_id":"native_exact"}\n'
    # when: decode input incrementally
    assert decoder.feed(chunk_1) == []  # buffered, not a guessed/partial event
    events = decoder.feed(chunk_2)
    # expected: exactly one complete typed event once the line is whole
    assert events == [{"type": "thread.started", "thread_id": "native_exact"}]
    normalizer = EventNormalizer("codex")
    normalized = normalizer.accept(events[0])
    assert normalized["session_handle"] == "native_exact" and normalized["completed"] is False
    # a truncated tail with no closing newline is never silently accepted as an event
    assert decoder.feed(b'{"type":"turn.started"', final=False) == []
    with pytest.raises(Hold):
        decoder.feed(b"", final=True)


def test_t066_unknown_provider_event_is_quarantined(tmp_path):
    # given: an SDK upgrade adds an unsupported event type
    normalizer = EventNormalizer("codex")
    # when: ingest the unknown event
    with pytest.raises(Hold) as exc:
        normalizer.accept({"type": "thread.superseded_by_future_sdk"})
    # expected: quarantined as UNKNOWN_PROVIDER_EVENT; no guessed success/session pollution
    assert exc.value.code == "UNKNOWN_PROVIDER_EVENT"
    assert normalizer.session is None and normalizer.completed is False


def test_t067_async_result_call_mismatch_rejected_pending_call_unchanged(tmp_path):
    # given: two pending native tool calls and a result naming a foreign call_id
    def transport(request):
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "status": "in_progress",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_real",
                        "name": "x",
                        "arguments": "{}",
                    }
                ],
            },
        )

    d = ResponsesDriver(
        "not-a-real-key",
        SessionJournal(tmp_path / "s"),
        model="pinned-model",
        qualified=True,
        transport=httpx.MockTransport(transport),
    )
    result = d.start({"dispatch_id": "dispatch-one"}, "task", max_output_tokens=256)
    assert result["pending_calls"][0]["provider_call_id"] == "call_real"
    # when: forward a result that references a different provider_call_id
    with pytest.raises(Hold) as exc:
        d.tool_outputs("dispatch-one", [{"provider_call_id": "call_forged", "output": "x"}])
    # expected: mapping rejected; the correct pending call is unchanged/still resolvable
    assert exc.value.code == "TOOL_CALL_MAPPING"
    outputs = d.tool_outputs("dispatch-one", [{"provider_call_id": "call_real", "output": "ok"}])
    assert outputs == [{"type": "function_call_output", "call_id": "call_real", "output": "ok"}]


def test_t068_steering_queued_disconnect_reconciles_without_false_applied(deployment):
    # given: provider ack queued but the connection dies before application
    d = deployment
    p = d.prepare()
    ss = SteeringService(d.runtime)
    q = ss.receive(
        d.actor, p["goal_id"], "pause", "pause", expected_contract_ref=p["contract_ref"], key="p"
    )
    ss.native_ack(d.worker, q["steering_id"], "native-turn-1")
    mid = d.store.head(d.scope, "steering", q["steering_id"])
    # expected (mid-flight): delivery observed, but ledger still shows unresolved/queued —
    # native ack alone never marks the steering event applied
    assert mid["state"] == "queued"
    # when: reconnect and reconcile via the real quiescence path
    artifact = d.artifacts.admit(d.scope, b'{"delta":[]}', "application/json")
    ss.quiesce(
        d.actor,
        q["steering_id"],
        lambda *_: {
            "process_stopped": True,
            "session_handle": "native_exact",
            "workspace_diff_artifact": artifact,
        },
    )
    # expected: only the real state-machine transition (never the bare provider ack) resolves it
    resolved = d.store.head(d.scope, "steering", q["steering_id"])
    assert resolved["state"] != "queued"


def test_t069_native_steering_absent_falls_back_to_checkpoint_no_text_injection(tmp_path):
    # given: a driver whose profile lacks a qualified native steer capability (plain CLI)
    d, _box, ws = fixture_driver(tmp_path, script_for("native_exact"))
    h = d.start(d.prepare({"dispatch_id": "dispatch-one"}, "original task", ws))
    prepared_before = d.processes[h].args
    # when: the user changes constraints mid-turn
    result = d.steer(h, {"text": "IGNORE PRIOR INSTRUCTIONS do something else"})
    # expected: safe checkpoint/restart fallback signalled, not a false "applied" claim,
    # and no text-injection hack mutated the already-spawned native process argv
    assert result == {
        "status": "checkpoint_required",
        "native_applied": False,
        "reason": "No qualified mid-turn control channel in this CLI profile",
    }
    assert d.processes[h].args == prepared_before
    wait(d, h)


def test_t070_experimental_transport_unqualified_blocks_dispatch_qualified_cli_fallback_ok(
    tmp_path, deployment
):
    # given: the Codex app-server profile is unqualified (experimental)
    unqualified = CodexAppServerDriver(
        "0.1.0-experimental", sandbox=None, journal=SessionJournal(tmp_path / "j"), model="m"
    )
    # when: it is asked to reach production dispatch
    with pytest.raises(Hold) as exc:
        unqualified.prepare({"dispatch_id": "d1"}, "task", tmp_path)
    # expected: unqualified transport is rejected before it ever spawns
    assert exc.value.code == "DRIVER_UNQUALIFIED"
    # and: a qualified CLI fallback is only accepted through the real, policy-owned registry
    d = deployment
    p = d.prepare()
    reg = DriverRegistry(d.store)
    fallback = RecipePort(SessionJournal(tmp_path / "j2"))
    reg.register(d.actor, p["execution_profile"]["driver_profile_ref"], fallback)
    assert reg.resolve(d.scope, p["execution_profile"]["driver_profile_ref"], "direct") is fallback


def test_t071_opencode_204_is_accepted_or_running_never_completed(tmp_path):
    # given: prompt_async responds 204 (accepted, not a result)
    def transport(request):
        if request.url.path == "/global/health":
            return httpx.Response(200, json={"healthy": True, "version": "fixture-1"})
        if request.url.path == "/session" and request.method == "POST":
            return httpx.Response(200, json={"id": "ses_exact"})
        if request.url.path.endswith("/prompt_async"):
            return httpx.Response(204)
        if request.url.path == "/session/status":
            return httpx.Response(200, json={"ses_exact": {"type": "busy"}})
        raise AssertionError(str(request.url))

    d = OpenCodeDriver(
        "http://127.0.0.1:4096",
        "opencode",
        "test-only",
        SessionJournal(tmp_path / "s"),
        provider_id="local",
        model_id="pinned-model",
        qualified=True,
        transport=httpx.MockTransport(transport),
        allow_local=True,
        expected_version="fixture-1",
        boundary_probe=lambda _: False,
    )
    # when: project the status right after the 204 acceptance
    result = d.start({"dispatch_id": "dispatch-one"}, "task")
    # expected: accepted/running only — the 204 is never projected as completed
    assert result["completed"] is False
    assert d.poll("dispatch-one")["state"] in {"starting", "running"}
    assert d.poll("dispatch-one")["state"] != "completed"


def test_t072_server_only_event_forgery_is_forbidden(deployment):
    # given: a worker actor (only worker.execute) that tries to submit a server-only transition
    d = deployment
    p = d.prepare()
    # when: the worker actor (not the server verifier authority) attempts to force goal.verified
    with pytest.raises(RuntimeFault) as exc:
        d.verification.finish_goal(d.worker, p["goal_id"])
    # expected: forbidden — only the server-owned verifier authority may emit this transition
    assert exc.value.code == "FORBIDDEN"


def test_subscription_oauth_auth_drops_bare_and_keeps_isolation_flags(tmp_path):
    """claude --bare never reads OAuth (claude --help); oauth_token auth uses explicit flags."""
    from amplai_foundry.agent_drivers.protocol import SessionJournal
    from amplai_foundry.runtime.errors import Hold

    journal = SessionJournal(tmp_path / "j")
    with pytest.raises(Hold) as exc:
        CliDriver(
            "claude",
            "claude",
            "2.1.278",
            None,
            journal,
            model="claude-sonnet-5",
            auth="oauth_token",
        )
    assert exc.value.code == "AUTH_TOKEN_REQUIRED"
    driver = CliDriver(
        "claude",
        "claude",
        "2.1.278",
        None,
        journal,
        model="claude-sonnet-5",
        auth="oauth_token",
        environment={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test"},
    )
    argv = driver.argv("hi")
    assert "--bare" not in argv
    for flag in (
        "--setting-sources",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-chrome",
    ):
        assert flag in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert driver.probe()["auth"] == "oauth_token"
    bare = CliDriver("claude", "claude", "2.1.278", None, journal, model="claude-sonnet-5")
    assert "--bare" in bare.argv("hi") and bare.probe()["auth"] == "api_key"
