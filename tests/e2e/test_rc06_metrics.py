"""Work 018 — what the Observatory sees from the local execution flow.

Found by the metrics review and the real runs (specs/018-v3-real-execution/runs/): stopped runs
kept a ``running`` RunRecord, approvals left no audit event, human wait and queue time were
hard-coded unknown, and a failure signature was only a hash. Same stand-ins as the e2e rig.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from amplai_foundry.evaluation.observatory import Observatory, _instant
from rc06_rig import DRAFT, approved, rig_with_codex, submit


def summary(rig: Any) -> dict[str, Any]:
    value: dict[str, Any] = Observatory(rig.d.store).summary(rig.d.scope)
    return value


def test_a_stopped_run_record_takes_the_same_outcome_as_its_head(
    deployment: Any, tmp_path: Path
) -> None:
    # the real store had a run head 'cancelled' whose RunRecord still said 'running'
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    d = rig.d
    goal = approved(rig)
    dispatch = d.runtime.claim(d.worker, goal_id=goal)
    assert dispatch is not None
    d.runtime.end_goal(rig.actors.service, goal, outcome="cancelled", reason="operator stop")
    run = d.store.head(d.scope, "run", dispatch["run_id"])
    assert run["state"] == run["data"]["record"]["status"] == "cancelled"
    assert run["data"]["record"]["finished_at"]
    v = summary(rig)
    assert v["status_counts"] == {"cancelled": 1}
    assert v["ended_run_reasons"] == {"operator stop": 1}
    assert v["integrity_findings"] == []


def test_the_observatory_flags_a_run_whose_head_and_record_disagree(
    deployment: Any, tmp_path: Path
) -> None:
    # rows written before the fix stay in existing stores; report them, do not guess
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    d = rig.d
    dispatch = d.runtime.claim(d.worker, goal_id=approved(rig))
    run_id = dispatch["run_id"]
    with d.store.tx() as db:
        head = d.store.head(d.scope, "run", run_id, db=db)
        d.store.cas(db, d.scope, "run", run_id, head["row_version"], "cancelled", head["data"])
    findings = summary(rig)["integrity_findings"]
    assert {
        "run_id": run_id,
        "finding": "run_state_mismatch",
        "head": "cancelled",
        "record": head["data"]["record"]["status"],
    } in findings


def test_approval_wait_and_queue_time_are_measured(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right")
    loop.run_goal(approved(rig))
    v = summary(rig)
    assert v["human_intervention_events"] == {"approval.requested": 1, "approval.granted": 1}
    assert v["human_wait_samples"] == 1 and v["human_wait_ms"] >= 0
    assert v["queue_samples"] == 1 and v["queue_ms"] >= 0
    assert v["compute_ms"] is None  # wall time is not compute time


def test_questions_and_operator_cancels_count_as_human_intervention(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right")
    rig.planner.draft_value = {**DRAFT, "acceptance": [], "questions": ["Which module is slow?"]}
    asked = submit(rig, "make it faster")
    assert rig.service.plan(asked)["status"] == "needs_answers"
    rig.planner.draft_value = dict(DRAFT)
    cancelled = submit(rig, "second goal")
    rig.service.plan(cancelled)
    rig.service.approve(rig.operator, cancelled)
    loop.cancel(rig.operator, cancelled)
    loop.run_goal(cancelled)
    v = summary(rig)
    # a goal that never ran still counts when nothing narrows the summary
    assert v["human_intervention_events"] == {
        "question.asked": 1,
        "approval.requested": 1,
        "approval.granted": 1,
        "approval.revoked": 1,
    }
    assert v["queue_samples"] == 0 and v["queue_ms"] is None  # never started: unknown, not 0


def test_failure_signatures_map_to_readable_reasons(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "always-wrong")
    loop.run_goal(approved(rig))
    v = summary(rig)
    # the loop's own stop after the attempt budget is not a human action
    assert "approval.revoked" not in v["human_intervention_events"]
    (signature,) = v["failure_signatures"]
    assert v["failure_signatures"][signature] == 3
    assert v["failure_reasons"] == {signature: {"AC-1": "fail: check exited 1"}}


def _runs(rig: Any, goal: str) -> list[dict[str, Any]]:
    d = rig.d
    rows = d.store.conn.execute(
        "SELECT data FROM heads WHERE tenant=? AND project=? AND kind='run'", d.scope.keys()
    ).fetchall()
    records = [json.loads(r["data"])["record"] for r in rows]
    return sorted((r for r in records if r["root_goal_id"] == goal), key=lambda r: r["attempt"])


def _granted_at(rig: Any, goal: str) -> float:
    row = rig.d.store.conn.execute(
        "SELECT created_at FROM events WHERE event_type='approval.granted' AND aggregate_id=?",
        (goal,),
    ).fetchone()
    value = _instant(row["created_at"])
    assert value is not None
    return value


def _set_record(rig: Any, run_id: str, **updates: Any) -> None:
    # controlled fixture for rows the runtime does not produce; never a write path
    d = rig.d
    with d.store.tx() as db:
        head = d.store.head(d.scope, "run", run_id, db=db)
        data = {**head["data"], "record": {**head["data"]["record"], **updates}}
        d.store.cas(db, d.scope, "run", run_id, head["row_version"], head["state"], data)


def test_queue_time_is_measured_to_the_goals_first_claim_even_outside_the_window(
    deployment: Any, tmp_path: Path
) -> None:
    # found by review: with a window holding only attempt 2, queue_ms used attempt 2's claim
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "wrong-first")
    goal = approved(rig)
    loop.run_goal(goal)
    first, second = _runs(rig, goal)
    started = _instant(first["started_at"])
    assert started is not None
    expected = (started - _granted_at(rig, goal)) * 1000
    v = Observatory(rig.d.store).summary(rig.d.scope, since=second["started_at"])
    assert v["run_count"] == 1
    assert v["queue_samples"] == 1 and v["queue_ms"] == expected


def test_a_claim_before_the_approval_is_an_integrity_finding_not_a_sample(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right")
    goal = approved(rig)
    loop.run_goal(goal)
    (run,) = _runs(rig, goal)
    _set_record(rig, run["run_id"], started_at="2000-01-01T00:00:00Z")
    v = summary(rig)
    assert v["queue_samples"] == 0 and v["queue_ms"] is None
    assert {"goal_id": goal, "finding": "negative_queue_duration"} in v["integrity_findings"]


def test_a_repeated_human_revoke_is_one_intervention(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    goal = approved(rig)
    rig.service.revoke(rig.operator, goal)
    rig.service.revoke(rig.operator, goal)
    assert summary(rig)["human_intervention_events"]["approval.revoked"] == 1


def test_failure_reasons_keep_the_most_complete_evidence(deployment: Any, tmp_path: Path) -> None:
    # a first run whose verdicts cannot be resolved must not pin an empty explanation
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "always-wrong")
    goal = approved(rig)
    loop.run_goal(goal)
    # heads are read in id order: leave only the last id complete so an incomplete run comes first
    *incomplete, _complete = sorted(_runs(rig, goal), key=lambda r: r["run_id"])
    for run in incomplete:
        _set_record(rig, run["run_id"], verdict_refs=[])
    v = summary(rig)
    (signature,) = v["failure_reasons"]
    assert v["failure_reasons"][signature] == {"AC-1": "fail: check exited 1"}


def test_the_planners_task_class_reaches_the_observatory_slices(
    deployment: Any, tmp_path: Path
) -> None:
    # gap 4 (D-076): the workgraph node carries task_class; an unknown label is not guessed
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right")
    rig.planner.draft_value = {**DRAFT, "task_class": "bug_fix"}
    loop.run_goal(approved(rig))
    rig.planner.draft_value = {**DRAFT, "task_class": "made_up"}
    other = submit(rig, "second goal")
    rig.service.plan(other)
    rig.service.approve(rig.operator, other)
    loop.run_goal(other)
    v = summary(rig)
    assert v["slices"]["task_class"] == {"bug_fix": 1, "unreported": 1}
    only = Observatory(rig.d.store).summary(rig.d.scope, filters={"task_class": "bug_fix"})
    assert only["run_count"] == 1 and only["verified_goals"] == 1
