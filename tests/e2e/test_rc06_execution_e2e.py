"""Work 018 — in-process end-to-end: approved goal → Codex-shaped CLI turn → verify → finish.

Real: CliDriver + SeededCodexPort (CliPort) through WorkCoordinator, the git workspace and
host-computed change, the patch verifier, operator approval, the execution loop and its
budgets. Stand-in: the "container" runs a script on the host that edits the workspace and
prints a Codex JSONL stream. This proves the wiring, not Codex or container conformance
(D-065); the real Codex runs are recorded under specs/018-v3-real-execution/runs/.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution.codex import AUTH
from rc06_rig import approved, checkout, rig_with_codex, submit


def test_approved_goal_runs_verifies_and_publishes(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right")
    before = checkout(rig.repo)
    goal = approved(rig)
    record = loop.run_goal(goal)
    assert record["status"] == "published" and rig.published == [goal]
    assert [a["outcome"] for a in record["attempts"]] == ["pass"]
    d = rig.d
    assert d.store.head(d.scope, "goal", goal)["state"] == "verified"
    change = json.loads(d.artifacts.read(d.scope, record["attempts"][0]["change"]))
    patch = d.artifacts.read(d.scope, change["patch"])
    assert b"+    return 2" in patch
    # the operator's checkout, branches and main never moved; the token refresh came back
    assert checkout(rig.repo) == before
    assert (rig.home / AUTH).read_text() == '{"tokens": "refreshed"}'
    # every run workspace was discarded
    assert list(rig.workspaces.root.glob("run-*")) == []


def test_a_failed_acceptance_is_repaired_with_feedback(deployment: Any, tmp_path: Path) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "wrong-first")
    record = loop.run_goal(approved(rig))
    assert [a["outcome"] for a in record["attempts"]] == ["fail", "pass"]
    assert record["status"] == "published"
    first, second = container.prompts
    assert "did not pass" not in first
    assert "did not pass" in second and "AC-1" in second


def test_attempts_stop_at_the_design_budget(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "always-wrong")
    record = loop.run_goal(approved(rig))
    # first attempt included (runtime-defaults max_verifier_repair_attempts_including_initial)
    assert [a["outcome"] for a in record["attempts"]] == ["fail", "fail", "fail"]
    assert record["status"] == "failed" and rig.published == []


def test_a_driver_failure_stops_without_retry_or_publish(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "crash")
    before = checkout(rig.repo)
    goal = approved(rig)
    record = loop.run_goal(goal)
    assert record["status"] == "held" and len(record["attempts"]) == 1
    assert record["attempts"][0]["outcome"] == "driver_failed" and rig.published == []
    # the approval is revoked and the goal ended failed: nothing can claim it again
    assert rig.d.store.head(rig.d.scope, "goal", goal)["state"] == "failed"
    with contextlib.suppress(Hold):  # either no candidate or an explicit hold
        assert rig.d.runtime.claim(rig.d.worker, goal_id=goal) is None
    assert checkout(rig.repo) == before


def test_cancel_and_timeout_stop_before_any_attempt(deployment: Any, tmp_path: Path) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "right")
    goal = approved(rig)
    loop.cancel(rig.operator, goal)
    assert loop.run_goal(goal)["status"] == "cancelled" and container.prompts == []
    goal2 = submit(rig, "second goal for the timeout")
    rig.service.plan(goal2)
    rig.service.approve(rig.operator, goal2)
    loop.clock = lambda: 10.0**10  # far past approved_at + max_wall_seconds
    assert loop.run_goal(goal2)["status"] == "timed_out" and container.prompts == []


def test_stopped_goals_end_in_runtime_state_and_count_in_the_observatory(
    deployment: Any, tmp_path: Path
) -> None:
    # found by the metrics review: the loop only updated its plan record, so a failed goal
    # stayed "active" and the Observatory's verified rate excluded it (inflated)
    from amplai_foundry.evaluation.observatory import Observatory

    rig, loop, _ = rig_with_codex(deployment, tmp_path, "always-wrong")
    failed = approved(rig)
    loop.run_goal(failed)
    cancelled = submit(rig, "second goal")
    rig.service.plan(cancelled)
    rig.service.approve(rig.operator, cancelled)
    loop.cancel(rig.operator, cancelled)
    loop.run_goal(cancelled)
    d = rig.d
    assert d.store.head(d.scope, "goal", failed)["state"] == "failed"
    assert d.store.head(d.scope, "goal", cancelled)["state"] == "cancelled"
    graph = d.store.get(d.scope, "workgraph", rig.service.plan_record(failed)["graph_ref"])
    assert d.store.head(d.scope, "work", graph["nodes"][0]["work_id"])["state"] == "failed"
    summary = Observatory(d.store).summary(d.scope)
    assert summary["eligible_terminated_goals"] == 1 and summary["verified_goal_rate"] == 0.0


def test_the_background_loop_picks_up_approved_goals(deployment: Any, tmp_path: Path) -> None:
    import time

    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right")
    loop.idle = 0.05
    goal = approved(rig)
    loop.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline and rig.service.plan_record(goal)["status"] != "published":
            time.sleep(0.1)
    finally:
        loop.stop()
    assert rig.service.plan_record(goal)["status"] == "published"
