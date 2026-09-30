"""Work 030 S5 (D-089, D-092) — the canary adapter feeds MetaHarness.canary_trial.

The adapter turns an offline trial observation into what a canary accepts: a boolean success, known
tokens and a receipt that binds the candidate, the task, the mode and the target. It is checked
against the real ``canary_trial`` (the meta reference deployment); the trial itself is a stub with
the executor's call shape, since the executor has its own tests (rc10).
"""

from __future__ import annotations

from typing import Any

import pytest
from test_dev03_meta_harness import meta03, ready  # noqa: F401  (fixture)

from amplai_foundry.evaluation.service import TrialObservation
from amplai_foundry.meta_harness.local_canary import canary_execute, canary_policy
from amplai_foundry.runtime.contracts.identity import canonical, digest
from amplai_foundry.runtime.errors import Hold, RuntimeFault


class StubTrials:
    """The executor's call shape; records what the adapter asked for."""

    def __init__(self, m: Any, **over: Any) -> None:
        self.m, self.over, self.calls = m, over, []

    def __call__(
        self, composition_ref: dict[str, Any], case: dict[str, Any], repeat: int, mode: str
    ) -> TrialObservation:
        self.calls.append((composition_ref, case["case_id"], repeat, mode))
        fields: dict[str, Any] = {
            "success": True,
            "input_tokens": 300,
            "output_tokens": 100,
            "cost_microunits": None,
            "usage_status": "estimated",
            **self.over,
        }
        receipt = {
            "task_id": case["case_id"],
            "repeat": repeat,
            "composition_ref": composition_ref,
            "mode": mode,
            "safety_failures": 0,
            "unknown_effects": 0,
            **fields,
        }
        artifact = self.m.d.artifacts.admit(
            self.m.d.scope, canonical(receipt), "application/json", trust="verifier"
        )
        return TrialObservation(
            fields["success"],
            (artifact,),
            cost_microunits=fields["cost_microunits"],
            input_tokens=fields["input_tokens"],
            output_tokens=fields["output_tokens"],
            usage_status=fields["usage_status"],
        )


def running(m: Any) -> dict[str, Any]:
    """A canary at canary_running on a not_compared policy, built the way a local run would."""
    p = ready(m, {"cost_basis": "not_compared", "max_trial_tokens": 500})
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
    m.meta.approve_canary(m.reviewer, p["proposal_id"], policy_ref, approval)
    m.meta.start_canary(m.reviewer, p["proposal_id"])
    return {**p, "policy_ref": policy_ref}


def adapter(m: Any, p: dict[str, Any], stub: StubTrials) -> Any:
    return canary_execute(
        stub,
        m.d.artifacts,
        m.d.scope,
        candidate_ref=p["candidate_ref"],
        task_binding_refs=p["policy"]["task_binding_refs"],
    )


def test_a_solved_task_is_accepted_by_the_real_canary(meta03: Any) -> None:  # noqa: F811
    m = meta03
    p = running(m)
    stub = StubTrials(m)
    task = p["policy"]["eligible_task_ids"][0]
    outcome = m.meta.canary_trial(m.reviewer, p["proposal_id"], task, adapter(m, p, stub))
    assert outcome["state"] == "canary_running" and outcome["observed_runs"] == 1
    # the trial ran on the candidate, in canary mode
    assert stub.calls == [(p["candidate_ref"], task, 0, "canary")]
    data = m.d.store.head(m.d.scope, "evolution", p["proposal_id"])["data"]
    assert data["canary_incidents"] == []
    assert data["canary_trials"][0]["usage_status"] == "estimated"


def test_a_failed_task_aborts_the_canary(meta03: Any) -> None:  # noqa: F811
    m = meta03
    p = running(m)
    task = p["policy"]["eligible_task_ids"][0]
    outcome = m.meta.canary_trial(
        m.reviewer, p["proposal_id"], task, adapter(m, p, StubTrials(m, success=False))
    )
    assert outcome["state"] == "aborted"
    data = m.d.store.head(m.d.scope, "evolution", p["proposal_id"])["data"]
    assert data["canary_incidents"][0]["reason"] == "canary_verifier_failure"


def test_a_trial_with_no_answer_is_not_counted_either_way(meta03: Any) -> None:  # noqa: F811
    m = meta03
    p = running(m)
    task = p["policy"]["eligible_task_ids"][0]
    outcome = m.meta.canary_trial(
        m.reviewer, p["proposal_id"], task, adapter(m, p, StubTrials(m, success=None))
    )
    assert outcome["state"] == "aborted"
    data = m.d.store.head(m.d.scope, "evolution", p["proposal_id"])["data"]
    assert data["canary_trials"] == []  # nothing was recorded as a pass or a failure
    assert data["canary_incidents"][0]["reason"] == "uncertain_canary_Hold"


def test_the_adapter_refuses_a_task_without_a_target_and_unknown_tokens(
    meta03: Any,  # noqa: F811
) -> None:
    m = meta03
    p = running(m)
    task = p["policy"]["eligible_task_ids"][0]
    with pytest.raises(Hold) as no_target:
        canary_execute(
            StubTrials(m),
            m.d.artifacts,
            m.d.scope,
            candidate_ref=p["candidate_ref"],
            task_binding_refs={},
        )(task)
    assert no_target.value.code == "CANARY_TASK_TARGET"
    with pytest.raises(RuntimeFault) as tokens:
        adapter(m, p, StubTrials(m, input_tokens=None))(task)
    assert tokens.value.code == "CANARY_RESULT"


def test_a_canary_of_all_tasks_reaches_promotion_pending(meta03: Any) -> None:  # noqa: F811
    m = meta03
    p = running(m)
    stub = StubTrials(m)
    for task in p["policy"]["eligible_task_ids"]:
        m.meta.canary_trial(m.reviewer, p["proposal_id"], task, adapter(m, p, stub))
    assert m.meta.request_promotion(m.reviewer, p["proposal_id"]) == "promotion_pending"
    assert len(stub.calls) == len(p["policy"]["eligible_task_ids"])


def test_the_policy_builder_declares_a_bounded_low_risk_opt_in_without_cost() -> None:
    binding = {"id": "binding", "revision": 1, "digest": "sha256:" + "0" * 64}
    release = {"id": "release", "revision": 1, "digest": "sha256:" + "1" * 64}
    policy = canary_policy(
        task_ids=["t1", "t2", "t3"],
        binding_ref=binding,
        fallback_release_ref=release,
        policy_id="canary-1",
        max_trial_tokens=200000,
    )
    assert policy["max_runs"] == 3 and policy["max_concurrent"] == 1
    assert policy["eligible_risk_classes"] == ["low"] and policy["project_opt_in"] is True
    assert policy["cost_basis"] == "not_compared"
    assert policy["max_cost_microunits"] == 0 and policy["max_trial_cost_microunits"] == 0
    assert policy["task_binding_refs"] == {"t1": binding, "t2": binding, "t3": binding}
    assert policy["fallback_release_ref"] == release
