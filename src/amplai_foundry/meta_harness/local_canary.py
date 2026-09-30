"""Canary trials on the local product (Work 030 S5, D-089, D-092).

``MetaHarness.canary_trial`` calls ``execute(task_id)`` for each admitted canary task and accepts
only a result it can bind to a receipt. This module supplies that callable from the offline trial
executor: the task runs as a real goal on the candidate composition (the baseline stays what every
other goal uses, so a stopped canary changes nothing), and the result carries a canary receipt.

A trial with no answer (``success`` unknown) is not a result: the canary needs a boolean, so the
adapter refuses it and the canary aborts as uncertain rather than counting it either way.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..evaluation.service import TrialObservation
from ..runtime.contracts.identity import canonical
from ..runtime.errors import Hold, RuntimeFault
from ..runtime.evidence.cas import ArtifactStore
from ..runtime.storage.store import Scope

CANARY_MODE = "canary"
# what LocalTrialExecutor.__call__ is: (composition_ref, case, repeat, mode) -> observation
TrialCall = Callable[[dict[str, Any], dict[str, Any], int, str], TrialObservation]


def canary_policy(
    *,
    task_ids: list[str],
    binding_ref: dict[str, Any],
    fallback_release_ref: dict[str, Any],
    policy_id: str,
    max_trial_tokens: int,
    max_wall_seconds: int = 3600,
    cost_basis: str = "not_compared",
) -> dict[str, Any]:
    """A canary policy for low-risk corpus tasks on one app (every task binds that app).

    Cost is not compared by default (D-092): the cost ceilings are 0 because they are not used.
    The whole canary is sequential (one run at a time) and bounded by the number of tasks.
    """
    return {
        "policy_id": policy_id,
        "eligible_task_ids": list(task_ids),
        "max_runs": len(task_ids),
        "max_wall_seconds": max_wall_seconds,
        "max_cost_microunits": 0,
        "max_trial_cost_microunits": 0,
        "max_trial_tokens": max_trial_tokens,
        "max_concurrent": 1,
        "project_opt_in": True,
        "eligible_risk_classes": ["low"],
        "target_binding_refs": [binding_ref],
        "task_binding_refs": {task: binding_ref for task in task_ids},
        "abort_on_safety_failure": True,
        "abort_on_unknown_effect": True,
        "fallback_release_ref": fallback_release_ref,
        "cost_basis": cost_basis,
    }


def canary_execute(
    executor: TrialCall,
    artifacts: ArtifactStore,
    scope: Scope,
    *,
    candidate_ref: dict[str, Any],
    task_binding_refs: dict[str, dict[str, Any]],
) -> Callable[[str], dict[str, Any]]:
    """The ``execute`` callable for ``MetaHarness.canary_trial`` of one candidate."""

    def execute(task_id: str) -> dict[str, Any]:
        binding = task_binding_refs.get(task_id)
        if binding is None:
            raise Hold("CANARY_TASK_TARGET", "The task has no approved target binding")
        obs = executor(candidate_ref, {"case_id": task_id}, 0, CANARY_MODE)
        if obs.success is None:
            raise Hold("CANARY_UNKNOWN", "The canary trial gave no answer; it is not counted")
        if obs.input_tokens is None or obs.output_tokens is None:
            raise RuntimeFault("CANARY_RESULT", "The canary trial reported no token usage")
        fields = {
            "success": obs.success,
            "safety_failures": obs.safety_failures,
            "unknown_effects": obs.unknown_effects,
            "cost_microunits": obs.cost_microunits,
            "input_tokens": obs.input_tokens,
            "output_tokens": obs.output_tokens,
            "usage_status": obs.usage_status,
        }
        receipt = {
            **fields,
            "composition_ref": candidate_ref,
            "task_id": task_id,
            "mode": CANARY_MODE,
            "scope": scope.wire(),
            "risk_class": "low",
            "target_binding_ref": binding,
            "trial_receipt_ref": obs.artifact_refs[0],
        }
        artifact = artifacts.admit(scope, canonical(receipt), "application/json", trust="verifier")
        return {**fields, "artifact_ref": artifact}

    return execute
