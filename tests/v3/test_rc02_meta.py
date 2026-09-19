"""V3 test-catalog cases for groups meta, meta decision and canary promote.

T-079..T-083 (meta), T-084..T-086 (meta decision), T-088/T-091 (canary promote).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from amplai_foundry.evaluation.analysis import analyze_pairs
from amplai_foundry.meta_harness.reference import MetaReference
from amplai_foundry.runtime.contracts.identity import canonical, digest, new_id, now
from amplai_foundry.runtime.errors import Conflict, Hold


@pytest.fixture
def meta(tmp_path):
    m = MetaReference(tmp_path / "meta")
    try:
        yield m
    finally:
        m.close()


def observations(n=24, repeats=1, candidate=True, cost_b=10, cost_c=5, safety=0):
    rows = [
        {
            "task_id": str(i),
            "arm": arm,
            "repeat": r,
            "success": True if arm == "baseline" else candidate,
            "safety_failures": 0,
            "unknown_effects": 0,
            "cost_microunits": cost_b if arm == "baseline" else cost_c,
            "elapsed_ms": 2.0,
            "usage_status": "measured",
        }
        for i in range(n)
        for arm in ("baseline", "candidate")
        for r in range(repeats)
    ]
    if safety:
        rows[0]["safety_failures"] = safety
    return rows


def analysis_plan(**kw):
    return {
        "method": "paired_binary_conservative",
        "confidence": 0.95,
        "minimum_tasks": 24,
        "repeats_per_task": 1,
        "noninferiority_margin": 0.25,
        "safety_failure_limit": 0,
        "missing_policy": "inconclusive",
        "purpose": "local_qualification",
        **kw,
    }


def test_t079_protected_evaluator_change_blocks_screen(meta):
    m = meta
    p = m.prepare()
    base = m.d.store.get(m.d.scope, "harness-change-proposal", p["proposal_ref"])
    change = m.d.artifacts.admit(
        m.d.scope,
        canonical(
            {
                "baseline_ref": base["baseline_ref"],
                "candidate_ref": base["candidate_ref"],
                "changed_paths": ["src/amplai_foundry/verification/verifier.py"],
            }
        ),
        "application/json",
        trust="operator",
    )
    proposal = {**base, "proposal_id": new_id("screen-weaken-verifier"), "change_artifact": change}
    m.meta.submit(m.proposer, proposal)
    # given: candidate diff also touches the protected verifier surface
    with pytest.raises(Hold) as exc:
        # when: reviewer screens the proposal
        m.meta.screen(m.reviewer, proposal["proposal_id"])
    # expected: class C -> separate governed Work; screen does not admit the same experiment
    assert exc.value.code == "PROTECTED_META_SURFACE"
    assert m.d.store.head(m.d.scope, "evolution", proposal["proposal_id"])["state"] == "draft"


def test_t080_holdout_leakage_denied_to_proposer(meta):
    m = meta
    artifact = m.d.artifacts.admit(
        m.d.scope, b'{"expected":99}', "application/json", trust="operator"
    )
    corpus_ref = m.eval.corpus.freeze(
        m.reviewer,
        new_id("sealed-holdout"),
        [
            {
                "case_id": "hidden-1",
                "split": "holdout",
                "task_class": "sum",
                "artifact_ref": artifact,
            }
        ],
        holdout_use_limit=1,
    )
    # a proposer identity that (defense-in-depth) also holds read access
    reader_proposer = replace(m.proposer, permissions=m.proposer.permissions | {"corpus.read"})
    # given: MetaProposer requests hidden evaluation answers
    with pytest.raises(Hold) as exc:
        # when: proposer fetches the sealed holdout corpus
        m.eval.corpus.select(reader_proposer, corpus_ref, "holdout", purpose="evaluation")
    # expected: access denied
    assert exc.value.code == "HOLDOUT_PROPOSER"


def test_t081_immutable_experiment_rejects_corpus_change(meta):
    m = meta
    p = m.prepare()
    scope = m.d.scope
    original = m.d.store.get(scope, "eval-experiment", p["experiment_ref"])
    alt_corpus = m.eval.corpus.freeze(
        m.reviewer, new_id("alt-corpus"), p["cases"], holdout_use_limit=1
    )
    changed = {**original, "corpus_ref": alt_corpus}
    changed["approval_ref"] = m.approve(
        "experiment.execute", digest({k: v for k, v in changed.items() if k != "approval_ref"})
    )
    # given: experiment p["experiment_ref"] is already frozen
    with pytest.raises(Conflict) as exc:
        # when: a same-id refreeze changes the task corpus
        m.eval.freeze(m.reviewer, changed)
    # expected: rejected as an immutable-revision conflict; the original is preserved
    assert exc.value.code == "IMMUTABLE_REVISION"
    assert m.d.store.get(scope, "eval-experiment", p["experiment_ref"]) == original


def test_t082_provider_alias_drift_marks_inconclusive_and_blocks_promotion(meta):
    m = meta
    p = m.prepare()
    m.meta.start_offline(m.reviewer, p["proposal_id"])

    def callback(*args):
        obs = m.execute_case(*args)
        # given: baseline/candidate resolution changes mid-run (changed provider alias)
        m.eval.environment_probe = lambda env: {**env, "resolved_alias": "changed-provider-alias"}
        return obs

    # when: analyze (eval.run re-probes the pinned environment before each trial)
    report_ref = m.eval.run(m.reviewer, p["experiment_ref"], callback)
    report = m.d.store.get(m.d.scope, "eval-report", report_ref)
    # expected: environment_drifted with limits
    assert report["verdict"] == "inconclusive" and report["environment_drifted"] is True
    m.meta.evaluate(m.reviewer, p["proposal_id"], report_ref)
    # expected: no harness-only improvement claim can be promoted from a drifted report
    with pytest.raises(Hold) as exc:
        m.meta._passing_report(m.d.scope, report_ref)
    assert exc.value.code == "EVAL_NOT_PASSING"


def test_t083_replay_mode_never_claims_causal_promotion(meta):
    m = meta
    # given: recorded tool outcomes are replayed without a live model/tool
    p = m.prepare(mode="replay")
    # when: report is produced
    result = m.execute(p, rollback=False)
    report = m.d.store.get(m.d.scope, "eval-report", result["report_ref"])
    # expected: marked as not evidence of real-world success improvement; not promoted
    assert result["verdict"] == "inconclusive" and result["promoted"] is False
    assert any("Mode: replay" in line for line in report["limitations"])
    assert (
        m.d.store.head(m.d.scope, "release-pointer", "active")["data"]["release_ref"]
        == p["baseline_release_ref"]
    )


def test_t084_insufficient_evidence_small_noisy_sample_is_inconclusive(meta):
    # given: a small/noisy sample under the frozen noninferiority rule (minimum_tasks=24)
    ts = observations(n=5, candidate=False)
    for i in (0, 2, 4):
        ts[2 * i + 1]["success"] = True
    # when: analyze
    result = analyze_pairs(ts, analysis_plan(), expected_tasks=[str(i) for i in range(5)])
    # expected: inconclusive; no manufactured performance guarantee
    assert result["verdict"] == "inconclusive"
    assert "insufficient_distinct_tasks" in result["reasons"]


def test_t085_failed_safety_gate_dominates_cost_benefit(meta):
    plan = analysis_plan(
        benefit={
            "endpoint": "cost_mean_microunits",
            "minimum_reduction": 2,
            "bootstrap_samples": 1000,
            "seed": 42,
        }
    )
    # given: candidate improves cost but violates the safety gate on one trial
    ts = observations(cost_c=5, safety=1)
    # when: evaluate
    result = analyze_pairs(ts, plan, expected_tasks=[str(i) for i in range(24)])
    # expected: fail regardless of the passing aggregate/cost-benefit score
    assert result["benefit"]["verdict"] == "pass"
    assert result["verdict"] == "fail"
    assert "safety_failure" in result["reasons"]


def test_t086_exploratory_purpose_cannot_auto_promote(meta):
    m = meta
    p = m.prepare()
    scope = m.d.scope
    frozen = m.d.store.get(scope, "eval-experiment", p["experiment_ref"])
    base = m.d.store.get(scope, "harness-change-proposal", p["proposal_ref"])
    draftplan = m.put(
        "experiment-draft",
        {"draft_id": new_id("exp-draft-2"), "comparison": "paired actual arithmetic workspaces"},
    )
    rollback = m.put(
        "rollback-plan",
        {"rollback_id": new_id("rollback-2"), "target_release_ref": p["baseline_release_ref"]},
    )
    observation = m.d.artifacts.admit(
        m.d.scope,
        b'{"issue":"second candidate reusing the same frozen evaluation set"}',
        "application/json",
        trust="verifier",
    )
    observation_ref = m.put(
        "harness-observation",
        {"observation_id": new_id("observation-2"), "artifact": observation},
    )
    proposal2 = {
        **base,
        "proposal_id": new_id("harness-proposal-exploratory"),
        "experiment_plan_ref": draftplan,
        "rollback_plan_ref": rollback,
        "observation_refs": [observation_ref],
    }
    proposal2_ref = m.meta.submit(m.proposer, proposal2)
    m.meta.screen(m.reviewer, str(proposal2["proposal_id"]))
    exploratory_analysis = m.put(
        "analysis-plan",
        {
            "analysis_id": new_id("analysis-exploratory"),
            "policy": {**analysis_plan(), "purpose": "exploratory"},
        },
    )
    sampling = m.put(
        "sampling-plan",
        {
            "sampling_id": new_id("sampling-2"),
            "split": "validation",
            "case_ids": [c["case_id"] for c in p["cases"]],
        },
    )
    plan2 = {
        "schema_version": "3.0.0",
        "experiment_id": new_id("experiment-exploratory"),
        "scope": scope.wire(),
        "proposal_ref": proposal2_ref,
        "baseline_ref": p["baseline_ref"],
        "candidate_ref": p["candidate_ref"],
        "corpus_ref": frozen["corpus_ref"],
        "verifier_ref": frozen["verifier_ref"],
        "environment_ref": frozen["environment_ref"],
        "mode": "sandbox_rerun",
        "primary_endpoint": "task_all_repeats_pass",
        "analysis_plan_ref": exploratory_analysis,
        "sampling_plan_ref": sampling,
        "safety_stop_rules": ["Any safety failure or unknown effect stops the run"],
        "holdout_policy_ref": frozen["holdout_policy_ref"],
        "budget": frozen["budget"],
        "frozen_at": now(),
    }
    plan2["approval_ref"] = m.approve("experiment.execute", digest(plan2))
    ref2 = m.eval.freeze(m.reviewer, plan2)
    m.meta.approve_experiment(
        m.reviewer, str(proposal2["proposal_id"]), plan2["approval_ref"], ref2
    )
    m.meta.start_offline(m.reviewer, str(proposal2["proposal_id"]))
    # given/when: a further candidate is confirmatorily evaluated against the same frozen
    # validation set, but its own plan only declares "exploratory" purpose
    report_ref = m.eval.run(m.reviewer, ref2, m.execute_case)
    report = m.d.store.get(scope, "eval-report", report_ref)
    # expected: reuse/purpose policy enforced; exploratory result is not auto-promoted
    assert report["verdict"] == "inconclusive"
    with pytest.raises(Hold) as exc:
        m.meta._passing_report(scope, report_ref)
    assert exc.value.code == "EVAL_NOT_PASSING"


def test_t088_canary_kill_switch_on_unknown_effect_aborts_admission(meta):
    m = meta
    p = m.prepare()
    pid = p["proposal_id"]
    m.meta.start_offline(m.reviewer, pid)
    report_ref = m.eval.run(m.reviewer, p["experiment_ref"], m.execute_case)
    m.meta.evaluate(m.reviewer, pid, report_ref)
    task_bindings = {c["case_id"]: m.canary_target(c["case_id"]) for c in p["cases"][:2]}
    targets = list({digest(r): r for r in task_bindings.values()}.values())
    policy = {
        "policy_id": new_id("canary-policy"),
        "eligible_task_ids": list(task_bindings),
        "max_runs": 2,
        "max_wall_seconds": 120,
        "max_cost_microunits": 100,
        "max_trial_cost_microunits": 10,
        "max_trial_tokens": 0,
        "max_concurrent": 1,
        "project_opt_in": True,
        "eligible_risk_classes": ["low"],
        "target_binding_refs": targets,
        "task_binding_refs": task_bindings,
        "abort_on_safety_failure": True,
        "abort_on_unknown_effect": True,
        "fallback_release_ref": p["baseline_release_ref"],
    }
    policy_ref = m.put("canary-policy", policy)
    approval = m.approve(
        "canary.execute",
        digest(
            {"proposal_ref": p["proposal_ref"], "report_ref": report_ref, "policy_ref": policy_ref}
        ),
    )
    m.meta.approve_canary(m.reviewer, pid, policy_ref, approval)
    m.meta.start_canary(m.reviewer, pid)
    first_task = p["cases"][0]["case_id"]

    def incident(_: str) -> dict:
        obs = m.execute_case(p["candidate_ref"], p["cases"][0], 0, "canary")
        return {
            "success": obs.success,
            # given: canary produces an unknown effect / security incident
            "safety_failures": 0,
            "unknown_effects": 1,
            "cost_microunits": obs.cost_microunits,
            "artifact_ref": obs.artifact_refs[0],
            "input_tokens": obs.input_tokens,
            "output_tokens": obs.output_tokens,
        }

    # when: the incident is observed (next scheduler tick admits/settles the trial)
    result = m.meta.canary_trial(m.reviewer, pid, first_task, incident)
    # expected: abort; baseline fallback (active pointer left untouched)
    assert result["state"] == "aborted"
    assert result["fallback"] == "candidate admission stopped; active pointer unchanged"
    assert (
        m.d.store.head(m.d.scope, "release-pointer", "active")["data"]["release_ref"]
        == p["baseline_release_ref"]
    )
    # expected: new admission is aborted (no further canary trial is accepted)
    with pytest.raises(Hold) as exc:
        m.meta.canary_trial(m.reviewer, pid, p["cases"][1]["case_id"], incident)
    assert exc.value.code == "CANARY_STATE"


def test_t091_in_flight_run_composition_pinned_against_promotion(meta):
    m = meta
    p = m.prepare()
    pid = p["proposal_id"]
    m.meta.start_offline(m.reviewer, pid)
    report_ref = m.eval.run(m.reviewer, p["experiment_ref"], m.execute_case)
    m.meta.evaluate(m.reviewer, pid, report_ref)
    task_bindings = {c["case_id"]: m.canary_target(c["case_id"]) for c in p["cases"][:2]}
    targets = list({digest(r): r for r in task_bindings.values()}.values())
    policy = {
        "policy_id": new_id("canary-policy"),
        "eligible_task_ids": list(task_bindings),
        "max_runs": 2,
        "max_wall_seconds": 120,
        "max_cost_microunits": 100,
        "max_trial_cost_microunits": 10,
        "max_trial_tokens": 0,
        "max_concurrent": 1,
        "project_opt_in": True,
        "eligible_risk_classes": ["low"],
        "target_binding_refs": targets,
        "task_binding_refs": task_bindings,
        "abort_on_safety_failure": True,
        "abort_on_unknown_effect": True,
        "fallback_release_ref": p["baseline_release_ref"],
    }
    policy_ref = m.put("canary-policy", policy)
    approval = m.approve(
        "canary.execute",
        digest(
            {"proposal_ref": p["proposal_ref"], "report_ref": report_ref, "policy_ref": policy_ref}
        ),
    )
    m.meta.approve_canary(m.reviewer, pid, policy_ref, approval)
    m.meta.start_canary(m.reviewer, pid)
    for case in p["cases"][:2]:

        def run_canary(_: str, case=case) -> dict:
            obs = m.execute_case(p["candidate_ref"], case, 0, "canary")
            return {
                "success": obs.success,
                "safety_failures": obs.safety_failures,
                "unknown_effects": obs.unknown_effects,
                "cost_microunits": obs.cost_microunits,
                "artifact_ref": obs.artifact_refs[0],
                "input_tokens": obs.input_tokens,
                "output_tokens": obs.output_tokens,
            }

        m.meta.canary_trial(m.reviewer, pid, case["case_id"], run_canary)
    m.meta.request_promotion(m.reviewer, pid)
    # given: an old Run is still active, bound to the baseline composition
    with m.d.store.tx() as db:
        m.d.store.cas(
            db,
            m.d.scope,
            "run",
            "in-flight-run",
            0,
            "running",
            {"composition_ref": p["baseline_ref"], "process_stopped": False},
        )
    plan = {
        "schema_version": "3.0.0",
        "promotion_id": new_id("promotion"),
        "scope": m.d.scope.wire(),
        "expected_active_release_ref": p["baseline_release_ref"],
        "candidate_release_ref": p["candidate_release_ref"],
        "eval_report_ref": report_ref,
        "target_binding_refs": targets,
        "canary_policy_ref": policy_ref,
        "abort_rules": ["No unknown effects", "No security regression"],
        "rollback_release_ref": p["baseline_release_ref"],
        "expires_at": (datetime.fromtimestamp(m.d.store.clock(), UTC) + timedelta(minutes=5))
        .isoformat()
        .replace("+00:00", "Z"),
        "active_run_policy": "drain",
    }
    plan["grant_ref"] = m.approve("release.promote", digest(plan))
    # when: promotion is attempted while the Run is read/continuing
    with pytest.raises(Hold) as exc:
        m.meta.promote(m.reviewer, pid, plan)
    # expected: promotion is blocked; the in-flight Run's composition binding is not overwritten
    assert exc.value.code == "DRAIN_REQUIRED"
    run_after = m.d.store.head(m.d.scope, "run", "in-flight-run")
    assert run_after["row_version"] == 1
    assert run_after["data"]["composition_ref"] == p["baseline_ref"]
    assert (
        m.d.store.head(m.d.scope, "release-pointer", "active")["data"]["release_ref"]
        == p["baseline_release_ref"]
    )
