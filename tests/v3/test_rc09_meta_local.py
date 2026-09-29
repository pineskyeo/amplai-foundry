"""Work 030 S3 (D-089) — the meta-harness wired into the local product.

MetaHarness and EvaluationService use the product's store, with a meta-proposer service identity,
the human operator as the reviewer, and operator approvals that are durable objects instead of an
in-memory demo table. The proposer can never review, approve, execute, promote or reject.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from test_rc06_local_product import product  # noqa: F401  (fixture)

from amplai_foundry.runtime.contracts.identity import canonical, digest, new_id
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution import meta_local, prompts
from amplai_foundry.runtime.storage.store import Scope


def draft(dep: Any, suffix: str = "cand") -> str:
    """A class-A proposal (a new IMPLEMENTER prompt bundle) submitted by the meta-proposer."""
    d, service, meta = dep, dep.service, dep.meta_local
    proposer = meta.proposer
    base_ref = service.apps[next(iter(service.apps))].compositions["codex-cli"]
    base = dep.store.get(dep.scope, "harness-composition", base_ref)
    bundle = service._put(
        prompts.KIND, f"implementer-{suffix}",
        prompts.bundle(f"implementer-{suffix}", ["Implement it in {app_id}."], "test"),
    )  # fmt: skip
    value = {**base, "composition_id": base["composition_id"] + "__" + suffix,
             "prompt_bundle_ref": bundle}  # fmt: skip
    candidate = meta.meta.compositions.register(proposer, value)
    observation = d.artifacts.admit(
        d.scope, b'{"issue": "the implementer prompt is long"}', "application/json",
        trust="verifier",
    )  # fmt: skip
    observation_ref = service._put(
        "harness-observation", new_id("observation"),
        {"observation_id": new_id("observation"), "artifact": observation},
    )  # fmt: skip
    change = d.artifacts.admit(
        d.scope,
        canonical({"changed_paths": ["prompts/implementer.txt"],
                   "baseline_ref": base_ref, "candidate_ref": candidate}),
        "application/json", trust="operator",
    )  # fmt: skip
    proposal = {
        "schema_version": "3.0.0",
        "proposal_id": new_id("harness-proposal"),
        "scope": d.scope.wire(),
        "baseline_ref": base_ref,
        "candidate_ref": candidate,
        "surface_class": "A",
        "hypothesis": "A shorter implementer prompt keeps the pass rate.",
        "observation_refs": [observation_ref],
        "change_artifact": change,
        "expected_benefit": "Less prompt text per turn",
        "risks": ["A shorter prompt may drop a useful instruction"],
        "protected_surface_findings": [],
        "experiment_plan_ref": service._put(
            "experiment-draft", new_id("exp-draft"),
            {"draft_id": new_id("exp-draft"), "comparison": "20 demo tasks, two arms"},
        ),
        "rollback_plan_ref": service._put(
            "rollback-plan", new_id("rollback"),
            {"rollback_id": new_id("rollback"), "target_release_ref": base_ref},
        ),
        "proposer": proposer.wire(),
        "status": "draft",
    }  # fmt: skip
    meta.meta.submit(proposer, proposal)
    return str(proposal["proposal_id"])


def test_the_meta_proposer_is_a_service_identity_that_only_proposes(product: Any) -> None:  # noqa: F811
    dep, _client, _token = product
    proposer = dep.meta_local.proposer
    assert proposer.kind == "service" and proposer.permissions == {"harness.propose"}
    operator = dep.meta_operator()
    assert operator.kind == "human" and "harness.review" in operator.permissions
    assert "harness.propose" not in operator.permissions
    # the operator's ordinary permissions are unchanged by the meta ones
    assert {"goal.submit", "execution.approve"} <= operator.permissions


def test_the_operator_screens_and_rejects_and_the_findings_are_kept(product: Any) -> None:  # noqa: F811
    dep, _client, _token = product
    meta, operator = dep.meta_local.meta, dep.meta_operator()
    proposal = draft(dep)
    assert meta.screen(operator, proposal)["state"] == "screened"
    assert meta.reject(operator, proposal, "the pass rate did not hold") == "rejected"
    head = dep.store.head(dep.scope, "evolution", proposal)
    assert head["state"] == "rejected"
    assert head["data"]["rejection"]["reason"] == "the pass rate did not hold"
    assert head["data"]["rejection"]["from_state"] == "screened"
    assert head["data"]["rejection"]["by"]["subject_id"] == operator.subject_id
    with pytest.raises(RuntimeFault):  # terminal: nothing moves it again
        meta.screen(operator, proposal)


def test_a_draft_can_be_rejected_and_a_reason_is_required(product: Any) -> None:  # noqa: F811
    dep, _client, _token = product
    meta, operator = dep.meta_local.meta, dep.meta_operator()
    proposal = draft(dep)
    with pytest.raises(RuntimeFault) as blank:
        meta.reject(operator, proposal, "  ")
    assert blank.value.code == "REJECT_REASON"
    assert dep.store.head(dep.scope, "evolution", proposal)["state"] == "draft"
    assert meta.reject(operator, proposal, "not worth an experiment") == "rejected"


def test_the_proposer_cannot_screen_approve_or_reject(product: Any) -> None:  # noqa: F811
    dep, _client, _token = product
    meta, proposer = dep.meta_local.meta, dep.meta_local.proposer
    proposal = draft(dep)
    with pytest.raises(RuntimeFault) as denied:
        meta.screen(proposer, proposal)
    assert denied.value.code == "FORBIDDEN"
    with pytest.raises(RuntimeFault):
        meta.reject(proposer, proposal, "self")
    # even holding a reviewer permission it is refused as its own proposal
    greedy = replace(proposer, permissions=proposer.permissions | {"harness.review"})
    with pytest.raises(Hold) as own:
        meta.reject(greedy, proposal, "self")
    assert own.value.code == "SELF_APPROVAL"
    assert dep.store.head(dep.scope, "evolution", proposal)["state"] == "draft"


def test_an_operator_approval_is_durable_and_bound_to_its_action_and_subject(
    product: Any,  # noqa: F811
) -> None:
    dep, _client, _token = product
    approvals, operator = dep.meta_local.approvals, dep.meta_operator()
    subject = digest({"experiment": "e1"})
    ref = approvals.issue(operator, "experiment.execute", subject)
    check = approvals.check
    assert check(dep.scope, ref, "experiment.execute", subject)["approved_by"]["kind"] == "human"
    with pytest.raises(Hold):  # another action
        check(dep.scope, ref, "release.promote", subject)
    with pytest.raises(Hold):  # another subject
        check(dep.scope, ref, "experiment.execute", digest({"experiment": "e2"}))
    # a restart reads it back from the store: nothing lives only in memory
    again = meta_local.LocalMetaApprovals(dep.store, dep.scope)
    assert again.check(dep.scope, ref, "experiment.execute", subject)["action"]
    approvals.revoke(operator, ref)
    with pytest.raises(Hold):
        again.check(dep.scope, ref, "experiment.execute", subject)


def test_only_a_human_operator_who_is_not_the_proposer_can_approve(product: Any) -> None:  # noqa: F811
    dep, _client, _token = product
    approvals, proposer = dep.meta_local.approvals, dep.meta_local.proposer
    subject = digest({"experiment": "e1"})
    with pytest.raises(Hold):  # the proposer, a service identity
        approvals.issue(proposer, "experiment.execute", subject)
    service = dep.actors.service
    with pytest.raises(Hold):  # any other service identity, whatever it may hold
        approvals.issue(service, "experiment.execute", subject)
    human_proposer = replace(dep.meta_operator(), permissions=frozenset({"harness.propose"}))
    with pytest.raises((Hold, RuntimeFault)):  # a human holding the proposer permission
        approvals.issue(human_proposer, "experiment.execute", subject)
    ordinary = dep.operator()  # the goal operator without the meta permissions
    with pytest.raises(RuntimeFault) as forbidden:
        approvals.issue(ordinary, "release.promote", subject)
    assert forbidden.value.code == "FORBIDDEN"
    with pytest.raises(Hold):  # an action the meta-harness never asks for
        approvals.issue(dep.meta_operator(), "goal.publish", subject)


def test_an_approval_is_not_accepted_from_another_scope(product: Any) -> None:  # noqa: F811
    dep, _client, _token = product
    approvals, operator = dep.meta_local.approvals, dep.meta_operator()
    subject = digest({"canary": "c1"})
    ref = approvals.issue(operator, "canary.execute", subject)
    other = Scope("other-tenant", "other-project")
    with pytest.raises(Hold):
        approvals.check(other, ref, "canary.execute", subject)


def test_the_meta_release_keys_trust_the_local_authority(product: Any) -> None:  # noqa: F811
    dep, _client, _token = product
    assert set(dep.meta_local.meta.keys) == {"local-authority"}
    assert dep.meta_local.evaluation.executor_policy is None  # S4 supplies the executor
