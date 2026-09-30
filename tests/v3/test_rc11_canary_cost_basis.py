"""Work 030 S5 (D-092) — a canary may declare that cost is not compared.

Same shape as D-088 for the analysis: a subscription driver reports no cost (Codex, D-085) or an
estimated one (Claude, D-084). Under ``cost_basis: not_compared`` the canary accepts an unknown
cost and an estimated usage, keeps the reserved cost instead of writing 0, and still needs known
tokens, a bound receipt and a success. The default (``compared``) is unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_dev03_meta_harness import meta03, ready, result, start  # noqa: F401  (fixture)

from amplai_foundry.runtime.contracts.identity import canonical, digest
from amplai_foundry.runtime.errors import Hold

# tokens are still bounded per trial (the shared root budget is 1000 tokens)
NOT_COMPARED = {"cost_basis": "not_compared", "max_trial_tokens": 500}


def subscription_result(m: Any, p: dict[str, Any], task: str, **over: Any) -> dict[str, Any]:
    """A canary result as a subscription driver gives it: tokens known, cost unknown, usage
    estimated, with a receipt that binds exactly what it claims."""
    proposal = m.d.store.get(m.d.scope, "harness-change-proposal", p["proposal_ref"])
    fields: dict[str, Any] = {
        "success": True,
        "safety_failures": 0,
        "unknown_effects": 0,
        "cost_microunits": None,
        "input_tokens": 300,
        "output_tokens": 100,
        "usage_status": "estimated",
        **over,
    }
    receipt = {
        **fields,
        "composition_ref": proposal["candidate_ref"],
        "task_id": task,
        "mode": "canary",
        "scope": m.d.scope.wire(),
        "risk_class": "low",
        "target_binding_ref": p["policy"]["task_binding_refs"][task],
    }
    artifact = m.d.artifacts.admit(
        m.d.scope, canonical(receipt), "application/json", trust="verifier"
    )
    return {**fields, "artifact_ref": artifact}


def head(m: Any, p: dict[str, Any]) -> dict[str, Any]:
    return dict(m.d.store.head(m.d.scope, "evolution", p["proposal_id"]))


def reason(m: Any, p: dict[str, Any]) -> str:
    """Why the canary stopped: the first incident, so a negative test cannot pass by accident."""
    return str(head(m, p)["data"]["canary_incidents"][0]["reason"])


def first_task(p: dict[str, Any]) -> str:
    return str(p["policy"]["eligible_task_ids"][0])


def test_the_default_still_refuses_an_unknown_cost(meta03: Any) -> None:  # noqa: F811
    m = meta03
    p = start(m, ready(m))
    task = first_task(p)
    outcome = m.meta.canary_trial(
        m.reviewer, p["proposal_id"], task, lambda t: subscription_result(m, p, t)
    )
    assert outcome["state"] == "aborted"
    assert head(m, p)["data"]["canary_incidents"][0]["reason"].startswith("uncertain_canary_")


def test_not_compared_accepts_an_unknown_cost_and_estimated_usage(meta03: Any) -> None:  # noqa: F811
    m = meta03
    p = start(m, ready(m, NOT_COMPARED))
    task = first_task(p)
    outcome = m.meta.canary_trial(
        m.reviewer, p["proposal_id"], task, lambda t: subscription_result(m, p, t)
    )
    assert outcome["state"] == "canary_running" and outcome["observed_runs"] == 1
    data = head(m, p)["data"]
    assert data["canary_incidents"] == []
    (trial,) = data["canary_trials"]
    assert trial["cost_microunits"] is None and trial["usage_status"] == "estimated"
    totals = m.meta.budgets.totals(m.d.scope, p["proposal_id"])
    # settled, not unknown; the cost stays the reserved amount, never 0
    assert totals["unknown"] == 0 and totals["reserved_or_spent_cost_microunits"] == 10


def test_not_compared_still_needs_known_tokens(meta03: Any) -> None:  # noqa: F811
    m = meta03
    p = start(m, ready(m, NOT_COMPARED))
    outcome = m.meta.canary_trial(
        m.reviewer,
        p["proposal_id"],
        first_task(p),
        lambda t: subscription_result(m, p, t, input_tokens=None),
    )
    assert outcome["state"] == "aborted"
    assert reason(m, p) == "uncertain_canary_RuntimeFault"  # refused as a result, not by a budget


@pytest.mark.parametrize("claimed", ["unknown", "guessed"])
def test_not_compared_still_needs_a_real_usage_class(meta03: Any, claimed: str) -> None:  # noqa: F811
    m = meta03
    p = start(m, ready(m, NOT_COMPARED))
    outcome = m.meta.canary_trial(
        m.reviewer,
        p["proposal_id"],
        first_task(p),
        lambda t: subscription_result(m, p, t, usage_status=claimed),
    )
    assert outcome["state"] == "aborted"
    assert reason(m, p) == "uncertain_canary_RuntimeFault"


def test_the_receipt_must_bind_the_usage_class_the_result_claims(meta03: Any) -> None:  # noqa: F811
    m = meta03
    p = start(m, ready(m, NOT_COMPARED))

    def lying(task: str) -> dict[str, Any]:
        honest = subscription_result(m, p, task)
        return {**honest, "usage_status": "measured"}  # the receipt says estimated

    outcome = m.meta.canary_trial(m.reviewer, p["proposal_id"], first_task(p), lying)
    assert outcome["state"] == "aborted"
    assert reason(m, p) == "uncertain_canary_Hold"  # OBSERVATION_BINDING


def test_not_compared_does_not_relax_success_or_safety(meta03: Any) -> None:  # noqa: F811
    m = meta03
    p = start(m, ready(m, NOT_COMPARED))
    outcome = m.meta.canary_trial(
        m.reviewer,
        p["proposal_id"],
        first_task(p),
        lambda t: subscription_result(m, p, t, success=False),
    )
    assert outcome["state"] == "aborted"
    assert head(m, p)["data"]["canary_incidents"][0]["reason"] == "canary_verifier_failure"


def test_a_measured_result_is_accepted_under_either_basis(meta03: Any) -> None:  # noqa: F811
    m = meta03
    p = start(m, ready(m))
    outcome = m.meta.canary_trial(
        m.reviewer, p["proposal_id"], first_task(p), lambda t: result(m, p, t)
    )
    assert outcome["state"] == "canary_running"


def test_a_canary_completes_and_is_promotable_without_a_cost(meta03: Any) -> None:  # noqa: F811
    m = meta03
    p = start(m, ready(m, NOT_COMPARED))
    for task in p["policy"]["eligible_task_ids"]:
        m.meta.canary_trial(
            m.reviewer, p["proposal_id"], task, lambda t: subscription_result(m, p, t)
        )
    assert m.meta.request_promotion(m.reviewer, p["proposal_id"]) == "promotion_pending"


def test_an_unknown_cost_basis_value_is_refused_when_the_canary_is_approved(
    meta03: Any,  # noqa: F811
) -> None:
    m = meta03
    p = ready(m, {"cost_basis": "free"})
    policy_ref = m.put("canary-policy", p["policy"])
    approval = m.approve(
        "canary.execute",
        digest(
            {
                "proposal_ref": p["proposal_ref"],
                "report_ref": p["report_ref"],
                "policy_ref": policy_ref,
            }
        ),
    )
    with pytest.raises(Hold) as refused:
        m.meta.approve_canary(m.reviewer, p["proposal_id"], policy_ref, approval)
    assert refused.value.code == "CANARY_POLICY"
