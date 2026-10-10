"""Work 034 S1a review — a cancel is never lost to a planner or an approval that finishes later.

Failure/recovery review of #74-#76 (P1): a cancel while the contract was being drafted was answered
but the planning thread's save turned the goal back to ``awaiting_approval``, and after a restart an
approve ran it. The fix is in the shared code, so the operator's ``amplai cancel`` and the front
agent's cancel behave the same. Real: `amplai ops local-init`, LocalProductDeployment, the API, the
execution loop's cancel and reconcile. Stand-in: a planner that waits on an event.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from amplai_foundry.runtime import cli
from amplai_foundry.runtime.errors import RuntimeFault
from amplai_foundry.runtime.execution.codex import AUTH
from amplai_foundry.runtime.local_deployment import LocalProductDeployment
from rc06_rig import FixedPlanner, codex_inputs, make_repo


class GatedPlanner(FixedPlanner):
    """Drafts only after ``release`` is set; ``entered`` tells the test it is drafting."""

    def __init__(self, *, broken: bool = False) -> None:
        super().__init__()
        self.entered, self.release, self.broken = threading.Event(), threading.Event(), broken

    def draft(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.entered.set()
        assert self.release.wait(20)
        if self.broken:
            raise RuntimeError("planner unavailable")
        return super().draft(*args, **kwargs)


def init_home(tmp_path: Path) -> Path:
    repo = make_repo(tmp_path)
    inputs = codex_inputs(tmp_path)
    codex_home = tmp_path / "codex-home"
    (codex_home / ".codex").mkdir(parents=True)
    (codex_home / AUTH).write_text('{"tokens": "x"}')
    home = tmp_path / "amplai"
    result = CliRunner().invoke(
        cli.app,
        [
            "ops", "local-init", "--repo", str(repo), "--app", "app",
            "--codex-home", str(codex_home),
            "--container-profile", str(inputs.container_profile),
            "--qualification-report", str(inputs.qualification_report),
            "--egress-profile", str(inputs.egress_profile),
            "--egress-qualification", str(inputs.egress_qualification),
            "--verifier", "check=python3 -c 'import app' | app imports",
            "--home", str(home), "--operator", "pinesky",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    return home


def start(home: Path, planner: FixedPlanner) -> LocalProductDeployment:
    dep = LocalProductDeployment(home / "local.json", start_loop=False)
    for installed in dep.service.apps.values():
        installed.planners["codex-cli"] = planner
    return dep


def settle(dep: Any) -> None:
    deadline = time.time() + 20
    while dep._planning and time.time() < deadline:
        time.sleep(0.05)
    assert not dep._planning


def goal_events(dep: Any, goal: str) -> list[tuple[str, dict[str, Any]]]:
    rows = dep.store.conn.execute(
        "SELECT event_type, data FROM events WHERE aggregate_type='goal' AND aggregate_id=? "
        "ORDER BY seq",
        (goal,),
    ).fetchall()
    return [(r["event_type"], json.loads(r["data"])) for r in rows]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return init_home(tmp_path)


def submit(client: TestClient, key: str = "g1") -> str:
    response = client.post(
        "/api/v3/intents",
        headers={"Idempotency-Key": key},
        json={"text": "make value return 2", "target_hints": ["app"]},
    )
    goal: str = response.json()["goal_id"]
    return goal


def test_a_cancel_while_planning_stays_cancelled_and_cannot_be_approved(home: Path) -> None:
    planner = GatedPlanner()
    dep = start(home, planner)
    token = (home / "operator.token").read_text().strip()
    try:
        with TestClient(dep.app) as client:
            client.headers["Authorization"] = "Bearer " + token
            goal = submit(client)
            assert client.post(f"/api/v3/local/goals/{goal}/plan").status_code == 202
            assert planner.entered.wait(20)
            cancelled = client.post(f"/api/v3/local/goals/{goal}/cancel").json()
            assert cancelled["status"] == "cancelled"
            planner.release.set()  # the planner finishes after the cancel
            settle(dep)
        assert dep.service.plan_record(goal)["status"] == "cancelled"
        assert dep.store.head(dep.scope, "goal", goal)["state"] == "cancelled"
        assert "approval.requested" not in [t for t, _ in goal_events(dep, goal)]
        with pytest.raises(RuntimeFault) as exc:
            dep.service.approve(dep.operator(), goal)
        assert exc.value.code == "GOAL_CANCELLED"
        # a second plan request finds the cancel, not a goal to plan again
        assert dep.request_plan(goal)["status"] == "cancelled"
    finally:
        dep.close()
    # a restart keeps it: the cancel is the plan record, not process memory
    again = start(home, FixedPlanner())
    try:
        again.loop.reconcile()
        assert again.service.plan_record(goal)["status"] == "cancelled"
        with pytest.raises(RuntimeFault) as exc:
            again.service.approve(again.operator(), goal)
        assert exc.value.code == "GOAL_CANCELLED"
    finally:
        again.close()


def test_a_failed_plan_cancelled_is_not_planned_again(home: Path) -> None:
    planner = GatedPlanner(broken=True)
    planner.release.set()
    dep = start(home, planner)
    try:
        goal = dep.goals.submit(dep.operator(), text="make value return 2", key="g-failed")[
            "goal_id"
        ]
        dep.request_plan(goal)
        settle(dep)
        assert dep.service.plan_record(goal)["status"] == "plan_failed"
        # the failure is a goal event a feed reader sees
        assert ("planning.failed", {"code": "RuntimeError"}) in goal_events(dep, goal)
        assert dep.loop.cancel(dep.operator(), goal)["status"] == "cancelled"
        assert dep.request_plan(goal)["status"] == "cancelled"
        assert not dep._planning
    finally:
        dep.close()


def test_a_planning_record_left_by_a_crash_becomes_a_visible_failure(home: Path) -> None:
    dep = start(home, FixedPlanner())
    try:
        goal = dep.goals.submit(dep.operator(), text="make value return 2", key="g-crash")[
            "goal_id"
        ]
        dep.service._save_plan(goal, {"goal_id": goal, "status": "planning"})  # thread died
        assert {"goal_id": goal, "action": "planning_interrupted"} in dep.loop.reconcile()
        assert dep.service.plan_record(goal)["status"] == "plan_failed"
        assert ("planning.failed", {"code": "INTERRUPTED"}) in goal_events(dep, goal)
    finally:
        dep.close()


def revoked_decisions(dep: Any) -> list[str]:
    rows = dep.store.conn.execute(
        "SELECT state FROM heads WHERE kind='operator-approval-state'"
    ).fetchall()
    return [r["state"] for r in rows]


@pytest.mark.parametrize("cancel_first", [True, False])
def test_a_cancel_during_approval_wins_and_the_new_approval_is_revoked(
    home: Path, cancel_first: bool
) -> None:
    dep = start(home, FixedPlanner())
    try:
        goal = dep.goals.submit(dep.operator(), text="make value return 2", key="g-race")["goal_id"]
        dep.service.plan(goal)
        operator = dep.operator()
        activate = dep.service.runtime.activate

        def racing(*args: Any, **kwargs: Any) -> Any:
            if cancel_first:  # the goal ends before activation
                dep.loop.cancel(operator, goal)
                return activate(*args, **kwargs)
            out = activate(*args, **kwargs)  # activated, then cancelled before the record
            dep.loop.cancel(operator, goal)
            return out

        dep.service.runtime.activate = racing
        with pytest.raises(RuntimeFault) as exc:
            dep.service.approve(operator, goal)
        if not cancel_first:
            assert exc.value.code == "GOAL_STOPPED"
        assert dep.service.plan_record(goal)["status"] == "cancelled"
        assert revoked_decisions(dep) == ["revoked"]
    finally:
        dep.close()
