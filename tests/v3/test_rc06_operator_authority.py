"""Work 018 S6 — only a human operator's exact approval authorizes execution (EX-005, D-070)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.runtime.errors import Hold, RuntimeFault
from rc06_rig import build_rig, submit


def planned(deployment: Any, tmp_path: Path) -> tuple[Any, str]:
    rig = build_rig(deployment, tmp_path)
    goal = submit(rig)
    rig.service.plan(goal)
    return rig, goal


def test_human_approval_activates_exactly_the_planned_contract(
    deployment: Any, tmp_path: Path
) -> None:
    rig, goal = planned(deployment, tmp_path)
    plan = rig.service.approve(rig.operator, goal)
    d = rig.d
    decision = d.store.get(d.scope, "operator-approval", plan["decision_ref"])
    grant = d.store.get(d.scope, "execution-grant", plan["grant_ref"])
    assert decision["approved_by"]["subject_id"] == "pinesky"
    assert decision["approved_by"]["kind"] == "human"
    assert grant["contract_ref"] == plan["contract_ref"] == decision["contract_ref"]
    assert grant["graph_ref"] == plan["graph_ref"] == decision["graph_ref"]
    assert d.store.head(d.scope, "goal", goal)["state"] == "ready"


def test_only_a_human_with_the_permission_can_approve(deployment: Any, tmp_path: Path) -> None:
    rig, goal = planned(deployment, tmp_path)
    service_with_permission = replace(
        rig.actors.service, permissions=rig.actors.service.permissions | {"execution.approve"}
    )
    with pytest.raises(RuntimeFault) as exc:
        rig.service.approve(service_with_permission, goal)
    assert exc.value.code == "APPROVER_KIND"
    human_without = replace(rig.operator, permissions=frozenset({"runtime.read"}))
    with pytest.raises(RuntimeFault) as exc:
        rig.service.approve(human_without, goal)
    assert exc.value.code == "FORBIDDEN"


def test_nothing_to_approve_before_a_plan(deployment: Any, tmp_path: Path) -> None:
    rig = build_rig(deployment, tmp_path)
    goal = submit(rig)
    with pytest.raises(RuntimeFault):
        rig.service.approve(rig.operator, goal)


def test_an_approval_does_not_cover_another_graph(deployment: Any, tmp_path: Path) -> None:
    rig, goal = planned(deployment, tmp_path)
    plan = rig.service.approve(rig.operator, goal)
    d = rig.d
    grant = dict(d.store.get(d.scope, "execution-grant", plan["grant_ref"]))
    grant.pop("signature", None)
    grant.update(grant_id="grant-forged", graph_ref={**plan["graph_ref"], "revision": 99})
    with pytest.raises(RuntimeFault) as exc:
        d.authority.issue(rig.actors.service, {k: v for k, v in grant.items() if k != "issuer"})
    assert exc.value.code == "DECISION_BINDING"


def test_revoking_the_approval_stops_every_later_claim(deployment: Any, tmp_path: Path) -> None:
    rig, goal = planned(deployment, tmp_path)
    rig.service.approve(rig.operator, goal)
    # positive control: an approved goal is claimable
    other = submit(rig, "second goal")
    rig.service.plan(other)
    rig.service.approve(rig.operator, other)
    rig.service.revoke(rig.operator, other)
    assert rig.d.runtime.claim(rig.d.worker, goal_id=goal) is not None
    with pytest.raises(Hold) as exc:
        rig.d.runtime.claim(rig.d.worker, goal_id=other)
    details = exc.value.details
    assert isinstance(details, list)
    assert [c["code"] for c in details] == ["AUTHORITY_DENIED"]
