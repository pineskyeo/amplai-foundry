"""Work 018 — what the Observatory sees from the local execution flow.

Found by the metrics review and the real runs (specs/018-v3-real-execution/runs/): stopped runs
kept a ``running`` RunRecord, approvals left no audit event, human wait and queue time were
hard-coded unknown, and a failure signature was only a hash. Same stand-ins as the e2e rig.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from amplai_foundry.evaluation.observatory import Observatory
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
