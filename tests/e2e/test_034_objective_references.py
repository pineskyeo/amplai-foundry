"""D-124 node references across revisions and failures (PR #85 failure review).

A node objective over the 4096 node bound is a reference with the digest of the full text. These
tests follow references through a replan (a new contract revision), a cascade escalation (the same
draft on a new revision), a reviewer turn that cannot resolve its reference (a failed review turn,
the attempt goes on), and a loop attempt that cannot (the goal is held with the code).

Stand-ins as in the Work 033 S9 strategy tests and the rc07 steering tests.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from amplai_foundry.runtime.execution import text_bounds
from e2e.test_033_s9_strategies import (
    APPROVE,
    AUX,
    CASCADE,
    LEAD,
    ORCH,
    make_cascade_world,
    make_world,
    orchestrated,
    reviewed,
)
from e2e.test_rc07_steer import wait_running
from rc06_rig import DRAFT, approved, rig_with_codex, with_permissions

MISMATCH = "OBJECTIVE_REFERENCE_MISMATCH"
OBJECTIVE = "Make value() return 2. " + "r" * 6900 + " CLOSING RULES: the CLI prints the value."
REPLANNED = "Make value() return 2 again. " + "q" * 6500 + " NEW CLOSING RULES."


def _get(rig: Any, kind: str, ref: Any) -> dict[str, Any]:
    value: dict[str, Any] = rig.d.store.get(rig.d.scope, kind, ref)
    return value


def _reference(node: dict[str, Any], source: str, text: str) -> None:
    assert f"source {source}, digest {text_bounds.text_digest(text)}" in node["objective"]


def test_a_replan_freezes_a_long_objective_and_its_references_name_the_new_contract(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "steer")
    rig.planner.draft_value = {**DRAFT, "objective": OBJECTIVE}
    operator = with_permissions(rig.operator, "goal.steer")
    goal = approved(rig)
    first = rig.service.plan_record(goal)
    (old_node,) = _get(rig, "workgraph", first["graph_ref"])["nodes"]
    _reference(old_node, "contract.objective", OBJECTIVE)
    result: dict[str, Any] = {}
    runner = threading.Thread(target=lambda: result.update(loop.run_goal(goal)))
    runner.start()
    wait_running(rig)
    rig.planner.draft_value = {**DRAFT, "objective": REPLANNED}
    assert loop.replan(operator, goal, "state the replanned objective")["status"] == "replanning"
    runner.join(60)
    assert result["status"] == "awaiting_approval" and result["revision"] == 2
    contract = _get(rig, "goal-contract", result["contract_ref"])
    graph = _get(rig, "workgraph", result["graph_ref"])
    assert contract["revision"] == 2 and contract["objective"] == REPLANNED
    assert graph["contract_ref"] == result["contract_ref"]
    (node,) = graph["nodes"]
    _reference(node, "contract.objective", REPLANNED)
    assert node["objective"] != old_node["objective"]  # the node definition follows the content
    plan = rig.service.plan_record(goal)
    assert text_bounds.full_objective(plan, node, lambda: contract["objective"]) == REPLANNED
    # the old contract's objective is not the source of the new node
    bare = {**plan, "work_items": []}
    held = _held(lambda: text_bounds.full_objective(bare, node, lambda: OBJECTIVE))
    assert held.code == MISMATCH
    # the revision runs on the full new objective
    container.mode = "right"
    rig.service.approve(rig.operator, goal)
    assert loop.run_goal(goal)["status"] == "published"
    assert "Objective: " + REPLANNED + "\n" in container.prompts[-1]


def test_a_replan_whose_draft_overruns_says_to_cancel_and_resubmit(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, _container = rig_with_codex(deployment, tmp_path, "steer")
    operator = with_permissions(rig.operator, "goal.steer")
    goal = approved(rig)
    result: dict[str, Any] = {}
    runner = threading.Thread(target=lambda: result.update(loop.run_goal(goal)))
    runner.start()
    wait_running(rig)
    rig.planner.draft_value = {**DRAFT, "objective": "o" * 8193}
    loop.replan(operator, goal, "state the replanned objective")
    runner.join(60)
    assert result["status"] == "replan_failed"
    assert result["reason"].startswith("PLANNING_OBJECTIVE_LENGTH: objective is 8193 characters")
    assert "cancel the goal and submit a shorter goal" in result["reason"]
    assert "Shorten the request" not in result["reason"]


def test_an_escalation_keeps_the_references_and_their_digests(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_cascade_world(deployment, tmp_path)
    w.rig.planner.draft_value["objective"] = OBJECTIVE
    goal = w.approved(w.comps.strategy("cascade", CASCADE))
    (before,) = w.nodes(goal)
    _reference(before, "contract.objective", OBJECTIVE)
    record = w.loop.run_goal(goal)
    assert record["status"] == "awaiting_approval" and record["revision"] == 2
    contract = _get(w.rig, "goal-contract", record["contract_ref"])
    graph = _get(w.rig, "workgraph", record["graph_ref"])
    assert contract["revision"] == 2 and contract["objective"] == OBJECTIVE
    (after,) = graph["nodes"]
    assert after["objective"] == before["objective"]  # same text, same digest
    # the escalation reason is an internal string, now kept whole
    reason = record["escalation"]["reason"]
    assert graph["replan_reason"] == "escalation to claude-cli: " + reason
    assert reason.endswith("escalating to claude-cli")
    w.service.approve(w.rig.operator, goal)
    final = w.loop.run_goal(goal)
    assert final["status"] == "published"
    assert "Objective: " + OBJECTIVE + "\n" in w.extra["claude"].prompts[0]


def test_a_reviewer_reference_that_does_not_resolve_is_a_failed_review_turn(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, reviewed)
    w.rig.planner.draft_value["objective"] = OBJECTIVE
    w.turns.outputs["review"] = APPROVE
    strategy = w.comps.strategy(
        "generator_reviewer", {"reviewer_role": "reviewer", "max_rounds": 2}, **AUX
    )
    goal = w.approved(strategy)
    # neither source matches the node's digest: the work item is changed, the contract unreadable
    plan = w.record(goal)
    plan["work_items"][0]["objective"] = OBJECTIVE + " (changed)"
    w.service._save_plan(goal, plan)
    w.runner._contract_objective = lambda plan: lambda: "not the objective"  # type: ignore[method-assign]
    copies: list[str] = []
    workspaces = w.service.workspaces
    materialize = workspaces.materialize

    def spy(scope: Any, run_id: str, snapshot: Any) -> Path:
        copies.append(run_id)
        return materialize(scope, run_id, snapshot)

    workspaces.materialize = spy
    record = w.loop.run_goal(goal)
    # the review turn failed and was recorded; the attempt went on to verification and passed
    assert record["status"] == "published" and [a["outcome"] for a in record["attempts"]] == [
        "pass"
    ]
    assert record["reviews"] and all(
        r["verdict"] is None and r["error"] == MISMATCH for r in record["reviews"]
    )
    assert w.turns.of("review") == []  # no reviewer turn ran on an unresolved objective
    assert not [c for c in copies if c.startswith("review")]  # and no review copy was made


def test_a_loop_attempt_whose_reference_does_not_resolve_holds_the_goal(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, orchestrated)
    w.rig.planner.draft_value["objective"] = OBJECTIVE
    w.turns.outputs["lead"] = LEAD
    goal = w.approved(w.comps.strategy("orchestrator", ORCH, **AUX))
    *_parts, integration = w.nodes(goal)
    assert "source plan.work_item" in integration["objective"]
    plan = w.record(goal)
    plan["work_items"][2]["objective"] += " (changed)"
    w.service._save_plan(goal, plan)
    record = w.loop.run_goal(goal)
    assert [a["outcome"] for a in record["attempts"]] == ["pass", "pass"]  # the parts ran
    assert record["status"] == "held"
    assert record["reason"].startswith(f"prompt: {MISMATCH}: ")
    assert len(w.agents.prompts) == 2  # the integration node never started


def _held(call: Any) -> Any:
    from amplai_foundry.runtime.errors import Hold

    try:
        call()
    except Hold as exc:
        return exc
    raise AssertionError("expected a Hold")
