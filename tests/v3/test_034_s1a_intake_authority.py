"""Work 034 S1a — the intake actor and its limits (spec H-2..H-5, D-106, D-112; AC-H2, AC-H4).

Real: the reference deployment (store, authority, steering, goal service) and the rc06 local
product rig (approval, revoke, the execution loop's cancel). The intake actor is built the way the
intake route will build it (``intake_actor``); no HTTP field supplies it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from amplai_foundry.control_plane.api_v3.server import ApiServices, BearerAuthenticator, create_app
from amplai_foundry.runtime.contracts.authority import INTAKE_KIND, Actor
from amplai_foundry.runtime.contracts.identity import new_id, now
from amplai_foundry.runtime.contracts.intake import (
    HERMES_CONTEXT,
    INTAKE_PERMISSIONS,
    INTAKE_QUESTION_KINDS,
    INTAKE_STEERING_KINDS,
    STOP_EVENT,
    intake_actor,
)
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution.loop import ExecutionLoop
from amplai_foundry.runtime.execution.steering import SteeringService
from amplai_foundry.runtime.local_deployment import OPERATOR_PERMISSIONS
from rc06_rig import build_rig

ALL_QUESTION_KINDS = (
    "business_intent", "authority", "ambiguous_target", "conflicting_constraint",
    "subjective_direction", "destructive_change",
)  # fmt: skip


def stop_events(d: Any, goal: str) -> list[dict[str, Any]]:
    rows = d.store.conn.execute(
        "SELECT data FROM events WHERE aggregate_type='goal' AND aggregate_id=? AND event_type=?",
        (goal, STOP_EVENT),
    ).fetchall()
    return [json.loads(r["data"]) for r in rows]


# -- the actor -----------------------------------------------------------------------------------


def test_the_intake_actor_is_not_human_and_holds_only_the_front_agent_permissions(
    deployment: Any,
) -> None:
    d = deployment
    hermes = intake_actor("demo-owner", d.scope)
    assert hermes.kind == INTAKE_KIND != "human"
    assert hermes.authn_context_ref == HERMES_CONTEXT == "intake:hermes"
    assert hermes.permissions == INTAKE_PERMISSIONS == {
        "goal.submit", "runtime.read", "goal.steer", "question.answer", "knowledge.propose",
        "meta.read", "goal.cancel",
    }  # fmt: skip
    assert not {"execution.approve", "grant.issue"} & hermes.permissions
    # the frozen wire kind is human|service: written as a service, marked by its context
    assert hermes.wire() == {
        "subject_id": "demo-owner", "kind": "service", "authn_context_ref": "intake:hermes",
    }  # fmt: skip
    submitted = d.goals.submit(hermes, text="alpha 결과 JSON 생성", channel="hermes", key="m-1")
    intent = d.store.get(d.scope, "intent-envelope", submitted["intent_ref"])
    assert intent["actor"] == hermes.wire() and intent["source_channel"] == "hermes"


def test_the_front_agent_cannot_issue_a_grant(deployment: Any) -> None:
    hermes = intake_actor("demo-owner", deployment.scope)
    with pytest.raises(RuntimeFault) as exc:
        deployment.authority.issue(hermes, {})
    assert exc.value.code == "FORBIDDEN"


def test_the_front_agent_cannot_approve_even_with_the_permission(
    deployment: Any, tmp_path: Path
) -> None:
    rig = build_rig(deployment, tmp_path)
    hermes = intake_actor("pinesky", deployment.scope)
    goal = deployment.goals.submit(hermes, text="make value return 2", key="m-approve")["goal_id"]
    rig.service.plan(goal)
    with pytest.raises(RuntimeFault) as exc:
        rig.service.approve(hermes, goal)
    assert exc.value.code == "FORBIDDEN"
    widened = replace(hermes, permissions=hermes.permissions | {"execution.approve"})
    with pytest.raises(RuntimeFault) as exc:
        rig.service.approve(widened, goal)
    assert exc.value.code == "APPROVER_KIND"


# -- steering kinds (H-3, D-112) -----------------------------------------------------------------


def receive(d: Any, actor: Actor, prepared: dict[str, Any], kind: str, **kw: Any) -> Any:
    return SteeringService(d.runtime).receive(
        actor, prepared["goal_id"], kind, "from the operator's chat",
        expected_contract_ref=prepared["contract_ref"], key=new_id("k"), **kw,
    )  # fmt: skip


@pytest.mark.parametrize("kind", sorted(INTAKE_STEERING_KINDS))
def test_the_front_agent_may_send_its_allowed_kinds(
    deployment: Any, prepared: dict[str, Any], kind: str
) -> None:
    hermes = intake_actor("demo-owner", deployment.scope)
    extra = {"priority": 5} if kind == "priority_change" else {}
    assert receive(deployment, hermes, prepared, kind, **extra)["status"] == "queued"


def test_the_front_agent_kinds_are_the_design_list() -> None:
    assert {
        "constraint_add", "priority_change", "new_evidence", "acceptance_change", "pause", "cancel",
    } == INTAKE_STEERING_KINDS  # fmt: skip


def pause_applied(d: Any, prepared: dict[str, Any], by: Actor) -> None:
    """Queue a pause as ``by`` and let the controller stop the run at a checkpoint."""
    x = d.runtime.claim(d.worker)
    d.runtime.start(d.worker, x, "session-exact")
    queued = receive(d, by, prepared, "pause")
    snapshot = d.artifacts.admit(d.scope, b'{"changes":[]}', "application/json")
    stopped = SteeringService(d.runtime).quiesce(
        d.actor,
        queued["steering_id"],
        lambda *_: {"process_stopped": True, "session_handle": "session-exact",
                    "workspace_diff_artifact": snapshot},
    )  # fmt: skip
    assert stopped["status"] == "applied"


def test_the_front_agent_resumes_only_a_goal_it_paused(
    deployment: Any, prepared: dict[str, Any]
) -> None:
    d = deployment
    hermes = intake_actor("demo-owner", d.scope)
    with pytest.raises(RuntimeFault) as exc:  # nothing paused
        receive(d, hermes, prepared, "resume")
    assert exc.value.code == "FORBIDDEN"
    pause_applied(d, prepared, by=hermes)
    assert receive(d, hermes, prepared, "resume")["status"] == "queued"


def test_the_front_agent_cannot_resume_what_the_operator_paused(
    deployment: Any, prepared: dict[str, Any]
) -> None:
    d = deployment
    pause_applied(d, prepared, by=d.actor)  # the operator (a human) paused it
    with pytest.raises(RuntimeFault) as exc:
        receive(d, intake_actor("demo-owner", d.scope), prepared, "resume")
    assert exc.value.code == "FORBIDDEN" and "operator resumes" in exc.value.message
    # the operator still resumes it, and a human keeps every kind
    assert receive(d, d.actor, prepared, "resume")["status"] == "queued"


def test_a_pause_or_cancel_through_the_front_agent_leaves_an_operator_record(
    deployment: Any, prepared: dict[str, Any]
) -> None:
    d = deployment
    hermes = intake_actor("demo-owner", d.scope)
    receive(d, hermes, prepared, "pause")
    receive(d, d.actor, prepared, "pause")  # the operator's own pause is not a front-agent stop
    receive(d, hermes, prepared, "constraint_add")
    (record,) = stop_events(d, prepared["goal_id"])
    assert record["kind"] == "pause" and record["reason"] == "from the operator's chat"
    assert record["actor"]["authn_context_ref"] == "intake:hermes"
    assert record["steering_ref"]["id"].startswith("steer-")


def test_the_front_agent_reaches_only_its_operator_goals(
    deployment: Any, prepared: dict[str, Any]
) -> None:
    other = intake_actor("someone-else", deployment.scope)
    with pytest.raises(RuntimeFault) as exc:
        receive(deployment, other, prepared, "pause")
    assert exc.value.code == "NOT_FOUND"


def test_the_steering_route_checks_the_kind_too(deployment: Any, prepared: dict[str, Any]) -> None:
    d = deployment
    token = "test-only-intake-token-aaaaaaaaaaaaaaaaaaaaaaa"
    hermes = intake_actor("demo-owner", d.scope)
    authenticate = BearerAuthenticator(
        {hashlib.sha256(token.encode()).hexdigest(): "hermes"}, lambda _b: hermes
    )
    app = create_app(ApiServices(d.runtime, d.goals, authenticate))
    goal = prepared["goal_id"]
    with TestClient(app) as c:
        version = d.store.head(d.scope, "goal", goal)["row_version"]
        response = c.post(
            f"/api/v3/goals/{goal}/steering",
            headers={"Authorization": "Bearer " + token, "Idempotency-Key": "s1",
                     "If-Match": f'"{version}"'},
            json={"kind": "resume", "text": "go on",
                  "expected_contract_ref": prepared["contract_ref"]},
        )  # fmt: skip
    assert response.status_code == 403 and response.json()["code"] == "FORBIDDEN"


# -- question kinds (H-4, AC-H4) -----------------------------------------------------------------


def question(d: Any, goal: str, kind: str) -> dict[str, Any]:
    q = {
        "schema_version": "3.0.0", "question_id": new_id("q"), "scope": d.scope.wire(),
        "goal_id": goal, "kind": kind, "question": "Which one?", "options": [],
        "evidence_checked": [], "blocked_capabilities": [], "assigned_actor_id": "demo-owner",
        "status": "open", "answer": None, "asked_at": now(),
    }  # fmt: skip
    d.contracts.validate("question", q)
    with d.store.tx() as db:
        ref: dict[str, Any] = d.store.put(db, d.scope, "question", q["question_id"], 1, q)
    return ref


@pytest.mark.parametrize("kind", ALL_QUESTION_KINDS)
def test_the_front_agent_refines_an_intent_only_for_non_authority_questions(
    deployment: Any, kind: str
) -> None:
    d = deployment
    hermes = intake_actor("demo-owner", d.scope)
    goal = d.goals.submit(d.actor, text="alpha 결과 JSON 생성", key="g-" + kind)["goal_id"]
    ref = question(d, goal, kind)
    if kind in INTAKE_QUESTION_KINDS:
        assert d.goals.answer(hermes, ref, "the alpha app")["status"] == "discovery_required"
    else:
        with pytest.raises(RuntimeFault) as exc:
            d.goals.answer(hermes, ref, "the alpha app")
        assert exc.value.code == "FORBIDDEN"
        # the operator answers it directly
        assert d.goals.answer(d.actor, ref, "the alpha app")["status"] == "discovery_required"


@pytest.mark.parametrize("kind", ALL_QUESTION_KINDS)
def test_the_front_agent_answers_only_non_authority_questions(
    deployment: Any, prepared: dict[str, Any], kind: str
) -> None:
    d = deployment
    hermes = intake_actor("demo-owner", d.scope)
    ref = question(d, prepared["goal_id"], kind)
    if kind in INTAKE_QUESTION_KINDS:
        assert d.goals.answer(hermes, ref, "yes", prepared["contract_ref"])["revision"] == 2
    else:
        with pytest.raises(RuntimeFault) as exc:
            d.goals.answer(hermes, ref, "yes", prepared["contract_ref"])
        assert exc.value.code == "FORBIDDEN"


# -- goal.cancel (D-112) -------------------------------------------------------------------------


def test_the_operator_holds_the_cancel_permission() -> None:
    assert "goal.cancel" in OPERATOR_PERMISSIONS


def cancel_rig(deployment: Any, tmp_path: Path) -> tuple[Any, ExecutionLoop, Actor, str]:
    rig = build_rig(deployment, tmp_path)
    loop = ExecutionLoop(rig.service, coordinator=None)
    hermes = intake_actor("pinesky", deployment.scope)
    goal = deployment.goals.submit(hermes, text="make value return 2", key="m-cancel")["goal_id"]
    rig.service.plan(goal)
    return rig, loop, hermes, goal


def test_the_front_agent_cancels_its_operator_goal_and_says_why(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, hermes, goal = cancel_rig(deployment, tmp_path)
    with pytest.raises(Hold) as held:
        loop.cancel(hermes, goal)
    assert held.value.code == "CANCEL_REASON"
    assert loop.cancel(hermes, goal, reason="the operator said stop")["status"] == "cancelled"
    (record,) = stop_events(rig.d, goal)
    assert record == {
        "kind": "cancel", "actor": hermes.wire(), "reason": "the operator said stop",
        "steering_ref": None,
    }  # fmt: skip


def test_the_front_agent_cancel_revokes_an_approval(deployment: Any, tmp_path: Path) -> None:
    rig, loop, hermes, goal = cancel_rig(deployment, tmp_path)
    plan = rig.service.approve(rig.operator, goal)
    assert loop.cancel(hermes, goal, reason="stop it")["status"] == "cancelling"
    d = rig.d
    state = d.store.head(d.scope, "operator-approval-state", plan["decision_ref"]["id"])
    assert state["state"] == "revoked"
    assert state["data"]["by"]["authn_context_ref"] == "intake:hermes"


def test_cancel_needs_a_cancel_permission_and_an_own_goal(deployment: Any, tmp_path: Path) -> None:
    _rig, loop, hermes, goal = cancel_rig(deployment, tmp_path)
    with pytest.raises(RuntimeFault) as exc:
        loop.cancel(intake_actor("someone-else", deployment.scope), goal, reason="x")
    assert exc.value.code == "NOT_FOUND"
    reader = replace(hermes, permissions=frozenset({"runtime.read"}))
    with pytest.raises(RuntimeFault) as exc:
        loop.cancel(reader, goal, reason="x")
    assert exc.value.code == "FORBIDDEN"
    # a human with only goal.cancel stops it too (the operator's own path)
    human = Actor("pinesky", deployment.scope, frozenset({"goal.cancel"}), "human", "local-token")
    assert loop.cancel(human, goal)["status"] == "cancelled"
