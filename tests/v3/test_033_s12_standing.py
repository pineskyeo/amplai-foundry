"""Work 033 S12: the standing approval and the nightly identity (IC-13, IC-17; interfaces.md §8.8).

IC-17 and IC-18 are provisional: the standing approval is human-issued (`issue_nightly`, action
`nightly.explore`, permission `nightly.approve`) and nothing derived from it works without it.
The service identity `amplai-meta-nightly` derives exact approvals through `issue_standing`, which
admits only an exploratory development experiment, a calibration or a regression-set drift check
inside the policy's cells and `max_budget`, and `check` re-validates the standing approval at every
trial guard. The identity can never select holdout, never approve a non-trial goal and never
obtain a confirmatory, canary or promotion approval.

Real: the product deployment (`test_rc06_local_product.product`), its store and
`LocalMetaApprovals`. Stand-ins: none needed; no driver, docker or network.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_rc06_local_product import product  # noqa: F401  (fixture)

from amplai_foundry.meta_harness.local_executor import LocalTrialExecutor
from amplai_foundry.meta_harness.nightly import NightlyConfig, nightly_policy
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.errors import RuntimeFault
from amplai_foundry.runtime.execution import meta_local
from amplai_foundry.runtime.execution.meta_local import (
    ACTION_PERMISSION,
    APPROVAL_KIND,
    META_OPERATOR_PERMISSIONS,
    NIGHTLY_EXCLUDED,
    NIGHTLY_ID,
    NIGHTLY_PERMISSIONS,
    STANDING_ALLOWED,
    nightly_actor,
    validate_nightly_policy,
)
from amplai_foundry.runtime.execution.product import PLAN_KIND

CELL = "codex-cli"
SHARES = {"drift": 0.1, "screening_design": 0.0, "search": 0.6, "confirmation": 0.3}
MAX_BUDGET = {
    "max_wall_seconds": 3600, "max_attempts": 3, "max_tokens": 100000,
    "max_cost_microunits": 0, "currency": "USD", "max_parallel_works": 2,
    "max_delegation_depth": 0,
}  # fmt: skip
PLAN_BUDGET = dict(MAX_BUDGET)
DAY = 86400


def iso(epoch: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def config(**over: Any) -> NightlyConfig:
    base: dict[str, Any] = {
        "budget_trials": 100, "shares": dict(SHARES), "cells": (CELL,), "drift_tasks": ("t1",),
        "pilot": True, "pilot_nights": 3, "keep_operator_share": 0.5, "max_parallel": 2,
        "stop_at": "07:00",
    }  # fmt: skip
    return NightlyConfig(**{**base, **over})


def make_policy(**over: Any) -> dict[str, Any]:
    now = time.time()
    args: dict[str, Any] = {
        "budget_trials": 100, "valid_from": iso(now - 60), "valid_until": iso(now + 3 * DAY),
        "max_budget": dict(MAX_BUDGET),
    }  # fmt: skip
    return nightly_policy(config(), **{**args, **over})


class Env:
    def __init__(self, dep: Any) -> None:
        self.dep = dep
        self.store, self.scope = dep.store, dep.scope
        self.approvals = dep.meta_local.approvals
        self.operator = dep.meta_operator()
        self.nightly = nightly_actor(dep.scope)

    def put(self, kind: str, object_id: str, value: dict[str, Any]) -> dict[str, Any]:
        with self.store.tx() as db:
            return self.store.put(
                db, self.scope, kind, object_id, 1, {"scope": self.scope.wire(), **value}
            )

    def standing(self, **over: Any) -> dict[str, Any]:
        return self.approvals.issue_nightly(self.operator, make_policy(**over))

    def experiment(
        self, *, purpose: str = "exploratory", split: str = "development", cell: str = CELL,
        budget: dict[str, Any] | None = None, tag: str = "a",
    ) -> dict[str, Any]:  # fmt: skip
        analysis = self.put("analysis-plan", f"an-{tag}", {"policy": {"purpose": purpose}})
        sampling = self.put("sampling-plan", f"sa-{tag}", {"split": split, "cell_id": cell})
        proposal = self.put("proposal-stub", f"pr-{tag}", {})
        plan: dict[str, Any] = {
            "scope": self.scope.wire(), "proposal_ref": proposal, "analysis_plan_ref": analysis,
            "sampling_plan_ref": sampling, "tag": tag,
        }  # fmt: skip
        if budget is not False:  # type: ignore[comparison-overlap]
            plan["budget"] = dict(PLAN_BUDGET if budget is None else budget)
        return plan

    def calibration(
        self, *, regression: bool, cells: list[str] | None = None, indexed: bool = True
    ) -> dict[str, Any]:
        """A calibration plan on a stored corpus; the regression one as `corpus_v2.freeze` writes
        it (eval-corpus `<corpus_id>-<version>` named by `taskindex-amplai-regression-v1`)."""
        frozen_id = "amplai-regression-v1-1.0.0" if regression else "own-corpus-v2-1.0.0"
        corpus = self.put("eval-corpus", frozen_id, {"corpus_id": frozen_id, "cases": []})
        if regression and indexed:
            self.put("corpus-task-index", "taskindex-amplai-regression-v1",
                     {"corpus_ref": corpus, "corpus_version": "1.0.0"})  # fmt: skip
        return {
            "schema": "amplai.calibration-plan.v1", "scope": self.scope.wire(),
            "cells": cells if cells is not None else [CELL], "corpus_ref": corpus,
            "budget": dict(PLAN_BUDGET),
        }  # fmt: skip

    def derive(self, standing: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        return self.approvals.issue_standing(self.nightly, standing, "experiment.execute", plan)


@pytest.fixture
def env(product: Any) -> Env:  # noqa: F811
    dep, _client, _token = product
    return Env(dep)


def hold_code(fn: Any, *args: Any, **kwargs: Any) -> tuple[str, str]:
    with pytest.raises(RuntimeFault) as caught:
        fn(*args, **kwargs)
    return caught.value.code, caught.value.outcome


# --- the identity and the permission tables ----------------------------------------------------


def test_the_nightly_identity_is_a_service_with_exactly_five_permissions(env: Env) -> None:
    actor = env.nightly
    assert (actor.subject_id, actor.kind, actor.scope) == (
        "amplai-meta-nightly",
        "service",
        env.scope,
    )
    # IC-30 (A): harness.screen (the mechanical screen of class A drafts) is the fifth
    assert actor.permissions == {"experiment.approve", "experiment.run", "corpus.read",
                                 "execution.approve", "harness.screen"}  # fmt: skip
    assert actor.permissions == NIGHTLY_PERMISSIONS
    assert "harness.screen" not in NIGHTLY_EXCLUDED
    for forbidden in ("corpus.holdout.evaluate", "canary.approve", "canary.run", "release.promote",
                      "release.rollback", "harness.review", "harness.propose",
                      "experiment.reconcile", "nightly.approve"):  # fmt: skip
        assert forbidden not in actor.permissions and forbidden in NIGHTLY_EXCLUDED


def test_only_the_human_operator_holds_nightly_approve(env: Env) -> None:
    assert "nightly.approve" in META_OPERATOR_PERMISSIONS
    assert "nightly.approve" in env.operator.permissions
    assert "nightly.approve" not in env.dep.operator().permissions
    assert "nightly.approve" not in env.nightly.permissions
    assert "nightly.approve" not in env.dep.meta_local.proposer.permissions
    assert ACTION_PERMISSION["nightly.explore"] == "nightly.approve"


def test_the_allowed_list_is_exploratory_development_calibration_and_regression_drift() -> None:
    assert [dict(a) for a in STANDING_ALLOWED] == [
        {"kind": "experiment", "split": "development", "purpose": "exploratory"},
        {"kind": "calibration"},
        {"kind": "drift", "corpus": "amplai-regression-v1"},
    ]


# --- issuing the standing approval (human only) ------------------------------------------------


def test_the_human_operator_issues_a_standing_approval_bound_to_the_policy_digest(env: Env) -> None:
    assert env.approvals.current_standing() is None
    policy = make_policy()
    ref = env.approvals.issue_nightly(env.operator, policy)
    record = env.store.get(env.scope, APPROVAL_KIND, ref)
    assert record["action"] == "nightly.explore" and record["revoked"] is False
    assert record["subject_digest"] == digest(policy) and record["policy"] == policy
    assert record["approved_by"]["kind"] == "human"
    assert env.approvals.standing(ref)["policy"] == policy
    assert env.approvals.current_standing() == ref


def test_the_nightly_identity_and_a_proposer_cannot_issue_a_standing_approval(env: Env) -> None:
    policy = make_policy()
    assert hold_code(env.approvals.issue_nightly, env.nightly, policy) == (
        "APPROVAL_HUMAN", "hold",
    )  # fmt: skip
    assert hold_code(env.approvals.issue_nightly, env.dep.meta_local.proposer, policy) == (
        "APPROVAL_HUMAN", "hold",
    )  # fmt: skip
    human_proposer = replace(
        env.operator, permissions=env.operator.permissions | {"harness.propose"}
    )
    assert hold_code(env.approvals.issue_nightly, human_proposer, policy)[0] == "SELF_APPROVAL"
    assert env.approvals.current_standing() is None


def test_a_human_without_nightly_approve_is_forbidden(env: Env) -> None:
    assert hold_code(env.approvals.issue_nightly, env.dep.operator(), make_policy()) == (
        "FORBIDDEN", "rejected",
    )  # fmt: skip
    assert env.approvals.current_standing() is None


def test_a_standing_approval_in_another_scope_is_refused(env: Env) -> None:
    other = Actor("op", replace(env.scope, project_id="other"), META_OPERATOR_PERMISSIONS)
    assert hold_code(env.approvals.issue_nightly, other, make_policy())[0] == "APPROVAL_HUMAN"


def test_the_ordinary_issue_never_issues_the_standing_action(env: Env) -> None:
    code, outcome = hold_code(
        env.approvals.issue, env.operator, "nightly.explore", digest(make_policy())
    )
    assert (code, outcome) == ("APPROVAL_ACTION", "hold")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("cells"),
        lambda p: p.update(extra=1),
        lambda p: p.update(cells=[]),
        lambda p: p.update(cells=[CELL, CELL]),
        lambda p: p.update(cells=[""]),
        lambda p: p.update(budget_trials=-1),
        lambda p: p.update(budget_trials=1.5),
        lambda p: p.update(shares={**SHARES, "search": 0.7}),
        lambda p: p.update(shares={"drift": 0.1, "search": 0.6, "confirmation": 0.3}),
        lambda p: p.update(shares={**SHARES, "screening_design": 0.7}),
        lambda p: p.update(
            allowed=[{"kind": "experiment", "split": "validation", "purpose": "confirmatory"}]
        ),
        lambda p: p.update(allowed=[*p["allowed"], {"kind": "canary"}]),
        lambda p: p.update(max_budget={**MAX_BUDGET, "currency": "usd"}),
        lambda p: p.update(max_budget={"max_tokens": 1}),
        lambda p: p.update(valid_until=p["valid_from"]),
        lambda p: p.update(valid_until="tomorrow"),
        lambda p: p.update(valid_until=iso(time.time() + 8 * DAY)),
    ],
)
def test_an_invalid_nightly_policy_is_a_rejected_fault_and_nothing_is_stored(
    env: Env, mutate: Any
) -> None:
    policy = make_policy()
    mutate(policy)
    with pytest.raises(RuntimeFault) as fault:
        env.approvals.issue_nightly(env.operator, policy)
    assert fault.value.code == "NIGHTLY_POLICY" and fault.value.outcome == "rejected"
    assert env.approvals.current_standing() is None


def test_seven_nights_are_allowed_and_eight_are_not() -> None:
    now = time.time()
    validate_nightly_policy(make_policy(valid_from=iso(now), valid_until=iso(now + 7 * DAY)))
    with pytest.raises(RuntimeFault):
        validate_nightly_policy(
            make_policy(valid_from=iso(now), valid_until=iso(now + 7 * DAY + 1))
        )


def test_a_policy_that_is_not_an_object_is_a_rejected_fault() -> None:
    with pytest.raises(RuntimeFault) as fault:
        validate_nightly_policy("policy")
    assert fault.value.code == "NIGHTLY_POLICY"


# --- the standing approval is current only inside its dates and while unrevoked ----------------


def test_a_revoked_standing_approval_is_no_longer_current(env: Env) -> None:
    ref = env.standing()
    env.approvals.revoke(env.operator, ref)
    assert hold_code(env.approvals.standing, ref) == ("STANDING_APPROVAL", "hold")
    assert env.approvals.current_standing() is None


def test_revoking_a_standing_approval_needs_its_own_permission_and_a_human(env: Env) -> None:
    ref = env.standing()
    assert hold_code(env.approvals.revoke, env.nightly, ref) == ("APPROVAL_HUMAN", "hold")
    assert hold_code(env.approvals.revoke, env.dep.operator(), ref) == ("FORBIDDEN", "rejected")
    assert env.approvals.current_standing() == ref


def test_an_expired_standing_approval_is_not_current(env: Env) -> None:
    now = time.time()
    ref = env.standing(valid_from=iso(now - 2 * DAY), valid_until=iso(now - DAY))
    assert hold_code(env.approvals.standing, ref) == ("STANDING_APPROVAL", "hold")
    assert env.approvals.current_standing() is None


def test_a_standing_approval_that_has_not_started_is_not_current(env: Env) -> None:
    now = time.time()
    ref = env.standing(valid_from=iso(now + DAY), valid_until=iso(now + 2 * DAY))
    assert hold_code(env.approvals.standing, ref)[0] == "STANDING_APPROVAL"


def test_the_store_clock_decides_when_a_standing_approval_expires(env: Env) -> None:
    ref = env.standing()
    assert env.approvals.standing(ref)
    env.store.clock = lambda: time.time() + 10 * DAY
    assert hold_code(env.approvals.standing, ref)[0] == "STANDING_APPROVAL"
    assert env.approvals.current_standing() is None


def test_the_newest_current_standing_approval_wins(env: Env) -> None:
    first = env.standing()
    second = env.standing(budget_trials=50)
    assert env.approvals.current_standing() in (first, second)
    env.approvals.revoke(env.operator, second)
    assert env.approvals.current_standing() == first


@pytest.mark.parametrize(
    "ref", [None, "x", {}, {"id": "nope", "revision": 1, "digest": "sha256:0"}]
)
def test_standing_refuses_a_missing_or_unknown_reference(env: Env, ref: Any) -> None:
    assert hold_code(env.approvals.standing, ref) == ("STANDING_APPROVAL", "hold")


def test_a_standing_record_not_issued_by_a_human_is_refused(env: Env) -> None:
    policy = make_policy()
    forged = env.put(APPROVAL_KIND, "meta-approval-forged", {
        "approval_id": "meta-approval-forged", "action": "nightly.explore",
        "subject_digest": digest(policy), "policy": policy, "approved_by": env.nightly.wire(),
        "approved_at": "2026-10-01T00:00:00Z", "revoked": False,
    })  # fmt: skip
    assert hold_code(env.approvals.standing, forged) == ("STANDING_APPROVAL", "hold")
    assert env.approvals.current_standing() is None


def test_a_standing_record_whose_policy_was_edited_is_refused(env: Env) -> None:
    policy = make_policy()
    edited = {**policy, "budget_trials": 10**6}
    forged = env.put(APPROVAL_KIND, "meta-approval-edited", {
        "approval_id": "meta-approval-edited", "action": "nightly.explore",
        "subject_digest": digest(policy), "policy": edited, "approved_by": env.operator.wire(),
        "approved_at": "2026-10-01T00:00:00Z", "revoked": False,
    })  # fmt: skip
    assert hold_code(env.approvals.standing, forged)[0] == "STANDING_APPROVAL"


def test_an_ordinary_operator_approval_is_not_a_standing_approval(env: Env) -> None:
    ref = env.approvals.issue(env.operator, "experiment.execute", "sha256:" + "1" * 64)
    assert hold_code(env.approvals.standing, ref)[0] == "STANDING_APPROVAL"


# --- derived approvals: what the standing policy admits ----------------------------------------


def test_an_exploratory_development_experiment_gets_a_derived_approval(env: Env) -> None:
    standing = env.standing()
    plan = env.experiment()
    ref = env.derive(standing, plan)
    record = env.store.get(env.scope, APPROVAL_KIND, ref)
    assert record["action"] == "experiment.execute"
    assert record["approved_by"] == env.nightly.wire()
    assert record["standing_ref"] == {k: standing[k] for k in ("id", "revision", "digest")}
    assert record["plan_kind"] == "experiment"
    assert record["subject_digest"] == digest(plan)
    assert env.approvals.check(env.scope, ref, "experiment.execute", digest(plan)) == record


def test_the_subject_digest_comes_from_the_plan_not_from_the_caller(env: Env) -> None:
    params = list(inspect.signature(env.approvals.issue_standing).parameters)
    assert params == ["nightly", "standing_ref", "action", "plan"]
    standing = env.standing()
    plan = env.experiment()
    ref = env.derive(standing, plan)
    record = env.store.get(env.scope, APPROVAL_KIND, ref)
    assert record["subject_digest"] == digest(plan)
    # a field of the plan changed afterwards: the approval no longer matches
    assert (
        hold_code(
            env.approvals.check, env.scope, ref, "experiment.execute", digest({**plan, "tag": "b"})
        )[0]
        == "META_APPROVAL"
    )


def test_the_approval_ref_of_a_plan_is_not_part_of_its_subject(env: Env) -> None:
    standing = env.standing()
    plan = env.experiment()
    ref = env.derive(standing, {**plan, "approval_ref": {"id": "x", "revision": 1, "digest": "d"}})
    record = env.store.get(env.scope, APPROVAL_KIND, ref)
    assert record["subject_digest"] == digest(plan)


@pytest.mark.parametrize(
    ("purpose", "split"),
    [
        ("confirmatory", "development"),
        ("confirmatory", "validation"),
        ("exploratory", "validation"),
        ("exploratory", "holdout"),
        ("confirmatory", "holdout"),
        ("canary", "development"),
        (None, "development"),
        ("exploratory", None),
    ],
)
def test_a_confirmatory_validation_or_holdout_plan_gives_standing_approval(
    env: Env, purpose: Any, split: Any
) -> None:
    standing = env.standing()
    code, outcome = hold_code(env.derive, standing, env.experiment(purpose=purpose, split=split))
    assert (code, outcome) == ("STANDING_APPROVAL", "hold")
    assert [v for r, v in env.store.list_objects(env.scope, APPROVAL_KIND)
            if v.get("action") == "experiment.execute"] == []  # fmt: skip


@pytest.mark.parametrize(
    "field",
    ["max_wall_seconds", "max_attempts", "max_tokens", "max_cost_microunits",
     "max_parallel_works", "max_delegation_depth"],
)  # fmt: skip
def test_an_over_budget_plan_gives_standing_approval_field_by_field(env: Env, field: str) -> None:
    standing = env.standing()
    over = {**PLAN_BUDGET, field: PLAN_BUDGET[field] + 1}
    assert hold_code(env.derive, standing, env.experiment(budget=over)) == (
        "STANDING_APPROVAL", "hold",
    )  # fmt: skip
    # at the ceiling exactly it is fine
    assert env.derive(standing, env.experiment(budget=dict(PLAN_BUDGET), tag="ok"))


def test_a_plan_budget_in_another_currency_or_of_another_type_is_refused(env: Env) -> None:
    standing = env.standing()
    assert (
        hold_code(env.derive, standing, env.experiment(budget={**PLAN_BUDGET, "currency": "EUR"}))[
            0
        ]
        == "STANDING_APPROVAL"
    )
    assert (
        hold_code(env.derive, standing, env.experiment(budget={**PLAN_BUDGET, "max_tokens": "1"}))[
            0
        ]
        == "STANDING_APPROVAL"
    )
    assert hold_code(env.derive, standing, env.experiment(budget=False))[0] == "STANDING_APPROVAL"


def test_a_plan_on_a_cell_outside_the_policy_gives_standing_approval(env: Env) -> None:
    standing = env.standing()
    code, _ = hold_code(env.derive, standing, env.experiment(cell="claude-cli"))
    assert code == "STANDING_APPROVAL"
    no_cell = env.experiment(cell=None, tag="nocell")  # type: ignore[arg-type]
    assert hold_code(env.derive, standing, no_cell)[0] == "STANDING_APPROVAL"


@pytest.mark.parametrize(
    "plan",
    [
        {},
        {"budget": PLAN_BUDGET},
        "plan",
        None,
        [],
        {"schema": "amplai.canary-plan.v1", "budget": PLAN_BUDGET},
    ],
)
def test_a_plan_that_is_not_an_experiment_calibration_or_drift_plan_is_refused(
    env: Env, plan: Any
) -> None:
    standing = env.standing()
    assert hold_code(env.derive, standing, plan) == ("STANDING_APPROVAL", "hold")


def test_a_calibration_and_a_regression_drift_check_are_admitted_and_labelled(env: Env) -> None:
    standing = env.standing()
    calibration = env.approvals.issue_standing(
        env.nightly, standing, "experiment.execute", env.calibration(regression=False)
    )
    drift = env.approvals.issue_standing(
        env.nightly, standing, "experiment.execute", env.calibration(regression=True)
    )
    assert env.store.get(env.scope, APPROVAL_KIND, calibration)["plan_kind"] == "calibration"
    assert env.store.get(env.scope, APPROVAL_KIND, drift)["plan_kind"] == "drift"


def test_drift_is_a_plan_on_the_frozen_regression_set_not_a_corpus_id_lookalike(
    env: Env,
) -> None:
    standing = env.standing()
    # an eval-corpus that only calls itself amplai-regression-v1 is not the frozen set (§10.6)
    lookalike = env.put("eval-corpus", "amplai-regression-v1", {"corpus_id": "amplai-regression-v1",
                                                               "cases": []})  # fmt: skip
    plan = {**env.calibration(regression=False), "corpus_ref": lookalike}
    ref = env.approvals.issue_standing(env.nightly, standing, "experiment.execute", plan)
    assert env.store.get(env.scope, APPROVAL_KIND, ref)["plan_kind"] == "calibration"
    unindexed = env.calibration(regression=True, indexed=False)
    ref = env.approvals.issue_standing(env.nightly, standing, "experiment.execute", unindexed)
    assert env.store.get(env.scope, APPROVAL_KIND, ref)["plan_kind"] == "calibration"


def test_a_calibration_of_a_cell_outside_the_policy_is_refused(env: Env) -> None:
    standing = env.standing()
    plan = env.calibration(regression=True, cells=[CELL, "claude-cli"])
    assert hold_code(env.approvals.issue_standing, env.nightly, standing, "experiment.execute",
                     plan)[0] == "STANDING_APPROVAL"  # fmt: skip
    plan = env.calibration(regression=True, cells=[])
    assert hold_code(env.approvals.issue_standing, env.nightly, standing, "experiment.execute",
                     plan)[0] == "STANDING_APPROVAL"  # fmt: skip


def test_a_calibration_whose_corpus_is_not_stored_is_refused(env: Env) -> None:
    standing = env.standing()
    plan = env.calibration(regression=True)
    plan["corpus_ref"] = {"id": "ghost", "revision": 1, "digest": "sha256:" + "0" * 64}
    assert hold_code(env.approvals.issue_standing, env.nightly, standing, "experiment.execute",
                     plan)[0] == "STANDING_APPROVAL"  # fmt: skip


@pytest.mark.parametrize(
    "action",
    ["experiment.confirm", "canary.execute", "canary.approve", "release.promote",
     "release.rollback", "nightly.explore", "harness.review", ""],
)  # fmt: skip
def test_the_nightly_identity_never_derives_a_confirmatory_canary_or_promotion_approval(
    env: Env, action: str
) -> None:
    standing = env.standing()
    code, outcome = hold_code(
        env.approvals.issue_standing, env.nightly, standing, action, env.experiment()
    )
    assert (code, outcome) == ("STANDING_APPROVAL", "hold")


def test_only_the_nightly_service_identity_derives_approvals(env: Env) -> None:
    standing = env.standing()
    plan = env.experiment()
    for actor in (
        env.operator,
        env.dep.operator(),
        env.dep.meta_local.proposer,
        replace(env.nightly, kind="human"),
        replace(env.nightly, subject_id="someone-else"),
        replace(env.nightly, permissions=env.nightly.permissions | {"corpus.holdout.evaluate"}),
        replace(env.nightly, permissions=env.nightly.permissions | {"release.promote"}),
        replace(env.nightly, scope=replace(env.scope, project_id="other")),
    ):
        assert hold_code(
            env.approvals.issue_standing, actor, standing, "experiment.execute", plan
        ) == ("STANDING_APPROVAL", "hold"), actor


def test_a_nightly_identity_without_the_approve_permission_is_forbidden(env: Env) -> None:
    standing = env.standing()
    weak = replace(env.nightly, permissions=env.nightly.permissions - {"experiment.approve"})
    assert hold_code(
        env.approvals.issue_standing, weak, standing, "experiment.execute", env.experiment()
    ) == ("FORBIDDEN", "rejected")


def test_nothing_is_derived_without_a_current_standing_approval(env: Env) -> None:
    standing = env.standing()
    plan = env.experiment()
    env.approvals.revoke(env.operator, standing)
    assert hold_code(env.derive, standing, plan) == ("STANDING_APPROVAL", "hold")
    now = time.time()
    expired = env.standing(valid_from=iso(now - 2 * DAY), valid_until=iso(now - DAY))
    assert hold_code(env.derive, expired, plan)[0] == "STANDING_APPROVAL"
    assert hold_code(env.derive, {"id": "none", "revision": 1, "digest": "d"}, plan)[0] == (
        "STANDING_APPROVAL"
    )


# --- check: the derived approval is re-validated at every trial guard --------------------------


def test_a_derived_approval_stops_passing_when_the_standing_approval_is_revoked(env: Env) -> None:
    standing = env.standing()
    plan = env.experiment()
    ref = env.derive(standing, plan)
    assert env.approvals.check(env.scope, ref, "experiment.execute", digest(plan))
    env.approvals.revoke(env.operator, standing)
    assert hold_code(env.approvals.check, env.scope, ref, "experiment.execute", digest(plan)) == (
        "STANDING_APPROVAL", "hold",
    )  # fmt: skip


def test_a_derived_approval_stops_passing_when_the_standing_approval_expires(env: Env) -> None:
    standing = env.standing()
    plan = env.experiment()
    ref = env.derive(standing, plan)
    env.store.clock = lambda: time.time() + 10 * DAY
    assert (
        hold_code(env.approvals.check, env.scope, ref, "experiment.execute", digest(plan))[0]
        == "STANDING_APPROVAL"
    )


def test_the_derived_approval_itself_can_be_revoked(env: Env) -> None:
    standing = env.standing()
    plan = env.experiment()
    ref = env.derive(standing, plan)
    env.approvals.revoke(env.operator, ref)
    assert hold_code(env.approvals.check, env.scope, ref, "experiment.execute", digest(plan)) == (
        "META_APPROVAL", "hold",
    )  # fmt: skip
    assert env.approvals.current_standing() == standing  # the standing approval is untouched


def test_a_derived_approval_does_not_cover_another_action(env: Env) -> None:
    standing = env.standing()
    plan = env.experiment()
    ref = env.derive(standing, plan)
    for action in ("canary.execute", "release.promote", "experiment.confirm"):
        assert hold_code(env.approvals.check, env.scope, ref, action, digest(plan))[0] == (
            "META_APPROVAL"
        )


def test_a_standing_approval_is_never_an_action_approval(env: Env) -> None:
    policy = make_policy()
    standing = env.approvals.issue_nightly(env.operator, policy)
    for action in ("nightly.explore", "experiment.execute", "canary.execute"):
        assert hold_code(env.approvals.check, env.scope, standing, action, digest(policy))[0] == (
            "META_APPROVAL"
        )


@pytest.mark.parametrize(
    "approver_over",
    [
        {"subject_id": "other-service"},  # a service that is not the nightly identity
        {"kind": "agent"},
        {"kind": "human", "subject_id": NIGHTLY_ID},  # a human claiming the nightly id still passes
    ],
)
def test_a_non_human_approval_is_accepted_only_for_the_nightly_identity_with_a_standing_ref(
    env: Env, approver_over: dict[str, str]
) -> None:
    standing = env.standing()
    subject = "sha256:" + "2" * 64
    approver = {**env.nightly.wire(), **approver_over}
    ref = env.put(APPROVAL_KIND, "meta-approval-x", {
        "approval_id": "meta-approval-x", "action": "experiment.execute",
        "subject_digest": subject, "approved_by": approver, "approved_at": "2026-10-01T00:00:00Z",
        "revoked": False, "standing_ref": {k: standing[k] for k in ("id", "revision", "digest")},
    })  # fmt: skip
    if approver["kind"] == "human":
        assert env.approvals.check(env.scope, ref, "experiment.execute", subject)
    else:
        assert hold_code(env.approvals.check, env.scope, ref, "experiment.execute", subject) == (
            "META_APPROVAL", "hold",
        )  # fmt: skip


def test_a_nightly_approval_without_a_standing_ref_is_refused(env: Env) -> None:
    subject = "sha256:" + "3" * 64
    ref = env.put(APPROVAL_KIND, "meta-approval-y", {
        "approval_id": "meta-approval-y", "action": "experiment.execute",
        "subject_digest": subject, "approved_by": env.nightly.wire(),
        "approved_at": "2026-10-01T00:00:00Z", "revoked": False,
    })  # fmt: skip
    assert hold_code(env.approvals.check, env.scope, ref, "experiment.execute", subject)[0] == (
        "META_APPROVAL"
    )


def test_a_nightly_approval_for_a_canary_is_refused_even_with_a_standing_ref(env: Env) -> None:
    standing = env.standing()
    subject = "sha256:" + "4" * 64
    ref = env.put(APPROVAL_KIND, "meta-approval-z", {
        "approval_id": "meta-approval-z", "action": "canary.execute", "subject_digest": subject,
        "approved_by": env.nightly.wire(), "approved_at": "2026-10-01T00:00:00Z",
        "revoked": False,
        "standing_ref": {k: standing[k] for k in ("id", "revision", "digest")},
    })  # fmt: skip
    assert hold_code(env.approvals.check, env.scope, ref, "canary.execute", subject)[0] == (
        "META_APPROVAL"
    )


def test_a_human_operator_approval_still_works_as_before(env: Env) -> None:
    subject = "sha256:" + "5" * 64
    ref = env.approvals.issue(env.operator, "experiment.execute", subject)
    assert (
        env.approvals.check(env.scope, ref, "experiment.execute", subject)["approved_by"]["kind"]
        == "human"
    )
    assert (
        hold_code(env.approvals.check, env.scope, ref, "experiment.execute", "sha256:other")[0]
        == "META_APPROVAL"
    )


def test_a_missing_approval_reference_is_a_hold(env: Env) -> None:
    assert hold_code(
        env.approvals.check, env.scope, {"id": "ghost", "revision": 1, "digest": "d"},
        "experiment.execute", "sha256:x",
    ) == ("META_APPROVAL", "hold")  # fmt: skip


# --- the nightly identity cannot select holdout (FORBIDDEN) -------------------------------------


def test_the_nightly_identity_cannot_select_or_consume_holdout(env: Env) -> None:
    corpus = env.put("eval-corpus", "corpus-h", {"corpus_id": "own-v2", "cases": [],
                                                  "holdout_use_limit": 1})  # fmt: skip
    service = env.dep.meta_local.evaluation.corpus
    assert service.select(env.nightly, corpus, "development", purpose="exploratory") == []
    for split in ("holdout",):
        assert hold_code(
            service.select, env.nightly, corpus, split, purpose="frozen_experiment"
        ) == (
            "FORBIDDEN", "rejected",
        )  # fmt: skip
        assert hold_code(service.select, env.nightly, corpus, split, purpose="exploratory")[0] == (
            "FORBIDDEN"
        )
    assert (
        hold_code(
            service.consume_holdout, env.nightly, corpus, {"id": "e", "revision": 1, "digest": "d"}
        )[0]
        == "FORBIDDEN"
    )


# --- approve: trial goals only ------------------------------------------------------------------


def seed_plan(env: Env, goal_id: str, **plan: Any) -> None:
    with env.store.tx() as db:
        env.store.cas(db, env.scope, PLAN_KIND, goal_id, 0, plan.get("status", "x"), plan)


def test_the_nightly_identity_cannot_approve_a_non_trial_goal(env: Env) -> None:
    seed_plan(env, "goal-real", status="awaiting_approval", app="app")
    code, outcome = hold_code(env.dep.service.approve, env.nightly, "goal-real")
    assert (code, outcome) == ("APPROVER_KIND", "rejected")
    seed_plan(env, "goal-notrial", status="awaiting_approval", app="app", trial=None)
    assert hold_code(env.dep.service.approve, env.nightly, "goal-notrial")[0] == "APPROVER_KIND"


def test_the_nightly_identity_passes_the_identity_gate_for_a_trial_goal(env: Env) -> None:
    # not awaiting approval: the next gate answers, so the identity gate was passed
    seed_plan(env, "goal-trial", status="planning", app="app", trial={"subject": "s"})
    assert hold_code(env.dep.service.approve, env.nightly, "goal-trial") == (
        "PLAN_NOT_READY", "hold",
    )  # fmt: skip


@pytest.mark.parametrize(
    "actor_over",
    [
        {"subject_id": "other-service"},
        {"permissions_add": {"corpus.holdout.evaluate"}},
        {"permissions_add": {"canary.approve"}},
        {"permissions_add": {"release.promote"}},
        {"permissions_add": {"harness.review"}},
        {"scope": "other"},
    ],
)
def test_a_lookalike_service_cannot_approve_even_a_trial_goal(env: Env, actor_over: dict) -> None:
    seed_plan(env, "goal-trial", status="planning", app="app", trial={"subject": "s"})
    actor = env.nightly
    if "subject_id" in actor_over:
        actor = replace(actor, subject_id=actor_over["subject_id"])
    if "permissions_add" in actor_over:
        actor = replace(actor, permissions=actor.permissions | actor_over["permissions_add"])
    if "scope" in actor_over:
        actor = replace(actor, scope=replace(env.scope, project_id="other"))
    assert hold_code(env.dep.service.approve, actor, "goal-trial")[0] == "APPROVER_KIND"


def test_a_service_without_execution_approve_is_forbidden(env: Env) -> None:
    seed_plan(env, "goal-trial", status="planning", app="app", trial={"subject": "s"})
    weak = replace(env.nightly, permissions=env.nightly.permissions - {"execution.approve"})
    assert hold_code(env.dep.service.approve, weak, "goal-trial") == ("FORBIDDEN", "rejected")


def test_a_non_nightly_service_and_the_proposer_cannot_approve_any_goal(env: Env) -> None:
    seed_plan(env, "goal-trial", status="planning", app="app", trial={"subject": "s"})
    other = Actor("other-service", env.scope, frozenset({"execution.approve"}), "service", "x")
    assert hold_code(env.dep.service.approve, other, "goal-trial")[0] == "APPROVER_KIND"
    assert hold_code(env.dep.service.approve, env.dep.meta_local.proposer, "goal-trial")[0] == (
        "FORBIDDEN"
    )


def test_the_human_operator_still_approves_through_the_same_gate(env: Env) -> None:
    seed_plan(env, "goal-human", status="planning", app="app")
    assert hold_code(env.dep.service.approve, env.operator, "goal-human")[0] == "PLAN_NOT_READY"


# --- the trial executor accepts the nightly identity -------------------------------------------


def make_executor(env: Env, operator: Actor, *, publisher: Any = None) -> LocalTrialExecutor:
    corpus = SimpleNamespace(tasks=[], base=Path(env.store.root))
    loop = SimpleNamespace(publisher=publisher)
    return LocalTrialExecutor(env.dep.service, loop, env.dep.goals, operator, corpus)  # type: ignore[arg-type]


def test_the_trial_executor_accepts_the_human_operator_and_the_nightly_identity(env: Env) -> None:
    assert make_executor(env, env.operator).operator is env.operator
    assert make_executor(env, env.nightly).operator is env.nightly


def test_the_trial_executor_refuses_other_services_and_a_nightly_with_excluded_rights(
    env: Env,
) -> None:
    other = Actor("someone", env.scope, frozenset({"experiment.run"}), "service", "x")
    assert hold_code(make_executor, env, other) == ("TRIAL_OPERATOR", "hold")
    assert hold_code(make_executor, env, env.dep.meta_local.proposer)[0] == "TRIAL_OPERATOR"
    for extra in ("corpus.holdout.evaluate", "release.promote", "canary.run", "harness.review"):
        rich = replace(env.nightly, permissions=env.nightly.permissions | {extra})
        assert hold_code(make_executor, env, rich) == ("TRIAL_OPERATOR", "hold"), extra


def test_a_trial_executor_never_publishes_whoever_runs_it(env: Env) -> None:
    for actor in (env.operator, env.nightly):
        assert hold_code(make_executor, env, actor, publisher=object()) == ("TRIAL_PUBLISH", "hold")


def test_the_module_exposes_the_nightly_action_and_id() -> None:
    assert meta_local.NIGHTLY_ACTION == "nightly.explore" and NIGHTLY_ID == "amplai-meta-nightly"
