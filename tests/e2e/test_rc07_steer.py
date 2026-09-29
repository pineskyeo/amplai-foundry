"""Work 019 S4 (D) — operator steering of a running agent (D-082).

The design's pause → exact-session resume pair on the product path: the running process is
stopped at a boundary and checkpointed, the same native session resumes with the operator's
message, and the result is verified as usual. Stand-ins as in the rc06 rig; the real steer is
recorded under specs/019-v3-completion/runs/.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.evaluation.observatory import Observatory
from amplai_foundry.runtime.errors import Hold
from rc06_rig import DRAFT, approved, rig_with_codex, with_permissions


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


def test_steering_pauses_and_resumes_the_same_session_with_the_message(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "steer")
    operator = with_permissions(rig.operator, "goal.steer")
    goal = approved(rig)
    result: dict[str, Any] = {}
    runner = threading.Thread(target=lambda: result.update(loop.run_goal(goal)))
    runner.start()
    wait_running(rig)
    queued = loop.steer(operator, goal, "value() must return 2")
    assert queued["status"] == "steering"
    runner.join(60)
    assert result["status"] == "published"
    (attempt,) = result["attempts"]
    assert attempt["outcome"] == "pass" and attempt["steered"] is True
    # the resumed turn got the operator's message on the same native session
    assert "Operator steering" in container.prompts[1]
    assert "value() must return 2" in container.prompts[1]
    record = rig.service.plan_record(goal)
    assert [s["text"] for s in record["steering"]] == ["value() must return 2"]
    d = rig.d
    boundary = d.store.conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='steering.process_boundary'"
    ).fetchone()[0]
    assert boundary == 1
    # the contract never changed; steering counts as human intervention
    assert (
        d.store.head(d.scope, "goal", goal)["data"]["active_contract_ref"] == record["contract_ref"]
    )
    events = Observatory(d.store).summary(d.scope)["human_intervention_events"]
    assert any(k.startswith("steering.") for k in events)
    # no credential stays in any session home: released at the pause and after the resumed turn,
    # and the resumed turn's token refresh came back to the scoped copy
    assert list((tmp_path / "journal").rglob("auth.json")) == []
    assert (rig.home / ".codex" / "auth.json").read_text() == '{"tokens": "refreshed"}'


def test_steering_outside_a_running_attempt_is_refused(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _container = rig_with_codex(deployment, tmp_path, "right")
    operator = with_permissions(rig.operator, "goal.steer")
    goal = approved(rig)  # approved, not running
    with pytest.raises(Hold) as held:
        loop.steer(operator, goal, "anything")
    assert held.value.code == "STEER_NOT_RUNNING"


def test_replanning_a_running_goal_freezes_the_next_revision_and_waits_for_approval(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "steer")
    operator = with_permissions(rig.operator, "goal.steer")
    goal = approved(rig)
    result: dict[str, Any] = {}
    runner = threading.Thread(target=lambda: result.update(loop.run_goal(goal)))
    runner.start()
    wait_running(rig)
    rig.planner.draft_value = {
        **DRAFT,
        "objective": "value() returns 2 (replanned)",
        "acceptance": [{"statement": "value() returns 2 after the replan", "verifier": "check"}],
    }
    assert loop.replan(operator, goal, "state the replanned objective")["status"] == "replanning"
    runner.join(60)
    assert result["status"] == "awaiting_approval" and result["revision"] == 2
    assert result["attempts"] == []
    assert [a["outcome"] for a in result["previous_attempts"]] == ["replanned"]
    d = rig.d
    contract = d.store.get(d.scope, "goal-contract", result["contract_ref"])
    assert contract["revision"] == 2 and contract["objective"] == "value() returns 2 (replanned)"
    graph = d.store.get(d.scope, "workgraph", result["graph_ref"])
    assert graph["previous_graph_ref"] == result["replan"]["previous_graph_ref"]
    assert graph["replan_reason"] == "state the replanned objective"
    assert d.store.head(d.scope, "goal", goal)["state"] == "blocked"
    # nothing runs before the operator approves the revision; then the revision runs
    container.mode = "right"
    rig.service.approve(rig.operator, goal)
    record = loop.run_goal(goal)
    assert record["status"] == "published"
    head = d.store.head(d.scope, "goal", goal)
    assert head["data"]["active_contract_ref"] == result["contract_ref"]


def test_replanning_is_for_running_goals(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _container = rig_with_codex(deployment, tmp_path, "right")
    operator = with_permissions(rig.operator, "goal.steer")
    goal = approved(rig)
    with pytest.raises(Hold) as held:
        loop.replan(operator, goal, "anything")
    assert held.value.code == "REPLAN_NOT_RUNNING"


# -- review fixes: no process, request or goal is left behind ------------------------------------


def steering_states(rig: Any) -> list[str]:
    rows = rig.d.store.conn.execute(
        "SELECT state FROM heads WHERE kind='steering' AND tenant=? AND project=?",
        rig.d.scope.keys(),
    ).fetchall()
    return sorted(r["state"] for r in rows)


def processes_stopped(container: Any) -> bool:
    return all(p.poll() is not None for p in container.driver.processes.values())


def test_a_steer_that_arrives_as_the_attempt_ends_is_withdrawn(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, _container = rig_with_codex(deployment, tmp_path, "right")
    operator = with_permissions(rig.operator, "goal.steer")
    goal = approved(rig)
    execute = loop.coordinator.execute

    def late(*args: Any, **kw: Any) -> Any:
        out = execute(*args, **kw)
        loop.steer(operator, goal, "too late")  # the agent's process already exited
        return out

    loop.coordinator.execute = late
    record = loop.run_goal(goal)
    assert record["status"] == "published"
    (entry,) = rig.service.plan_record(goal)["steering"]
    assert entry["text"] == "too late" and entry["applied"] is False
    # superseded, and the goal admits work again (nothing waits on the request)
    assert steering_states(rig) == ["superseded"]
    d = rig.d
    assert not d.store.head(d.scope, "goal", goal)["data"].get("admission_paused")


def test_a_pause_nobody_takes_over_stops_the_process(deployment: Any, tmp_path: Path) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "steer")
    operator = with_permissions(rig.operator, "goal.steer")
    goal = approved(rig)
    result: dict[str, Any] = {}
    runner = threading.Thread(target=lambda: result.update(loop.run_goal(goal)))
    started = time.time()
    runner.start()
    wait_running(rig)
    d = rig.d
    # a pause the loop did not request (another client of the runtime)
    loop.steering.receive(
        operator, goal, "pause", "from elsewhere", key="other-client",
        expected_contract_ref=d.store.head(d.scope, "goal", goal)["data"]["active_contract_ref"],
    )  # fmt: skip
    runner.join(40)
    assert not runner.is_alive() and time.time() - started < 40  # not the agent's 60 s turn
    assert result["status"] == "held"
    assert [a["outcome"] for a in result["attempts"]] == ["driver_failed"]
    assert processes_stopped(container)


def unconfirmed_boundary(loop: Any) -> None:
    """The stop is never confirmed (e.g. docker did not answer): quiesce leaves it queued."""
    stop = loop.coordinator.stop_and_snapshot

    def unconfirmed(worker: Any, run_id: str, kind: str) -> dict[str, Any]:
        stop(worker, run_id, kind)
        return {"process_stopped": False}

    loop.coordinator.stop_and_snapshot = unconfirmed


@pytest.mark.parametrize("kind", ["steer", "replan"])
def test_an_unconfirmed_boundary_holds_the_goal_and_stops_the_process(
    deployment: Any, tmp_path: Path, kind: str
) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "steer")
    operator = with_permissions(rig.operator, "goal.steer")
    goal = approved(rig)
    unconfirmed_boundary(loop)
    result: dict[str, Any] = {}
    runner = threading.Thread(target=lambda: result.update(loop.run_goal(goal)))
    runner.start()
    wait_running(rig)
    getattr(loop, kind)(operator, goal, "change course")
    runner.join(40)
    assert not runner.is_alive()
    # never resumed or replanned on an unconfirmed boundary: held, with the reason
    assert result["status"] == "held" and "revision" not in result
    (attempt,) = result["attempts"]
    assert attempt["outcome"] == "driver_failed" and attempt["reason"] == "STEER_BOUNDARY"
    assert len(container.prompts) == 1  # no resumed turn
    assert processes_stopped(container)
    # the request is withdrawn with the reason, not left queued
    (entry,) = rig.service.plan_record(goal)["steering"]
    assert entry["applied"] is False and entry["reason"] == "not applied: STEER_BOUNDARY"
    assert steering_states(rig) == ["superseded"]


def test_a_verification_fault_holds_the_goal(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _container = rig_with_codex(deployment, tmp_path, "right")
    goal = approved(rig)

    def broken(*args: Any, **kw: Any) -> Any:
        raise RuntimeError("verifier host fault")

    rig.service.verification.verify = broken
    record = loop.run_goal(goal)
    assert record["status"] == "held" and rig.published == []
    assert record["reason"] == "verification: RuntimeError"
    assert [a["outcome"] for a in record["attempts"]] == ["verify_failed"]


def test_an_approval_that_failed_after_activation_completes_on_retry(
    deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amplai_foundry.runtime.execution import product

    rig, loop, container = rig_with_codex(deployment, tmp_path, "steer")
    operator = with_permissions(rig.operator, "goal.steer")
    goal = approved(rig)
    result: dict[str, Any] = {}
    runner = threading.Thread(target=lambda: result.update(loop.run_goal(goal)))
    runner.start()
    wait_running(rig)
    loop.replan(operator, goal, "state the replanned objective")
    runner.join(60)
    assert result["status"] == "awaiting_approval"
    apply = product.SteeringService.apply_revision
    calls: list[str] = []

    def once(self: Any, actor: Any, steering_id: str) -> Any:
        calls.append(steering_id)
        if len(calls) == 1:
            raise RuntimeError("store unavailable")
        return apply(self, actor, steering_id)

    monkeypatch.setattr(product.SteeringService, "apply_revision", once)
    d = rig.d

    def grants() -> int:
        row = d.store.conn.execute("SELECT COUNT(*) FROM heads WHERE kind='execution-grant'")
        return int(row.fetchone()[0])

    with pytest.raises(RuntimeError):
        rig.service.approve(rig.operator, goal)
    issued = grants()
    # the revision is active but the approval did not finish: a retry completes it, no new grant
    head = d.store.head(d.scope, "goal", goal)
    assert head["data"]["active_contract_ref"] == result["contract_ref"]
    assert rig.service.plan_record(goal)["status"] == "awaiting_approval"
    record = rig.service.approve(rig.operator, goal)
    assert record["status"] == "approved" and grants() == issued and len(calls) == 2
    assert "applied" in steering_states(rig)
    container.mode = "right"
    assert loop.run_goal(goal)["status"] == "published"


# -- steering a steered (resumed) turn -----------------------------------------------------------


def wait_resumed(loop: Any, container: Any, goal: str, deadline: float = 30.0) -> None:
    """The steered turn has started and accepts steering again."""
    end = time.time() + deadline
    while time.time() < end:
        with loop._lock:
            ready = len(container.prompts) >= 2 and goal in loop._in_attempt
        if ready:
            return
        time.sleep(0.05)
    raise AssertionError("the resumed turn never started")


def test_a_steered_turn_can_be_steered_again(deployment: Any, tmp_path: Path) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "steer-twice")
    operator = with_permissions(rig.operator, "goal.steer")
    goal = approved(rig)
    result: dict[str, Any] = {}
    runner = threading.Thread(target=lambda: result.update(loop.run_goal(goal)))
    runner.start()
    wait_running(rig)
    loop.steer(operator, goal, "first guidance")
    wait_resumed(loop, container, goal)
    assert loop.steer(operator, goal, "SECOND guidance")["status"] == "steering"
    runner.join(60)
    # the reason and attempts in the message: this failed only on a Linux CI runner
    assert result["status"] == "published", {
        k: result.get(k) for k in ("status", "reason", "attempts", "steering")
    }
    (attempt,) = result["attempts"]
    assert attempt["outcome"] == "pass" and attempt["steered"] is True
    # three turns of one native session: original, first steer, second steer
    assert len(container.prompts) == 3
    assert "first guidance" in container.prompts[1] and "SECOND guidance" in container.prompts[2]
    steering = rig.service.plan_record(goal)["steering"]
    assert [(s["text"], s["applied"]) for s in steering] == [
        ("first guidance", True), ("SECOND guidance", True)
    ]  # fmt: skip
    d = rig.d
    boundaries = d.store.conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='steering.process_boundary'"
    ).fetchone()[0]
    assert boundaries == 2
    assert list((tmp_path / "journal").rglob("auth.json")) == []  # credential released each time


def test_a_steered_turn_can_be_replanned(deployment: Any, tmp_path: Path) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "steer-twice")
    operator = with_permissions(rig.operator, "goal.steer")
    goal = approved(rig)
    result: dict[str, Any] = {}
    runner = threading.Thread(target=lambda: result.update(loop.run_goal(goal)))
    runner.start()
    wait_running(rig)
    loop.steer(operator, goal, "first guidance")
    wait_resumed(loop, container, goal)
    rig.planner.draft_value = {**DRAFT, "objective": "value() returns 2 (replanned)"}
    assert loop.replan(operator, goal, "change the objective")["status"] == "replanning"
    runner.join(60)
    # the reason and attempts in the message: this failed once only on a Linux CI runner
    assert result["status"] == "awaiting_approval" and result["revision"] == 2, {
        k: result.get(k) for k in ("status", "reason", "attempts", "steering")
    }
    assert [a["outcome"] for a in result["previous_attempts"]] == ["replanned"]
    assert processes_stopped(container)
    assert rig.d.store.head(rig.d.scope, "goal", goal)["state"] == "blocked"
