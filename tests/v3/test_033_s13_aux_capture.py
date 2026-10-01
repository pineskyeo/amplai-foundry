"""Work 033 S13: auxiliary read-only turns join the run trace; a steered run keeps its trace
(interfaces.md §9.1, §9.2, §3.12, §2.11, D-100).

Contract: §9.1 row "Planner / reviewer / investigator read-only turns" (the same sanitizer when the
plan carries a trial with ``capture_trace``; stored as turn ``"planner"``, ``"reviewer"``,
``"investigator-<k>"`` of the same trace) and "Only trial goals of the corpus capture; real goals
never do"; §9.2 secret scan (any hit -> ``trace-drop`` ``secret_pattern``, nothing stored); §3.12
``WorkCoordinator`` trace sink.

Real: store, CAS, ``TraceService``, ``WorkCoordinator`` (``_admit_trace``, ``execute`` and
``continue_resumed`` through the execution loop and operator steering), ``StrategyRunner``
(``_aux_turn``, the pending named turns), the Codex CLI driver with its trace buffers. Stand-ins:
the rc06 host-process container, scripted read-only turns. No driver, docker or network.

The carriers (§9.1, the clarifications after the S10/S13/S14 fix wave): a run-time turn
(reviewer, investigator) reaches the worker through the ``aux_traces`` argument of ``execute`` and
``continue_resumed`` (``StrategyRunner.trace_source``), which ``ExecutionLoop._execute`` passes; a
plan-time turn goes back to the planner through ``PlanContext.snapshots`` (the goal-level planner
trace, ``product.plan``). The end-to-end strategy cases live in
``tests/e2e/test_033_s9_strategies.py``. IC-28 (provisional): an executor turn's own credential
literal drops its trace (``agent_drivers/cli.py``), tested in ``test_033_s13_traces_acl.py``.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from amplai_foundry.meta_harness import traces
from amplai_foundry.meta_harness.traces import (
    DROP_KIND,
    TRACE_KIND,
    TraceBuffer,
    TraceService,
    trace_sink,
)
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution.cells import DispatchOptions
from amplai_foundry.runtime.execution.product import TrialContext
from amplai_foundry.runtime.execution.readonly_turn import ReadOnlyTurn, TurnResult
from amplai_foundry.runtime.execution.strategy_runner import (
    AuxLedger,
    PlanContext,
    StrategyRunner,
    captures,
)
from amplai_foundry.runtime.execution.worker import WorkCoordinator, named_turns
from rc06_rig import rig_with_codex, submit, with_permissions

SECRET = "password: Zq9wXk3LmN8vBc2RtY6uHj4P"
CAPTURE = DispatchOptions("m", None, capture_trace=True)


def snap(*texts: str) -> dict[str, Any]:
    """A buffer snapshot holding one Codex agent message per text (the sanitizer's own path)."""
    buffer = TraceBuffer("codex")
    for text in texts:
        buffer.add({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
    return buffer.snapshot()


class TracePort:
    driver_id = "codex-cli"

    def __init__(self, by_handle: dict[str, Any]) -> None:
        self.by_handle = by_handle

    def trace(self, handle: str) -> Any:
        return self.by_handle.get(handle)


def admit(
    aux: Any, options: DispatchOptions | None = CAPTURE, port: Any = None
) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    me = SimpleNamespace(trace_sink=lambda run_id, value: calls.append((run_id, value)))
    WorkCoordinator._admit_trace(  # type: ignore[arg-type]
        me, {"run_id": "run-1"}, port or TracePort({"d1": snap("executor")}), ["d1"], options, aux
    )
    return calls


# ==================================================================================================
# the worker: one admission, executor turns then the named auxiliary turns
# ==================================================================================================
def test_the_named_auxiliary_turns_follow_the_executor_turns_in_one_trace() -> None:
    aux = [("planner", snap("plan")), ("investigator-1", snap("look")), ("reviewer", snap("rev"))]
    ((run_id, value),) = admit(lambda: aux)
    assert run_id == "run-1"
    assert [t["turn"] for t in value["turns"]] == [0, "planner", "investigator-1", "reviewer"]
    assert [t["items"][0]["text"] for t in value["turns"]] == ["executor", "plan", "look", "rev"]
    traces.validate_sanitized(value)


@pytest.mark.parametrize(
    "options", [None, DispatchOptions("m", None), DispatchOptions("m", "high")]
)
def test_a_non_capturing_dispatch_never_asks_for_the_auxiliary_turns(options: Any) -> None:
    asked: list[bool] = []

    def aux() -> list[tuple[str, dict[str, Any]]]:
        asked.append(True)
        return [("reviewer", snap("x"))]

    assert admit(aux, options) == [] and asked == []


def test_without_an_executor_turn_the_auxiliary_turns_are_not_taken() -> None:
    asked: list[bool] = []

    def aux() -> list[tuple[str, dict[str, Any]]]:
        asked.append(True)
        return []

    assert admit(aux, port=TracePort({})) == [] and asked == []


@pytest.mark.parametrize(
    "entry",
    [
        ("lead", {}),  # not a §9.1 name (traces.AUX_TURN)
        ("investigator-100", {}),
        ("reviewer", "not a snapshot"),
        (0, {}),  # an int turn is the worker's own (executor turns only)
        ("reviewer",),
        "reviewer",
        None,
    ],
)
def test_a_malformed_auxiliary_entry_is_left_out(entry: Any) -> None:
    good = ("reviewer", snap("kept"))
    assert named_turns(lambda: [entry, good]) == [good]


def test_a_failing_source_gives_no_auxiliary_turn_and_the_run_trace_is_still_admitted() -> None:
    def broken() -> list[tuple[str, dict[str, Any]]]:
        raise RuntimeFault("BROKEN", "source down")

    assert named_turns(broken) == [] and named_turns(None) == []
    ((_run, value),) = admit(broken)
    assert [t["turn"] for t in value["turns"]] == [0]


def test_a_foreign_auxiliary_snapshot_is_a_sanitizer_error_for_admission() -> None:
    ((_run, value),) = admit(lambda: [("reviewer", {"items": [{"text": "raw"}]})])
    assert value["errors"] == 1  # combine counts it; TraceService.admit drops sanitizer_error
    assert [t["turn"] for t in value["turns"]] == [0]


# ==================================================================================================
# the secret scan covers every auxiliary turn: the whole trace is dropped
# ==================================================================================================
def trial_head(d: Any, trial_id: str) -> None:
    with d.store.tx() as db:
        d.store.cas(db, d.scope, "eval-trial", trial_id, 0, "running", {"task_id": "bug-dev-00"})


def context(trial_id: str, capture: bool = True) -> TrialContext:
    return TrialContext(
        subject={"experiment_id": "exp-1", "trial_id": trial_id}, arm="candidate",
        cell_id="codex-cli", split="development", capture_trace=capture, planner_mode="fixed",
        environment_id="app",
    )  # fmt: skip


def service_admit(d: Any, trial_id: str) -> Any:
    service = TraceService(d.store, d.scope, d.artifacts)

    def sink(run_id: str, value: dict[str, Any]) -> None:
        service.admit(
            run_id=run_id, goal_id="goal-1", trial=context(trial_id), driver_id="codex-cli",
            sanitized=value,
        )  # fmt: skip

    return sink


@pytest.mark.parametrize("name", ["reviewer", "investigator-2", "planner"])
def test_a_secret_inside_an_auxiliary_turn_drops_the_whole_trace(
    deployment: Any, name: str
) -> None:
    d = deployment
    trial_head(d, "trial-secret")
    me = SimpleNamespace(trace_sink=service_admit(d, "trial-secret"))
    port = TracePort({"d1": snap("clean executor text")})
    aux = [("planner", snap("clean plan")), (name, snap("found " + SECRET))]
    WorkCoordinator._admit_trace(  # type: ignore[arg-type]
        me, {"run_id": "run-secret"}, port, ["d1"], CAPTURE, lambda: aux
    )
    assert list(d.store.list_objects(d.scope, TRACE_KIND)) == []  # nothing stored, not even turn 0
    ((_ref, drop),) = list(d.store.list_objects(d.scope, DROP_KIND))
    assert drop["run_id"] == "run-secret" and drop["reason"] == "secret_pattern"
    assert drop["patterns"] and all(p.startswith("secret-pattern-") for p in drop["patterns"])
    assert SECRET not in json.dumps(drop)


def test_clean_auxiliary_turns_are_stored_as_named_turns(deployment: Any) -> None:
    d = deployment
    trial_head(d, "trial-clean")
    me = SimpleNamespace(trace_sink=service_admit(d, "trial-clean"))
    aux = [("investigator-1", snap("look")), ("reviewer", snap("review"))]
    WorkCoordinator._admit_trace(  # type: ignore[arg-type]
        me, {"run_id": "run-clean"}, TracePort({"d1": snap("exec")}), ["d1"], CAPTURE,
        lambda: aux,
    )  # fmt: skip
    ((_ref, record),) = list(d.store.list_objects(d.scope, TRACE_KIND))
    body = json.loads(d.artifacts.read(d.scope, record["artifact"]))
    assert [t["turn"] for t in body["turns"]] == [0, "investigator-1", "reviewer"]


# ==================================================================================================
# the runner: capture only when asked, pending named turns handed once
# ==================================================================================================
class StrictTurn:
    """A read-only turn; ``legacy`` has today's signature without ``capture_trace``."""

    cell_id = "codex-cli"

    def __init__(self, *, trace: dict[str, Any] | None = None, fail: Hold | None = None) -> None:
        self.trace, self.fail = trace, fail
        self.asked: list[bool] = []

    def run(
        self, *, prompt: str, schema: dict[str, Any], workspace: Path, mounts: Any = None,
        capture_trace: bool = False,
    ) -> TurnResult:  # fmt: skip
        self.asked.append(capture_trace)
        if self.fail is not None:
            raise self.fail
        return TurnResult(
            {"ok": True}, {"input_tokens": 1, "output_tokens": 1}, 0.0, "sha256:" + "0" * 64,
            self.trace if capture_trace else None,
        )  # fmt: skip


class LegacyTurn:
    cell_id = "codex-cli"

    def run(
        self, *, prompt: str, schema: dict[str, Any], workspace: Path, mounts: Any = None
    ) -> TurnResult:
        return TurnResult({"ok": True}, None, 0.0, "sha256:" + "0" * 64)


def runner_with(turn: Any) -> StrategyRunner:
    service = SimpleNamespace(store=None, scope=None)
    return StrategyRunner(service, lambda cell: turn, queue=None)  # type: ignore[arg-type]


def aux_turn(runner: StrategyRunner, on_trace: Any) -> dict[str, Any]:
    return runner._aux_turn(
        AuxLedger(10_000), role="reviewer", purpose="review", cell_id="codex-cli",
        prompt="p", schema={}, workspace=Path("."), node_id="node-app", on_trace=on_trace,
    )  # fmt: skip


def test_a_turn_runs_with_capture_trace_only_when_its_goal_captures() -> None:
    turn = StrictTurn(trace=snap("seen"))
    runner = runner_with(turn)
    kept: list[dict[str, Any]] = []
    assert aux_turn(runner, None) == {"ok": True}
    assert aux_turn(runner, kept.append) == {"ok": True}
    assert turn.asked == [False, True] and kept == [snap("seen")]


def test_a_non_capturing_goal_calls_a_turn_exactly_as_before() -> None:
    # a turn implementation without the S13 keyword still works for every non-capturing goal
    assert aux_turn(runner_with(LegacyTurn()), None) == {"ok": True}


def test_a_turn_without_a_trace_or_a_failed_turn_keeps_nothing() -> None:
    kept: list[dict[str, Any]] = []
    aux_turn(runner_with(StrictTurn(trace=None)), kept.append)
    with pytest.raises(Hold) as held:
        aux_turn(runner_with(StrictTurn(fail=Hold("TURN_FAILED", "no reply"))), kept.append)
    assert held.value.code == "TURN_FAILED" and kept == []


def test_a_turn_at_the_cap_holds_aux_budget_before_it_runs_and_keeps_nothing() -> None:
    turn = StrictTurn(trace=snap("x"))
    kept: list[dict[str, Any]] = []
    with pytest.raises(Hold) as held:
        runner_with(turn)._aux_turn(
            AuxLedger(0), role="reviewer", purpose="review", cell_id="codex-cli", prompt="p",
            schema={}, workspace=Path("."), on_trace=kept.append,
        )  # fmt: skip
    assert held.value.code == "AUX_BUDGET" and turn.asked == [] and kept == []


def test_the_pending_turns_go_once_to_their_own_nodes_run() -> None:
    runner = runner_with(LegacyTurn())
    runner._keep_trace("g", "node-app.p1", "investigator-1", snap("a"))
    runner._keep_trace("g", "node-app.p2", "investigator-1", snap("b"))
    runner._keep_trace("other", "node-app.p1", "reviewer", snap("c"))
    first = runner.trace_source("g", "node-app.p1")()
    assert [n for n, _s in first] == ["investigator-1"]
    assert first[0][1] == snap("a")
    assert runner.aux_traces("g", "node-app.p1") == []  # handed once
    assert [s for _n, s in runner.aux_traces("g", "node-app.p2")] == [snap("b")]
    assert runner.aux_traces("g", "node-app.p2") == []
    assert [n for n, _s in runner.aux_traces("other", "node-app.p1")] == ["reviewer"]


def test_a_stale_reviewer_turn_is_dropped_and_forget_drops_the_rest() -> None:
    runner = runner_with(LegacyTurn())
    runner._keep_trace("g", "node-app", "investigator-1", snap("a"))
    runner._keep_trace("g", "node-app", "reviewer", snap("old review"))
    runner._drop_traces("g", "node-app", "reviewer")  # what before_attempt does
    assert [n for n, _s in runner.aux_traces("g", "node-app")] == ["investigator-1"]
    runner._keep_trace("g", "node-app", "reviewer", snap("r"))
    runner.forget("g")
    assert runner.aux_traces("g", "node-app") == []


def test_a_plan_time_turn_goes_back_through_the_plan_context_and_the_runner_keeps_nothing(
    tmp_path: Path,
) -> None:
    """§9.1: a plan-time turn of a capturing trial is appended to ``PlanContext.snapshots`` (the
    planner admits it as a "planner" turn of the goal-level trace); the runner holds no plan-time
    turn in memory, so no run trace can carry one."""
    turn = StrictTurn(trace=snap("lead"))
    runner = runner_with(turn)
    runner.service.workspaces = SimpleNamespace(
        materialize=lambda *_a: tmp_path, discard=lambda _p: None
    )
    choice = SimpleNamespace(roles={"planner": "codex-cli"}, params={})
    draft = {"objective": "o", "in_scope": ["app.py"], "acceptance": []}
    off = PlanContext("g", {}, True, AuxLedger(10**6), ["v"])
    on = PlanContext("g", {}, True, AuxLedger(10**6), ["v"], capture=True)
    runner._plan_turn(off, choice, draft, "steps", 3)  # type: ignore[arg-type]
    runner._plan_turn(on, choice, draft, "steps", 3)  # type: ignore[arg-type]
    assert turn.asked == [False, True]
    assert off.snapshots == [] and on.snapshots == [snap("lead")]
    assert runner._traces == {} and runner.aux_traces("g", "node-app") == []


def test_only_a_plan_with_a_capturing_trial_captures() -> None:
    assert captures({"trial": context("t").wire()}) is True
    assert captures({"trial": context("t", capture=False).wire()}) is False
    assert captures({}) is False  # a real goal has no trial (D-100)
    assert captures({"trial": {"capture_trace": "yes"}}) is False
    assert PlanContext("g", {}, False, AuxLedger(0), []).capture is False  # the default
    assert PlanContext("g", {}, False, AuxLedger(0), []).snapshots == []


def test_the_read_only_turn_protocol_takes_capture_trace() -> None:
    # Protocol-typed callers pass capture_trace (§9.1); mypy checks the call in strategy_runner
    turn: ReadOnlyTurn = StrictTurn(trace=snap("p"))
    result = turn.run(prompt="p", schema={}, workspace=Path("."), capture_trace=True)
    assert result.trace == snap("p")


# ==================================================================================================
# a run resumed after operator steering stores its trace (continue_resumed)
# ==================================================================================================
def wait_running(rig: Any, deadline: float = 20.0) -> None:
    end = time.time() + deadline
    while time.time() < end:
        rows = rig.d.store.conn.execute(
            "SELECT state FROM heads WHERE kind='run' AND tenant=? AND project=?",
            rig.d.scope.keys(),
        ).fetchall()
        if any(r["state"] == "running" for r in rows):
            return
        time.sleep(0.05)
    raise AssertionError("the attempt never started")


def spy_aux_traces(coordinator: Any) -> dict[str, list[Any]]:
    """Records the ``aux_traces`` argument the loop passes to each executor call (a spy: the
    real ``execute`` and ``continue_resumed`` still run with every argument)."""
    seen: dict[str, list[Any]] = {"execute": [], "continue_resumed": []}
    for name in seen:
        real = getattr(coordinator, name)

        def call(*args: Any, _real: Any = real, _name: str = name, **kw: Any) -> Any:
            seen[_name].append(kw.get("aux_traces"))
            return _real(*args, **kw)

        setattr(coordinator, name, call)
    return seen


def steered(
    deployment: Any, tmp_path: Path, trial: TrialContext | None
) -> tuple[Any, dict[str, Any], dict[str, list[Any]]]:
    rig, loop, _container = rig_with_codex(deployment, tmp_path, "steer")
    d = rig.d
    trial_head(d, "trial-steer")
    loop.coordinator.trace_sink = trace_sink(d.store, d.scope, d.artifacts, rig.service.plan_record)
    seen = spy_aux_traces(loop.coordinator)
    operator = with_permissions(rig.operator, "goal.steer")
    goal = submit(rig)
    composition = rig.service.apps["app"].compositions["codex-cli"]
    rig.service.plan(goal, composition=composition, **({"trial": trial} if trial else {}))
    rig.service.approve(rig.operator, goal)
    result: dict[str, Any] = {}
    runner = threading.Thread(target=lambda: result.update(loop.run_goal(goal)))
    runner.start()
    wait_running(rig)
    assert loop.steer(operator, goal, "value() must return 2")["status"] == "steering"
    runner.join(60)
    return rig, result, seen


def execution_of(rig: Any, run_id: str) -> dict[str, Any]:
    d = rig.d
    rows = d.store.conn.execute(
        "SELECT id FROM heads WHERE tenant=? AND project=? AND kind='worker-execution'",
        d.scope.keys(),
    ).fetchall()
    heads = [d.store.head(d.scope, "worker-execution", r["id"]) for r in rows]
    (head,) = [h for h in heads if h["data"]["run_id"] == run_id]
    return dict(head["data"])


def test_a_steered_capturing_trial_stores_the_trace_of_every_turn(
    deployment: Any, tmp_path: Path
) -> None:
    rig, result, seen = steered(deployment, tmp_path, context("trial-steer"))
    assert result["status"] == "verified", result
    (attempt,) = result["attempts"]
    assert attempt["steered"] is True
    d = rig.d
    ((_ref, record),) = list(d.store.list_objects(d.scope, TRACE_KIND))
    assert record["run_id"] == attempt["run_id"] and record["split"] == "development"
    body = json.loads(d.artifacts.read(d.scope, record["artifact"]))
    assert [t["turn"] for t in body["turns"]] == [0, 1]  # the first turn and the resumed turn
    data = execution_of(rig, attempt["run_id"])
    first = [h for h in data["trace_handles"] if not h.startswith("resume-")]
    assert len(first) == 1 and data["trace_handles"][1].startswith("resume-")
    assert list(d.store.list_objects(d.scope, DROP_KIND)) == []
    # the loop itself passed the node's trace source to both turns of the steered attempt
    assert len(seen["execute"]) == 1 and len(seen["continue_resumed"]) == 1
    assert all(callable(source) for source in seen["execute"] + seen["continue_resumed"])
    assert seen["continue_resumed"][0]() == []  # repair_loop has no auxiliary turn


def test_a_steered_trial_without_capture_and_a_steered_real_goal_store_nothing(
    deployment: Any, tmp_path: Path
) -> None:
    rig, result, _seen = steered(deployment, tmp_path, context("trial-steer", capture=False))
    assert result["status"] == "verified", result
    d = rig.d
    (attempt,) = result["attempts"]
    assert "trace_handles" not in execution_of(rig, attempt["run_id"])  # nothing recorded
    assert list(d.store.list_objects(d.scope, TRACE_KIND)) == []
    assert list(d.store.list_objects(d.scope, DROP_KIND)) == []


def test_a_steered_real_goal_stores_no_trace(deployment: Any, tmp_path: Path) -> None:
    rig, result, _seen = steered(deployment, tmp_path, None)
    assert result["status"] == "published", result
    d = rig.d
    (attempt,) = result["attempts"]
    assert "trace_handles" not in execution_of(rig, attempt["run_id"])
    assert list(d.store.list_objects(d.scope, TRACE_KIND)) == []
    assert list(d.store.list_objects(d.scope, DROP_KIND)) == []
    assert len(rig.published) == 1  # it ran and published as before


# ==================================================================================================
# a read-only turn that echoes its own sandbox credential stores nothing (§9.2 secret scan gap)
# ==================================================================================================
# Not real credentials: shaped like values the sandbox holds and outside the scan_secrets forms
# (runtime/evidence/cas.py:17-23), so only the turn's own credential check can catch them.
OAUTH = "oat-test-3hN8qLzV0wPcK5rT2yXbD7mF9sJ4gU6e"
ACCESS = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0LW9ubHkifQ.c2lnbmF0dXJlLW5vdC1yZWFs"
REFRESH = "rt_test_Q2wE4rT6yU8iO0pA1sD3fG5hJ7kL9zX"
ROTATED = "rt_test_rotated_M1nB3vC5xZ7lK9jH2gF4dS6a"
SCHEMA_OK = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


def auth_json(refresh: str) -> bytes:
    return json.dumps(
        {"auth_mode": "chatgpt", "tokens": {"access_token": ACCESS, "refresh_token": refresh}}
    ).encode()


class RecordingSandbox:
    def __init__(self) -> None:
        self.homes: list[Path] = []

    def command(self, argv: list[str], workspace: Path, name: str, **kw: Any) -> list[str]:
        self.homes.append(kw["native_home"])
        return ["turn", name]


class LeasedCredential:
    """Writes the leased auth.json into the run home as ``ScopedCredential.seed`` does."""

    def __init__(self, data: bytes | None = auth_json(REFRESH)) -> None:
        self.data = data

    def seed(self, home: Path) -> None:
        (home / ".codex").mkdir(parents=True, exist_ok=True)
        if self.data is not None:
            (home / ".codex" / "auth.json").write_bytes(self.data)

    def release(self, home: Path) -> None:
        (home / ".codex" / "auth.json").unlink(missing_ok=True)


def scripted(monkeypatch: pytest.MonkeyPatch, stdout: bytes, on_run: Any = None) -> None:
    from amplai_foundry.runtime.execution import readonly_turn

    def run(command: list[str], **kw: Any) -> Any:
        if command[0] == "docker":
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if on_run is not None:
            on_run()
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(readonly_turn.subprocess, "run", run)


def jsonl(*events: dict[str, Any]) -> bytes:
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode()


def codex_out(*texts: str) -> bytes:
    messages = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": t}} for t in texts
    ]
    return jsonl(
        *messages,
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}},
        {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 1}},
    )


def claude_out(*texts: str) -> bytes:
    return jsonl(
        *[
            {"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}
            for t in texts
        ],
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
         "structured_output": {"ok": True}, "usage": {"input_tokens": 3, "output_tokens": 1}},
    )  # fmt: skip


def codex_ro(tmp_path: Path, credential: Any) -> Any:
    from amplai_foundry.runtime.execution.readonly_turn import CodexReadOnlyTurn

    return CodexReadOnlyTurn(
        RecordingSandbox(), credential, tmp_path / "runs", model="m", effort=None,  # type: ignore[arg-type]
        cell_id="codex-cli",
    )  # fmt: skip


def claude_ro(tmp_path: Path) -> Any:
    from amplai_foundry.runtime.execution.readonly_turn import ClaudeReadOnlyTurn

    return ClaudeReadOnlyTurn(
        RecordingSandbox(), OAUTH, tmp_path / "runs", model="m", effort=None,  # type: ignore[arg-type]
        cell_id="claude-cli",
    )  # fmt: skip


def admit_reviewer(d: Any, trial_id: str, snapshot: dict[str, Any]) -> Any:
    trial_head(d, trial_id)
    return TraceService(d.store, d.scope, d.artifacts).admit_turns(
        goal_id="goal-" + trial_id, trial=context(trial_id), driver_id="codex-cli",
        turn="reviewer", snapshots=[snapshot],
    )  # fmt: skip


def test_the_credential_forms_are_outside_the_secret_patterns() -> None:
    # the gap this check closes: scan_secrets does not know these forms (cas.py:17-23)
    from amplai_foundry.runtime.evidence.cas import scan_secrets

    for text in (OAUTH, auth_json(REFRESH).decode(), f"token is {ACCESS}"):
        assert scan_secrets(text.encode()) == []


def test_a_claude_reviewer_that_echoes_its_oauth_token_stores_nothing(
    deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, claude_out("the env holds CLAUDE_CODE_OAUTH_TOKEN=" + OAUTH))
    result = claude_ro(tmp_path).run(
        prompt="p", schema=SCHEMA_OK, workspace=tmp_path, capture_trace=True
    )
    assert result.output == {"ok": True}  # the turn itself is unchanged
    assert result.trace["errors"] == 1 and result.trace["items"] == []
    assert OAUTH not in json.dumps(result.trace)
    d = deployment
    assert admit_reviewer(d, "trial-oauth", result.trace) is None
    assert list(d.store.list_objects(d.scope, TRACE_KIND)) == []
    ((_ref, drop),) = list(d.store.list_objects(d.scope, DROP_KIND))
    assert drop["reason"] == "sanitizer_error" and OAUTH not in json.dumps(drop)


def test_a_clean_claude_reviewer_turn_is_stored(
    deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, claude_out("the diff looks right"))
    result = claude_ro(tmp_path).run(
        prompt="p", schema=SCHEMA_OK, workspace=tmp_path, capture_trace=True
    )
    assert result.trace["errors"] == 0
    assert [i["text"] for i in result.trace["items"]] == ["the diff looks right"]
    d = deployment
    ref = admit_reviewer(d, "trial-claude-clean", result.trace)
    assert ref is not None and list(d.store.list_objects(d.scope, DROP_KIND)) == []


@pytest.mark.parametrize(
    "echo",
    [
        auth_json(REFRESH).decode(),  # cat ~/.codex/auth.json: the JSON key form is not pattern 2
        "access " + ACCESS,
        "x" * 3000 + REFRESH + "y" * 3000,  # across the sanitizer's cut point (MESSAGE_MAX)
    ],
    ids=["auth-json", "access-jwt", "across-the-cut"],
)
def test_a_codex_reviewer_that_echoes_its_leased_credential_stores_nothing(
    deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, echo: str
) -> None:
    scripted(monkeypatch, codex_out(echo))
    result = codex_ro(tmp_path, LeasedCredential()).run(
        prompt="p", schema=SCHEMA_OK, workspace=tmp_path, capture_trace=True
    )
    assert result.output == {"ok": True}
    assert result.trace["errors"] == 1 and result.trace["items"] == []
    d = deployment
    assert admit_reviewer(d, "trial-codex", result.trace) is None
    assert list(d.store.list_objects(d.scope, TRACE_KIND)) == []
    ((_ref, drop),) = list(d.store.list_objects(d.scope, DROP_KIND))
    assert drop["reason"] == "sanitizer_error"


def test_a_codex_token_refreshed_during_the_turn_is_also_recognised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = codex_ro(tmp_path, LeasedCredential())

    def rotate() -> None:  # the CLI rewrites auth.json in its home when it refreshes
        (turn.sandbox.homes[-1] / ".codex" / "auth.json").write_bytes(auth_json(ROTATED))

    scripted(monkeypatch, codex_out("new token " + ROTATED), on_run=rotate)
    result = turn.run(prompt="p", schema=SCHEMA_OK, workspace=tmp_path, capture_trace=True)
    assert result.trace["errors"] == 1 and result.trace["items"] == []


def test_a_codex_credential_link_is_not_followed_and_the_seeded_literals_still_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_bytes(json.dumps({"x": "host-file-value-not-a-credential"}).encode())
    turn = codex_ro(tmp_path, LeasedCredential())

    def relink() -> None:  # the agent swaps the home's auth.json for a link
        target = turn.sandbox.homes[-1] / ".codex" / "auth.json"
        target.unlink()
        target.symlink_to(outside)

    scripted(monkeypatch, codex_out("host-file-value-not-a-credential"), on_run=relink)
    result = turn.run(prompt="p", schema=SCHEMA_OK, workspace=tmp_path, capture_trace=True)
    assert result.trace["errors"] == 0  # the link's target is not read as a credential
    scripted(monkeypatch, codex_out("still " + REFRESH), on_run=relink)
    result = turn.run(prompt="p", schema=SCHEMA_OK, workspace=tmp_path, capture_trace=True)
    assert result.trace["errors"] == 1  # the seeded copy was read before the run


def test_a_captured_codex_turn_without_a_readable_credential_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, codex_out("plain text"))
    result = codex_ro(tmp_path, LeasedCredential(data=None)).run(
        prompt="p", schema=SCHEMA_OK, workspace=tmp_path, capture_trace=True
    )
    assert result.trace["errors"] == 1 and result.trace["items"] == []


def test_a_clean_codex_turn_keeps_its_items_and_an_uncaptured_turn_has_no_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, codex_out("auth_mode chatgpt, nothing secret"))
    turn = codex_ro(tmp_path, LeasedCredential())
    captured = turn.run(prompt="p", schema=SCHEMA_OK, workspace=tmp_path, capture_trace=True)
    assert captured.trace["errors"] == 0 and len(captured.trace["items"]) == 2
    assert turn.run(prompt="p", schema=SCHEMA_OK, workspace=tmp_path).trace is None


def test_credential_literals_are_the_long_values_not_the_key_names() -> None:
    from amplai_foundry.meta_harness.traces import credential_literals

    assert credential_literals(auth_json(REFRESH)) == {ACCESS, REFRESH}  # "chatgpt" is short
    assert credential_literals(b"  " + OAUTH.encode() + b"\n") == {OAUTH}  # not JSON: the text
